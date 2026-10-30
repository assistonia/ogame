#!/usr/bin/env python3
"""metrics.db의 신규 전투를 Discord webhook으로 알린다.

안전 성질:

* 프로젝트 루트 ``.battle_notifications_enabled`` marker가 없으면 완전 비활성이다.
* cursor가 없는 첫 활성은 현재 MAX(battle_id)를 baseline으로만 저장한다.
* 공격측 함선이 정찰기뿐인 battle report는 cursor만 넘기고 Discord에는 보내지 않는다.
* 전투를 파괴량 기준 상위 5건만 골라 요약하고, Discord 2,000자 제한을 넘으면 줄 단위로 나눈다.
* 모든 조각 전송 성공 뒤에만 cursor를 진전시켜, 실패 시 다음 주기에 재시도한다.
* webhook URL은 반환값/메시지/로그에 절대 포함하지 않는다.
* sender를 주입할 수 있어 테스트에서 네트워크를 전혀 사용하지 않는다.
"""
from __future__ import annotations

import json
import os
import pathlib
import sqlite3
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Mapping, Sequence
from zoneinfo import ZoneInfo


PROJECT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_MARKER = PROJECT / ".battle_notifications_enabled"
DEFAULT_WEBHOOK_FILE = PROJECT / ".discord_webhook_url"
DEFAULT_CURSOR_FILE = PROJECT / "scripts" / ".battle_notifications_cursor"
DISCORD_MAX_CHARS = 2000
HTTP_TIMEOUT_SECONDS = 10
DEFAULT_TOP_BATTLES = 5
KST = ZoneInfo("Asia/Seoul")

Sender = Callable[[str, str], None]


@dataclass(frozen=True)
class NotificationResult:
    status: str
    pending: int = 0
    sent: int = 0
    cursor: int | None = None
    reason: str | None = None


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in con.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _atomic_cursor(path: pathlib.Path, value: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(f"{int(value)}\n", encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)


def _read_cursor(path: pathlib.Path) -> tuple[int | None, str]:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None, "missing"
    except OSError:
        return None, "unreadable"
    try:
        value = int(raw)
    except ValueError:
        return None, "invalid"
    if value < 0:
        return None, "invalid"
    return value, "ok"


def _read_webhook(path: pathlib.Path) -> str | None:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    parsed = urllib.parse.urlparse(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "discord.com"
        or not parsed.path.startswith("/api/webhooks/")
        or parsed.username
        or parsed.password
    ):
        return None
    return value


def discord_sender(webhook_url: str, content: str) -> None:
    """Discord JSON POST. 호출자는 모든 예외를 URL 비노출 상태로 처리한다."""
    if len(content) > DISCORD_MAX_CHARS:
        raise ValueError("discord-message-too-long")
    body = json.dumps(
        {"content": content, "allowed_mentions": {"parse": []}},
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        webhook_url,
        data=body,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "ogame-battle-notifier/1",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
        status = int(getattr(response, "status", response.getcode()))
        if not 200 <= status < 300:
            raise RuntimeError("discord-non-2xx")


def _compact_number(value: object) -> str:
    if value is None:
        return "미수집"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "미수집"
    sign = "-" if number < 0 else ""
    number = abs(number)
    for threshold, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "k")):
        if number >= threshold:
            shown = number / threshold
            text = f"{shown:.1f}".rstrip("0").rstrip(".")
            return f"{sign}{text}{suffix}"
    return f"{sign}{number:.0f}"


def _float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _battle_id(row: Mapping[str, object]) -> int:
    raw = row.get("battle_id")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _actor(row: Mapping[str, object], side: str) -> str:
    email = row.get(f"{side}_email")
    if isinstance(email, str) and email.strip():
        # Discord 폭과 이메일 도메인 노출을 줄인다.
        return email.split("@", 1)[0][:28]
    player_id = row.get(f"{side}_id")
    return f"id:{int(player_id)}" if isinstance(player_id, (int, float)) else "미상"


def _battle_destruction(row: Mapping[str, object]) -> float | None:
    attacker_loss = _float(row.get("attacker_loss"))
    defender_loss = _float(row.get("defender_loss"))
    if attacker_loss is None or defender_loss is None:
        return None
    return attacker_loss + defender_loss


def _complete_sum(rows: Sequence[Mapping[str, object]], fields: Sequence[str]) -> float | None:
    """하나라도 수집되지 않은 값이 있으면 합계를 0으로 위장하지 않는다."""
    total = 0.0
    for row in rows:
        for field in fields:
            value = row.get(field)
            if value is None:
                return None
            try:
                total += float(value)
            except (TypeError, ValueError):
                return None
    return total


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _bounds(rows: Sequence[Mapping[str, object]]) -> tuple[datetime, datetime] | None:
    starts = [_parse_timestamp(row.get("battle_ts") or row.get("ts")) for row in rows]
    ends = [_parse_timestamp(row.get("ts") or row.get("battle_ts")) for row in rows]
    starts = [value for value in starts if value]
    ends = [value for value in ends if value]
    if not starts or not ends:
        return None
    return min(starts), max(ends)


def _period(rows: Sequence[Mapping[str, object]]) -> str:
    bounds = _bounds(rows)
    if bounds is None:
        return "미수집"
    start, end = (value.astimezone(KST) for value in bounds)
    start_text = start.strftime("%m-%d %H:%M")
    end_text = end.strftime("%H:%M") if start.date() == end.date() else end.strftime("%m-%d %H:%M")
    return f"{start_text}~{end_text} KST"


def _battle_time(row: Mapping[str, object]) -> str:
    parsed = _parse_timestamp(row.get("battle_ts") or row.get("ts"))
    return parsed.astimezone(KST).strftime("%m-%d %H:%M") if parsed else "시간미상"


def format_battle_line(row: Mapping[str, object]) -> str:
    """전투 하나를 짧은 Discord 한 줄로 만든다."""
    loot = _complete_sum(
        [row], ("loot_metal", "loot_crystal", "loot_deuterium")
    )
    attacker_loss = _complete_sum([row], ("attacker_loss",))
    defender_loss = _complete_sum([row], ("defender_loss",))
    battle_id = row.get("battle_id")
    try:
        battle_label = f"#{int(battle_id)}"
    except (TypeError, ValueError):
        battle_label = "#?"
    admin = " | 관리자테스트" if int(row.get("is_admin_test") or 0) else ""
    return (
        f"⚔️ {_battle_time(row)} {battle_label} "
        f"{_actor(row, 'attacker')} → {_actor(row, 'defender')}"
        f" | 약탈 {_compact_number(loot)}"
        f" | 손실 공 {_compact_number(attacker_loss)} / 방 {_compact_number(defender_loss)}"
        f"{admin}"
    )


def _battle_chunks(
    rows: Sequence[Mapping[str, object]],
) -> list[tuple[str, int]]:
    """Discord 길이 제한 안에서 전투 한 줄을 쏼개지 않고 묶는다."""
    chunks: list[tuple[str, int]] = []
    lines: list[str] = []
    for row in rows:
        line = format_battle_line(row)[:DISCORD_MAX_CHARS]
        candidate = "\n".join([*lines, line])
        if lines and len(candidate) > DISCORD_MAX_CHARS:
            chunks.append(("\n".join(lines), len(lines)))
            lines = [line]
        else:
            lines.append(line)
    if lines:
        chunks.append(("\n".join(lines), len(lines)))
    return chunks


def _chunk_lines(lines: Sequence[str]) -> list[tuple[str, int]]:
    """Discord 길이 제한 안에서 텍스트 라인을 묶는다."""
    chunks: list[tuple[str, int]] = []
    batch: list[str] = []
    for line in lines:
        candidate = "\n".join([*batch, line])
        if batch and len(candidate) > DISCORD_MAX_CHARS:
            chunks.append(("\n".join(batch), len(batch)))
            batch = [line]
        else:
            batch.append(line)
    if batch:
        chunks.append(("\n".join(batch), len(batch)))
    return chunks


def _top_battles(
    rows: Sequence[Mapping[str, object]],
    limit: int,
) -> list[tuple[float, Mapping[str, object]]]:
    """파괴량 기준 내림차순으로 상위 N개를 뽑는다."""
    if limit <= 0:
        return []
    scored: list[tuple[float, int, Mapping[str, object]]] = []
    for row in rows:
        score = _battle_destruction(row)
        if score is None:
            score = _complete_sum(
                [row], ("loot_metal", "loot_crystal", "loot_deuterium")
            )
        if score is None:
            score = 0.0
        scored.append((float(score), _battle_id(row), row))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [(score, row) for score, _id, row in scored[:limit]]


def _top_actor(rows: Sequence[Mapping[str, object]], side: str) -> tuple[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        actor = _actor(row, side)
        counts[actor] = counts.get(actor, 0) + 1
    if not counts:
        return "미상", 0
    # 동률은 이름순으로 고정해 재현 가능한 요약을 만든다.
    return min(counts.items(), key=lambda item: (-item[1], item[0]))


def _anomaly_text(rows: Sequence[Mapping[str, object]]) -> str:
    loss_making = 0
    zero_loot = 0
    admin_tests = 0
    for row in rows:
        loot_values = (
            row.get("loot_metal"),
            row.get("loot_crystal"),
            row.get("loot_deuterium"),
        )
        loot = None if any(value is None for value in loot_values) else sum(
            float(value) for value in loot_values
        )
        attacker_loss = row.get("attacker_loss")
        if loot == 0:
            zero_loot += 1
        if loot is not None and attacker_loss is not None and float(attacker_loss) > loot:
            loss_making += 1
        if int(row.get("is_admin_test") or 0):
            admin_tests += 1

    defender, defender_count = _top_actor(rows, "defender")
    warnings: list[str] = []
    if loss_making:
        warnings.append(f"손해공격 {loss_making}건")
    if zero_loot:
        warnings.append(f"무약탈 {zero_loot}건")
    if defender_count >= 3:
        warnings.append(f"집중피격 {defender} {defender_count}회")
    if admin_tests:
        warnings.append(f"관리자테스트 {admin_tests}건")
    return " · ".join(warnings) if warnings else "특이사항 없음"


def format_summary(
    rows: Sequence[Mapping[str, object]],
    *,
    debris_harvested: float | None,
    top_n: int = DEFAULT_TOP_BATTLES,
) -> str:
    """파괴량 TOP 기반으로 전투를 요약한다."""
    loot = _complete_sum(rows, ("loot_metal", "loot_crystal", "loot_deuterium"))
    attacker_loss = _complete_sum(rows, ("attacker_loss",))
    defender_loss = _complete_sum(rows, ("defender_loss",))
    attacker, attack_count = _top_actor(rows, "attacker")
    defender, defense_count = _top_actor(rows, "defender")
    top = _top_battles(rows, top_n)
    shown_count = min(top_n, len(rows))
    lines = [
        f"⚔️ OGame 전투 파괴 TOP {shown_count}",
        f"기간 {_period(rows)} | 전투 {len(rows)}건 (표시 {shown_count}건)",
        f"약탈 {_compact_number(loot)} | 손실 공격 {_compact_number(attacker_loss)}"
        f" / 방어 {_compact_number(defender_loss)} | 데브리 회수 {_compact_number(debris_harvested)}",
        f"최다 공격 {attacker} {attack_count}회 | 최다 피격 {defender} {defense_count}회",
    ]
    for index, (score, row) in enumerate(top, 1):
        lines.append(
            f"{index:02d}. {format_battle_line(row)} | 파괴 {_compact_number(score)}"
        )
    lines.append(f"주의 {_anomaly_text(rows)}")
    return "\n".join(lines)[:DISCORD_MAX_CHARS]


def _pending_rows(
    con: sqlite3.Connection,
    cursor: int,
) -> tuple[int, list[dict[str, object]]]:
    # NULL은 구 DB/원본 조회 실패로 분류가 아직 안 된 값이다. 실제 전투를 누락하지 않도록
    # 알림 대상으로 남기고, 명시적으로 1인 probe-only만 제외한다.
    total = int(con.execute(
        """SELECT COUNT(*) FROM battles
           WHERE battle_id>? AND COALESCE(is_probe_only,0)=0""", (cursor,)
    ).fetchone()[0])
    admin_test_expr = (
        "COALESCE(is_admin_test,0)" if "is_admin_test" in _columns(con, "battles") else "0"
    )
    result = con.execute(
        f"""SELECT ts, battle_id, attacker_id, defender_id,
                  attacker_email, defender_email,
                  loot_metal, loot_crystal, loot_deuterium,
                  battle_ts, attacker_loss, defender_loss,
                  {admin_test_expr} AS is_admin_test
           FROM battles
           WHERE battle_id>? AND COALESCE(is_probe_only,0)=0
           ORDER BY battle_id""",
        (cursor,),
    )
    names = [str(item[0]) for item in result.description]
    rows = [dict(zip(names, row)) for row in result.fetchall()]
    return total, rows


def _debris_harvested(
    con: sqlite3.Connection,
    rows: Sequence[Mapping[str, object]],
) -> float | None:
    """요약 구간의 실제 회수량. 수집 가능성이 없으면 0이 아닌 None이다."""
    if not {"event_ts", "metal", "crystal", "deuterium"}.issubset(
        _columns(con, "debris_harvests")
    ):
        return None
    capability_columns = _columns(con, "metric_capabilities")
    if {"metric", "last_success_ts"}.issubset(capability_columns):
        known = con.execute(
            "SELECT 1 FROM metric_capabilities WHERE metric='debris_harvests'"
        ).fetchone()
        if not known:
            return None

    bounds = _bounds(rows)
    if bounds is None:
        return None
    start, end = bounds
    start_text = start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_text = end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        values = con.execute(
            """SELECT metal, crystal, deuterium FROM debris_harvests
               WHERE event_ts>=? AND event_ts<=?""",
            (start_text, end_text),
        ).fetchall()
    except sqlite3.Error:
        return None
    if any(value is None for row in values for value in row):
        return None
    return sum(float(value) for row in values for value in row)


def notify_new_battles(
    con: sqlite3.Connection,
    *,
    marker_path: pathlib.Path = DEFAULT_MARKER,
    webhook_path: pathlib.Path = DEFAULT_WEBHOOK_FILE,
    cursor_path: pathlib.Path = DEFAULT_CURSOR_FILE,
    sender: Sender = discord_sender,
    max_items: int | None = None,
) -> NotificationResult:
    """상위 5건 전투를 요약 전송한다. 실패는 예외/secret을 노출하지 않는다."""
    max_items = DEFAULT_TOP_BATTLES if max_items is None else max_items
    if not marker_path.is_file():
        return NotificationResult("disabled", reason="marker-off")

    required = {
        "ts", "battle_id", "attacker_id", "defender_id",
        "attacker_email", "defender_email",
        "loot_metal", "loot_crystal", "loot_deuterium", "battle_ts",
        "attacker_loss", "defender_loss", "is_probe_only",
    }
    if not required.issubset(_columns(con, "battles")):
        return NotificationResult("unavailable", reason="battle-schema-not-ready")

    max_row = con.execute("SELECT COALESCE(MAX(battle_id),0) FROM battles").fetchone()
    current_max = int(max_row[0] or 0)
    cursor, cursor_state = _read_cursor(cursor_path)
    if cursor_state in {"missing", "invalid"}:
        # 첫 활성/손상 복구는 과거 전체 발송보다 현재를 baseline으로 잡는 쪽으로 fail-safe.
        try:
            _atomic_cursor(cursor_path, current_max)
        except OSError:
            return NotificationResult("unavailable", reason="cursor-write-failed")
        status = "baselined" if cursor_state == "missing" else "rebaselined"
        return NotificationResult(status, cursor=current_max, reason=cursor_state)
    if cursor_state != "ok" or cursor is None:
        return NotificationResult("unavailable", reason="cursor-read-failed")
    if cursor > current_max:
        # DB restore/reset으로 ID가 뒤로 간 경우에도 영구 정지하지 않고 안전 baseline.
        try:
            _atomic_cursor(cursor_path, current_max)
        except OSError:
            return NotificationResult("unavailable", cursor=cursor, reason="cursor-write-failed")
        return NotificationResult("rebaselined", cursor=current_max, reason="database-id-reset")

    try:
        total, rows = _pending_rows(con, cursor)
    except sqlite3.Error:
        return NotificationResult("unavailable", cursor=cursor, reason="battle-read-failed")
    if not rows:
        # cursor 뒤에 probe-only 행만 있으면 현재 MAX까지 소비해 매 주기 재검색하지 않는다.
        if current_max > cursor:
            try:
                _atomic_cursor(cursor_path, current_max)
            except OSError:
                return NotificationResult(
                    "unavailable", cursor=cursor, reason="cursor-write-failed"
                )
            return NotificationResult("filtered", cursor=current_max, reason="probe-only")
        return NotificationResult("idle", cursor=cursor)

    webhook_url = _read_webhook(webhook_path)
    if webhook_url is None:
        return NotificationResult(
            "unavailable", pending=total, cursor=cursor, reason="webhook-unavailable"
        )
    display_count = min(max_items, len(rows))
    summary = format_summary(
        rows,
        debris_harvested=_debris_harvested(con, rows),
        top_n=max_items,
    )
    sent_rows = 0
    for content, _row_count in _chunk_lines(summary.splitlines()):
        try:
            sender(webhook_url, content)
        except Exception:  # noqa: BLE001 - URL이 든 네트워크 예외 문자열은 절대 반환/로그하지 않는다.
            return NotificationResult(
                "retry", pending=total, sent=sent_rows, cursor=cursor,
                reason="send-failed",
            )
    sent_rows = display_count

    # pending 전체를 전송했으므로 뒤의 probe-only ID까지 함께 소비한다.
    next_cursor = current_max
    try:
        _atomic_cursor(cursor_path, next_cursor)
    except OSError:
        # POST 성공 후 cursor 쓰기 실패는 다음 주기에 중복될 수 있지만 누락보다 안전하다.
        return NotificationResult(
            "retry", pending=total, sent=sent_rows, cursor=cursor,
            reason="cursor-write-after-send-failed",
        )
    return NotificationResult("sent", pending=total, sent=sent_rows, cursor=next_cursor)
