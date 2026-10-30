#!/usr/bin/env python3
"""봇별 성장/전투 지표를 5분마다 SQLite 시계열로 수집한다.

- 소스: 게임 MariaDB (읽기 전용 쿼리만)
- 저장: scripts/metrics.db (SQLite)
- Discord 전투 알림: 수집과 분리해 10분마다 전투 파괴량 TOP 5 요약
- 부수 임무: universe.log / ai_loop.log 가 50MB 넘으면 로테이션 (.1 하나만 유지)

실행: nohup python3 scripts/metrics_collector.py > /dev/null 2>&1 &
분석 예시:
  sqlite3 scripts/metrics.db "SELECT personality, AVG(building_sum) FROM snapshots
    WHERE ts > datetime('now','-6 hours') GROUP BY personality;"
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT / "scripts" / "metrics.db"
OGAMEX_DIR = Path.home() / "Code" / "ogamex"
INTERVAL_SEC = 300
BATTLE_NOTIFICATION_INTERVAL_SEC = 600
LOG_ROTATE_BYTES = 50 * 1024 * 1024
LOGS_TO_ROTATE = [PROJECT / "scripts" / "universe.log", PROJECT / "scripts" / "ai_loop.log"]
SIMULATION_ADMIN_EMAIL = "simulation-admin@ogamex.local"

# 성격 배정 재현 (이메일 시드 결정적)
import sys
sys.path.insert(0, str(PROJECT))
try:
    from bots.rule_based.personality import assign_personality
except Exception:
    assign_personality = None
try:
    from scripts import discord_battle_notifier as battle_notifier
except ImportError:  # ``python3 scripts/metrics_collector.py`` 직접 실행 호환
    import discord_battle_notifier as battle_notifier


def game_sql(query: str) -> list[list[str]]:
    """도커 MariaDB에 읽기 전용 쿼리 실행, 탭 구분 행 반환."""
    r = subprocess.run(
        ["docker", "compose", "exec", "-T", "ogamex-db",
         "mariadb", "-uroot", "-ptoor", "laravel", "-N", "-e", query],
        capture_output=True, text=True, cwd=OGAMEX_DIR, timeout=60,
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:200])
    return [line.split("\t") for line in r.stdout.strip().splitlines() if line]


def init_db(con: sqlite3.Connection) -> None:
    con.executescript("""
    CREATE TABLE IF NOT EXISTS snapshots (
        ts TEXT NOT NULL,               -- UTC ISO
        email TEXT NOT NULL,
        personality TEXT,
        planets INTEGER,
        building_sum INTEGER,           -- 자원건물 4종 레벨 합 (전 행성)
        facility_sum INTEGER,           -- 로봇/조선소/연구소 합
        defense_sum INTEGER,            -- 방어 유닛 수 합
        ships_sum INTEGER,              -- 주요 함선 수 합
        metal REAL, crystal REAL, deuterium REAL,
        score INTEGER                   -- highscores.general (있으면)
    );
    CREATE INDEX IF NOT EXISTS idx_snap ON snapshots(ts, email);
    CREATE TABLE IF NOT EXISTS battles (
        ts TEXT NOT NULL,               -- 수집 시각
        battle_id INTEGER UNIQUE,       -- battle_reports.id
        attacker_id INTEGER, defender_id INTEGER,
        loot_metal INTEGER, loot_crystal INTEGER, loot_deuterium INTEGER,
        battle_ts TEXT,                 -- 실제 전투 시각(UTC ISO)
        attacker_email TEXT, defender_email TEXT,
        attacker_loss REAL, defender_loss REAL,
        is_probe_only INTEGER,          -- 공격측 함선이 정찰기뿐이면 1 (Discord 소음 필터)
        is_admin_test INTEGER           -- 전용 Admin 수동 전투/명시 보정 전투면 1
    );
    CREATE TABLE IF NOT EXISTS debris_harvests (
        message_id INTEGER PRIMARY KEY, -- messages.id (중복 수집 방지)
        event_ts TEXT,                  -- 실제 회수 시각(UTC ISO)
        email TEXT,
        metal INTEGER, crystal INTEGER, deuterium INTEGER
    );
    CREATE TABLE IF NOT EXISTS metric_capabilities (
        metric TEXT PRIMARY KEY,
        last_success_ts TEXT NOT NULL   -- 원본 DB 읽기가 실제 성공한 마지막 시각
    );
    CREATE TABLE IF NOT EXISTS stall_events (
        ts TEXT NOT NULL,               -- 감지 시각
        cancel_rate REAL,               -- 최근 1h 건설 취소율 %
        building_sum REAL,              -- 당시 평균 건물합
        detail TEXT                     -- 판정 근거
    );
    """)
    # 기존 metrics.db는 battles 7컬럼 스키마로 이미 만들어져 있다. CREATE TABLE IF NOT
    # EXISTS만으로는 컬럼이 늘지 않으므로 멱등 ALTER를 적용한다. 모두 nullable이라 과거 행을
    # 0으로 오인하지 않고, 다음 collect()가 라이브 원본에서 가능한 값만 backfill한다.
    battle_columns = {row[1] for row in con.execute("PRAGMA table_info(battles)")}
    for name, sql_type in {
        "battle_ts": "TEXT",
        "attacker_email": "TEXT",
        "defender_email": "TEXT",
        "attacker_loss": "REAL",
        "defender_loss": "REAL",
        "is_probe_only": "INTEGER",
        "is_admin_test": "INTEGER",
    }.items():
        if name not in battle_columns:
            con.execute(f"ALTER TABLE battles ADD COLUMN {name} {sql_type}")
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_battle_scorecard "
        "ON battles(battle_ts, attacker_email, defender_email)"
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_harvest_scorecard "
        "ON debris_harvests(event_ts, email)"
    )
    con.commit()


def _nullable_number(value: str, *, integer: bool = False) -> int | float | None:
    """mariadb CLI의 NULL/빈 문자열을 0과 구분해 파싱한다."""
    if value in {"", "NULL", "null", "None"}:
        return None
    number = float(value)
    return int(number) if integer else number


def _event_ts(value: str) -> str | None:
    """MariaDB UNIX_TIMESTAMP 값을 SQLite의 UTC ISO 문자열로 바꾼다."""
    epoch = _nullable_number(value, integer=True)
    if epoch is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _mark_capability(con: sqlite3.Connection, metric: str, ts: str) -> None:
    con.execute(
        """INSERT INTO metric_capabilities(metric, last_success_ts) VALUES (?,?)
           ON CONFLICT(metric) DO UPDATE SET last_success_ts=excluded.last_success_ts""",
        (metric, ts),
    )


def collect(con: sqlite3.Connection) -> None:
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    rows = game_sql("""
        SELECT u.email,
               COUNT(p.id),
               COALESCE(SUM(p.metal_mine+p.crystal_mine+p.deuterium_synthesizer+p.solar_plant),0),
               COALESCE(SUM(p.robot_factory+p.shipyard+p.research_lab),0),
               COALESCE(SUM(p.rocket_launcher+p.light_laser+p.heavy_laser+p.gauss_cannon+p.ion_cannon+p.plasma_turret+p.small_shield_dome+p.large_shield_dome),0),
               COALESCE(SUM(p.light_fighter+p.heavy_fighter+p.cruiser+p.small_cargo+p.large_cargo+p.espionage_probe+p.recycler+p.colony_ship),0),
               COALESCE(SUM(p.metal),0), COALESCE(SUM(p.crystal),0), COALESCE(SUM(p.deuterium),0),
               COALESCE(MAX(h.general),0)
        FROM users u
        LEFT JOIN planets p ON p.user_id=u.id
        LEFT JOIN highscores h ON h.player_id=u.id
        WHERE u.email LIKE '%bots.local'
        GROUP BY u.id""")
    for r in rows:
        email = r[0]
        pers = None
        if assign_personality and (email.startswith("bot") or email in {"pirate01@bots.local", "explorer01@bots.local"}):
            try:
                pers = assign_personality(email)
            except Exception:
                pers = None
        elif email.startswith("ai"):
            pers = "ai_agent"
        con.execute(
            "INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, email, pers, *[int(float(x)) for x in r[1:6]],
             float(r[6]), float(r[7]), float(r[8]), int(r[9])),
        )
    # 전투 기록. 실제 스키마: 공격자는 attacker(JSON longtext) 안, 피해자는
    # planet_user_id, 약탈은 loot(JSON). ON CONFLICT UPDATE로 옛 7컬럼 행도 손실/이메일을
    # backfill한다. 값이 원본에 없으면 NULL을 유지해 "손실 0"으로 오인하지 않는다.
    try:
        brows = game_sql("""
            SELECT br.id,
                   COALESCE(JSON_EXTRACT(br.attacker,'$.player_id'),0),
                   COALESCE(br.planet_user_id,0),
                   COALESCE(JSON_EXTRACT(br.loot,'$.metal'),0),
                   COALESCE(JSON_EXTRACT(br.loot,'$.crystal'),0),
                   COALESCE(JSON_EXTRACT(br.loot,'$.deuterium'),0),
                   UNIX_TIMESTAMP(br.created_at),
                   COALESCE(ua.email,''), COALESCE(ud.email,''),
                   JSON_UNQUOTE(JSON_EXTRACT(br.attacker,'$.resource_loss')),
                   JSON_UNQUOTE(JSON_EXTRACT(br.defender,'$.resource_loss')),
                   CASE
                     WHEN JSON_LENGTH(JSON_EXTRACT(br.attacker,'$.units')) = 1
                      AND COALESCE(CAST(JSON_UNQUOTE(JSON_EXTRACT(
                            br.attacker,'$.units.espionage_probe')) AS UNSIGNED),0) > 0
                     THEN 1 ELSE 0
                   END,
                   CASE
                     WHEN LOWER(COALESCE(JSON_UNQUOTE(JSON_EXTRACT(
                            br.general,'$.admin_test')),'')) IN ('true','1')
                       OR COALESCE(ua.email,'') = 'simulation-admin@ogamex.local'
                       OR COALESCE(ud.email,'') = 'simulation-admin@ogamex.local'
                     THEN 1 ELSE 0
                   END
            FROM battle_reports br
            LEFT JOIN users ua ON ua.id = CAST(
                JSON_UNQUOTE(JSON_EXTRACT(br.attacker,'$.player_id')) AS UNSIGNED)
            LEFT JOIN users ud ON ud.id = br.planet_user_id""")
        for b in brows:
            con.execute(
                """INSERT INTO battles (
                       ts, battle_id, attacker_id, defender_id,
                       loot_metal, loot_crystal, loot_deuterium,
                       battle_ts, attacker_email, defender_email,
                       attacker_loss, defender_loss, is_probe_only, is_admin_test
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(battle_id) DO UPDATE SET
                       battle_ts=excluded.battle_ts,
                       attacker_email=excluded.attacker_email,
                       defender_email=excluded.defender_email,
                       attacker_loss=excluded.attacker_loss,
                       defender_loss=excluded.defender_loss,
                       is_probe_only=excluded.is_probe_only,
                       is_admin_test=excluded.is_admin_test""",
                (
                    ts,
                    *[int(float(x or 0)) for x in b[:6]],
                    _event_ts(b[6]),
                    b[7] or None,
                    b[8] or None,
                    _nullable_number(b[9]),
                    _nullable_number(b[10]),
                    int(float(b[11] or 0)),
                    int(float(b[12] or 0)),
                ),
            )
        _mark_capability(con, "battle_outcomes", ts)
    except Exception:
        pass  # battle_reports 스키마가 다르면 스냅샷만 수집

    # 실제 리사이클러/리퍼 회수 수입. battle_reports의 생성 데브리가 아니라 플레이어에게
    # 전달된 debris_field_harvest 메시지를 적재해 "생성량"을 "수입"으로 세는 오류를 피한다.
    try:
        hrows = game_sql("""
            SELECT m.id, UNIX_TIMESTAMP(m.created_at), COALESCE(u.email,''),
                   JSON_UNQUOTE(JSON_EXTRACT(m.params,'$.harvested_metal')),
                   JSON_UNQUOTE(JSON_EXTRACT(m.params,'$.harvested_crystal')),
                   JSON_UNQUOTE(JSON_EXTRACT(m.params,'$.harvested_deuterium'))
            FROM messages m
            LEFT JOIN users u ON u.id = m.user_id
            WHERE m.key = 'debris_field_harvest'""")
        for h in hrows:
            con.execute(
                """INSERT INTO debris_harvests (
                       message_id, event_ts, email, metal, crystal, deuterium
                   ) VALUES (?,?,?,?,?,?)
                   ON CONFLICT(message_id) DO UPDATE SET
                       event_ts=excluded.event_ts, email=excluded.email,
                       metal=excluded.metal, crystal=excluded.crystal,
                       deuterium=excluded.deuterium""",
                (
                    int(float(h[0])),
                    _event_ts(h[1]),
                    h[2] or None,
                    _nullable_number(h[3], integer=True),
                    _nullable_number(h[4], integer=True),
                    _nullable_number(h[5], integer=True),
                ),
            )
        _mark_capability(con, "debris_harvests", ts)
    except Exception:
        pass  # messages 스키마/JSON이 다르면 데브리 수입만 unavailable로 남긴다
    # 보존 정책: 스냅샷 30일 초과분 삭제 (디스크 방어)
    con.execute("DELETE FROM snapshots WHERE ts < datetime('now','-30 days')")
    con.commit()
    detect_stall(con)


def detect_stall(con: sqlite3.Connection) -> None:
    """정체 감지 → stall_events 에 기록만 한다 (알림 없음 — 사후 추적용).

    판정: 평균 건물합이 1시간 전과 동일(성장 0) AND 최근 1h 건설 취소율 > 50%.
    30분 dedupe (연속 감지 시 30분에 1행만).
    """
    try:
        last = con.execute(
            "SELECT ts FROM stall_events ORDER BY ts DESC LIMIT 1").fetchone()
        if last:
            recent = con.execute(
                "SELECT 1 FROM stall_events WHERE ts > datetime('now','-30 minutes')").fetchone()
            if recent:
                return
        now_sum = con.execute(
            "SELECT AVG(building_sum) FROM snapshots WHERE ts = (SELECT MAX(ts) FROM snapshots)"
        ).fetchone()[0]
        old_sum = con.execute(
            """SELECT AVG(building_sum) FROM snapshots WHERE ts =
               (SELECT MAX(ts) FROM snapshots WHERE ts < datetime('now','-55 minutes'))"""
        ).fetchone()[0]
        if now_sum is None or old_sum is None or now_sum != old_sum:
            return
        row = game_sql(
            "SELECT COALESCE(ROUND(SUM(canceled)/NULLIF(COUNT(*),0)*100,1),0) "
            "FROM building_queues WHERE created_at > NOW() - INTERVAL 1 HOUR")
        cancel_rate = float(row[0][0]) if row else 0.0
        if cancel_rate > 50:
            ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            con.execute(
                "INSERT INTO stall_events VALUES (?,?,?,?)",
                (ts, cancel_rate, now_sum,
                 f"건물합 {now_sum} 1시간 정지 + 취소율 {cancel_rate}%"),
            )
            con.commit()
    except Exception:
        pass  # 감지 실패는 수집을 막지 않는다


def rotate_logs() -> None:
    for log in LOGS_TO_ROTATE:
        try:
            if log.exists() and log.stat().st_size > LOG_ROTATE_BYTES:
                bak = log.with_suffix(log.suffix + ".1")
                if bak.exists():
                    bak.unlink()
                log.rename(bak)
        except OSError:
            pass


def notify_battles_safely(con: sqlite3.Connection):
    """Discord 알림 실패를 수집 루프와 완전히 격리한다.

    반환값은 테스트/수동 진단용이며 main은 출력하지 않는다. 네트워크 예외에 webhook URL이
    들어 있을 수 있으므로 여기서도 예외 문자열을 로그로 흘리지 않는다.
    """
    try:
        return battle_notifier.notify_new_battles(con)
    except Exception:  # noqa: BLE001 - 알림은 metrics 수집을 절대 중단시키면 안 됨
        return None


def backfill_snapshot_personalities(con: sqlite3.Connection) -> int:
    """NULL personality를 결정적 이메일 배정으로 복구한다(명시 호출 전용)."""
    if assign_personality is None:
        raise RuntimeError("personality assignment import unavailable")
    rows = con.execute(
        "SELECT DISTINCT email FROM snapshots WHERE personality IS NULL "
        "AND (email LIKE 'bot%@bots.local' OR email IN "
        "('pirate01@bots.local','explorer01@bots.local'))"
    ).fetchall()
    changed = 0
    for (email,) in rows:
        personality = assign_personality(str(email))
        cur = con.execute(
            "UPDATE snapshots SET personality=? WHERE email=? AND personality IS NULL",
            (personality, email),
        )
        changed += int(cur.rowcount or 0)
    con.commit()
    return changed


def main() -> None:
    con = sqlite3.connect(DB_PATH)
    init_db(con)
    if "--backfill-personalities" in sys.argv[1:]:
        print(f"personality backfill: {backfill_snapshot_personalities(con)} rows")
        return
    print(
        f"metrics collector 시작: 수집 {INTERVAL_SEC}s / "
        f"Discord 전투 알림 {BATTLE_NOTIFICATION_INTERVAL_SEC}s 간격 → {DB_PATH}"
    )
    next_notification_at = 0.0
    while True:
        try:
            collect(con)
        except Exception as e:
            print(f"수집 실패 (다음 주기 재시도): {e}")
        # 수집 성공 뒤 실행하되 위 try 밖에 둔다. 따라서 수집 실패 때도 이미 적재된 미전송
        # batch를 재시도하고, 반대로 Discord 실패는 다음 수집 주기를 막지 않는다.
        now = time.monotonic()
        if now >= next_notification_at:
            notify_battles_safely(con)
            next_notification_at = time.monotonic() + BATTLE_NOTIFICATION_INTERVAL_SEC
        rotate_logs()
        time.sleep(INTERVAL_SEC)


if __name__ == "__main__":
    main()
