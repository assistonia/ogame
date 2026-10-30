#!/usr/bin/env python3
"""Week 4 AI 대 동일 클래스 규칙봇 평가 리포트 생성기.

입력 DB에는 SELECT만 수행한다. metrics SQLite는 URI mode=ro, 선택적 MariaDB는
READ ONLY 트랜잭션으로 연다. 출력은 동일한 내용을 담은 한국어 Markdown과 JSON이다.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import hashlib
import json
import math
import os
import pathlib
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from typing import Any, Iterable


ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_METRICS_DB = ROOT / "scripts" / "metrics.db"
DEFAULT_POLICY_DIR = ROOT / "bots" / "ai_agents" / "policies"

UTC = dt.timezone.utc
CLASS_NAMES = {1: "COLLECTOR", 2: "GENERAL", 3: "DISCOVERER"}
CLASS_IDS = {name: number for number, name in CLASS_NAMES.items()}
PERSONALITY_CLASS = {
    "miner": 1,
    "turtle": 1,
    "raider": 2,
    "hybrid": 2,
    "explorer": 3,
    "explorer_aggressive": 3,
}

METRIC_SPECS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("total_score_delta", ("growth", "total_score_delta"), "score"),
    ("planet_delta", ("growth", "colony", "planet_count_delta"), "count"),
    ("building_delta", ("growth", "economy", "building_level_sum_delta"), "level_sum"),
    ("facility_delta", ("growth", "economy", "facility_level_sum_delta"), "level_sum"),
    ("ship_delta", ("growth", "military", "ship_count_delta"), "count_proxy"),
    ("defense_delta", ("growth", "military", "defense_count_delta"), "count_proxy"),
    ("attacks", ("combat", "outgoing_combat_attacks"), "count"),
    ("loot", ("combat", "outgoing_loot"), "resource_equivalent"),
    ("attacker_loss", ("combat", "outgoing_attacker_loss"), "resource_equivalent"),
    ("scout_conversion_pct", ("scouting", "scout_to_launched_rate_percent"), "percent"),
)

NO_RESOURCE_EXPEDITION_HINTS = (
    "nothing",
    "empty",
    "delay",
    "speed",
    "early",
    "merchant",
    "pirate",
    "alien",
    "black_hole",
    "lost",
    "dark_matter",
)
RESOURCE_EXPEDITION_HINTS = ("resource", "found", "gain", "reward")
RESOURCE_CONTAINER_KEYS = (
    "resources",
    "resource",
    "found_resources",
    "gained_resources",
    "reward",
    "expedition_resources",
)


def measured(value: Any, unit: str, method: str, **extra: Any) -> dict[str, Any]:
    return {"status": "measured", "value": value, "unit": unit, "method": method, **extra}


def derived(value: Any, unit: str, method: str, **extra: Any) -> dict[str, Any]:
    return {"status": "derived", "value": value, "unit": unit, "method": method, **extra}


def estimated(value: Any, unit: str, method: str, reason: str, **extra: Any) -> dict[str, Any]:
    return {
        "status": "estimated",
        "value": value,
        "unit": unit,
        "method": method,
        "reason": reason,
        **extra,
    }


def partial(value: Any, unit: str, method: str, reason: str, **extra: Any) -> dict[str, Any]:
    return {
        "status": "partial",
        "value": value,
        "unit": unit,
        "method": method,
        "reason": reason,
        **extra,
    }


def missing(unit: str, reason: str, method: str | None = None, **extra: Any) -> dict[str, Any]:
    result = {"status": "missing", "value": None, "unit": unit, "reason": reason, **extra}
    if method:
        result["method"] = method
    return result


def canonical_number(value: Any) -> int | float | None:
    if value is None:
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    if number.is_integer():
        return int(number)
    return round(number, 3)


def parse_utc(value: str, label: str) -> dt.datetime:
    text = value.strip()
    if len(text) == 10:
        text += "T00:00:00Z"
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{label}은 ISO-8601 시각이어야 합니다: {value}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label}에 timezone이 필요합니다. UTC에는 Z를 사용하십시오: {value}")
    return parsed.astimezone(UTC).replace(microsecond=0)


def db_datetime(value: Any) -> dt.datetime | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        parsed = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = dt.datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).replace(microsecond=0)


def iso(value: dt.datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def mysql_time(value: dt.datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def selectors(values: list[str] | None, default: str) -> list[str]:
    raw = values or [default]
    result: list[str] = []
    for value in raw:
        result.extend(part.strip() for part in value.split(",") if part.strip())
    return sorted(set(result))


def select_emails(candidates: Iterable[str], patterns: list[str]) -> tuple[list[str], list[str]]:
    available = sorted(set(candidates))
    matched = sorted(
        email for email in available if any(fnmatch.fnmatchcase(email, pattern) for pattern in patterns)
    )
    unmatched = sorted(pattern for pattern in patterns if not any(fnmatch.fnmatchcase(e, pattern) for e in available))
    return matched, unmatched


def sqlite_ro(path: pathlib.Path) -> sqlite3.Connection:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"metrics DB가 없습니다: {resolved}")
    con = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    con.execute("BEGIN")
    return con


def table_columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in con.execute(f"PRAGMA table_info({table})")}


def nested(record: dict[str, Any], path: tuple[str, ...]) -> Any:
    value: Any = record
    for part in path:
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def delta_metric(first: sqlite3.Row, last: sqlite3.Row, column: str, unit: str) -> dict[str, Any]:
    if first[column] is None or last[column] is None:
        return missing(unit, f"시작 또는 종료 snapshot의 {column}이 NULL")
    return measured(
        canonical_number(float(last[column]) - float(first[column])),
        unit,
        f"기간 내 마지막 snapshot.{column} - 첫 snapshot.{column}",
    )


def load_policy_identity(policy_dir: pathlib.Path, email: str) -> dict[str, Any]:
    localpart = email.split("@", 1)[0]
    path = policy_dir / f"{localpart}.json"
    if not path.is_file():
        return {
            "personality": None,
            "expedition_enabled": None,
            "source": "missing",
            "fingerprint": None,
        }
    try:
        raw = path.read_bytes()
        policy = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {
            "personality": None,
            "expedition_enabled": None,
            "source": "invalid",
            "fingerprint": None,
        }
    identity = policy.get("identity") if isinstance(policy.get("identity"), dict) else {}
    personality = policy.get("personality") or identity.get("personality")
    action_plan = policy.get("action_plan")
    expedition_enabled = None
    if isinstance(action_plan, list):
        expedition_enabled = "expedition" in action_plan
    return {
        "personality": str(personality) if personality else None,
        "expedition_enabled": expedition_enabled,
        "source": str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path),
        "fingerprint": hashlib.sha256(raw).hexdigest(),
    }


def normalize_class(value: Any) -> tuple[int | None, str | None]:
    if value is None:
        return None, None
    if isinstance(value, str):
        text = value.strip().upper()
        if text.isdigit():
            number = int(text)
            return (number, CLASS_NAMES.get(number))
        aliases = {
            "COLLECTOR": "COLLECTOR",
            "GENERAL": "GENERAL",
            "DISCOVERER": "DISCOVERER",
            "DISCOVERER_CLASS": "DISCOVERER",
        }
        name = aliases.get(text)
        return (CLASS_IDS.get(name), name) if name else (None, None)
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None, None
    return number, CLASS_NAMES.get(number)


def game_connection(args: argparse.Namespace):
    try:
        import pymysql
        import pymysql.cursors
    except ImportError as exc:
        raise RuntimeError("게임 DB 입력에는 pymysql 패키지가 필요합니다") from exc

    password = os.environ.get(args.game_db_password_env)
    if password is None:
        raise RuntimeError(f"환경변수 {args.game_db_password_env}가 설정되지 않았습니다")
    con = pymysql.connect(
        host=args.game_db_host,
        port=args.game_db_port,
        user=args.game_db_user,
        password=password,
        database=args.game_db_name,
        charset="utf8mb4",
        autocommit=False,
        connect_timeout=10,
        read_timeout=60,
        write_timeout=10,
        cursorclass=pymysql.cursors.DictCursor,
    )
    with con.cursor() as cur:
        cur.execute("SET time_zone = '+00:00'")
        cur.execute("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        cur.execute("START TRANSACTION READ ONLY")
    return con


def safe_db_error(exc: Exception) -> str:
    return " ".join(str(exc).split())[:240]


def load_game_data(
    args: argparse.Namespace,
    emails: list[str],
    start: dt.datetime,
    end: dt.datetime,
) -> dict[str, Any]:
    blank = {
        "enabled": False,
        "classes": {},
        "scouts": [],
        "expeditions": [],
        "capabilities": {
            "account_classes": missing("capability", "--game-db-host가 지정되지 않음"),
            "successful_scout_reports": missing("capability", "--game-db-host가 지정되지 않음"),
            "expedition_messages": missing("capability", "--game-db-host가 지정되지 않음"),
        },
    }
    if not args.game_db_host:
        return blank

    con = game_connection(args)
    result = {"enabled": True, "classes": {}, "scouts": [], "expeditions": [], "capabilities": {}}
    marks = ",".join(["%s"] * len(emails))
    try:
        try:
            with con.cursor() as cur:
                cur.execute(
                    f"SELECT email, character_class FROM users WHERE email IN ({marks})",
                    emails,
                )
                for row in cur.fetchall():
                    result["classes"][str(row["email"])] = row.get("character_class")
            result["capabilities"]["account_classes"] = measured(
                len(result["classes"]), "accounts", "users.character_class 현재값 SELECT"
            )
        except Exception as exc:  # noqa: BLE001 - capability별 결측을 보존한다.
            result["capabilities"]["account_classes"] = missing(
                "capability", safe_db_error(exc), method="users.character_class SELECT"
            )

        try:
            with con.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT m.id, observer.email observer_email,
                           target.email target_email,
                           er.planet_galaxy, er.planet_system, er.planet_position,
                           er.planet_type, m.created_at
                    FROM messages m
                    JOIN users observer ON observer.id=m.user_id
                    JOIN espionage_reports er ON er.id=m.espionage_report_id
                    LEFT JOIN users target ON target.id=er.planet_user_id
                    WHERE observer.email IN ({marks})
                      AND m.created_at >= %s AND m.created_at < %s
                    ORDER BY m.created_at, m.id
                    """,
                    [*emails, mysql_time(start), mysql_time(end)],
                )
                result["scouts"] = [dict(row) for row in cur.fetchall()]
            result["capabilities"]["successful_scout_reports"] = measured(
                len(result["scouts"]),
                "reports",
                "messages.espionage_report_id와 espionage_reports 조인",
            )
        except Exception as exc:  # noqa: BLE001
            result["capabilities"]["successful_scout_reports"] = missing(
                "capability", safe_db_error(exc), method="espionage report SELECT"
            )

        try:
            with con.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT m.id, u.email, m.`key`, m.params, m.created_at
                    FROM messages m
                    JOIN users u ON u.id=m.user_id
                    WHERE u.email IN ({marks})
                      AND m.created_at >= %s AND m.created_at < %s
                      AND LOWER(m.`key`) LIKE '%%expedition%%'
                    ORDER BY m.created_at, m.id
                    """,
                    [*emails, mysql_time(start), mysql_time(end)],
                )
                result["expeditions"] = [dict(row) for row in cur.fetchall()]
            result["capabilities"]["expedition_messages"] = measured(
                len(result["expeditions"]),
                "messages",
                "messages.key LIKE '%expedition%'",
            )
        except Exception as exc:  # noqa: BLE001
            result["capabilities"]["expedition_messages"] = missing(
                "capability", safe_db_error(exc), method="expedition message SELECT"
            )
    finally:
        con.rollback()
        con.close()
    return result


def build_identities(
    snapshots: dict[str, list[sqlite3.Row]],
    ai_emails: set[str],
    game_data: dict[str, Any],
    policy_dir: pathlib.Path,
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    identities: dict[str, dict[str, Any]] = {}
    policy_fingerprints: dict[str, str] = {}
    class_capability = game_data["capabilities"]["account_classes"]["status"] == "measured"
    for email, rows in sorted(snapshots.items()):
        cohort = "ai" if email in ai_emails else "rule"
        snapshot_personalities = [str(row["personality"]) for row in rows if row["personality"]]
        policy = load_policy_identity(policy_dir, email) if cohort == "ai" else None
        if policy and policy["fingerprint"]:
            policy_fingerprints[email] = policy["fingerprint"]
        personality = policy["personality"] if policy else None
        personality_source = policy["source"] if policy else "snapshots.personality"
        if not personality and snapshot_personalities:
            personality = Counter(snapshot_personalities).most_common(1)[0][0]
            personality_source = "snapshots.personality"

        current_class = game_data["classes"].get(email) if class_capability else None
        class_id, class_name = normalize_class(current_class)
        if class_name:
            class_info = {
                "id": class_id,
                "name": class_name,
                "status": "measured",
                "source": "game_db.users.character_class 현재값",
                "period_history_available": False,
            }
        else:
            inferred = PERSONALITY_CLASS.get(str(personality))
            class_info = {
                "id": inferred,
                "name": CLASS_NAMES.get(inferred),
                "status": "estimated" if inferred else "missing",
                "source": "personality -> class 배정 규칙" if inferred else "none",
                "reason": (
                    "기간별 class 이력이 없어 personality 매핑으로 추정"
                    if inferred
                    else "현재 class와 추정 가능한 personality가 없음"
                ),
                "period_history_available": False,
            }

        if policy and policy["expedition_enabled"] is not None:
            expedition_enabled = policy["expedition_enabled"]
            expedition_source = policy["source"]
        else:
            expedition_enabled = personality in {"explorer", "explorer_aggressive"}
            expedition_source = "personality behavior rule"

        identities[email] = {
            "email": email,
            "cohort": cohort,
            "personality": {
                "value": personality,
                "source": personality_source,
                "status": "measured" if personality else "missing",
            },
            "character_class": class_info,
            "expedition_enabled": {
                "value": expedition_enabled,
                "source": expedition_source,
                "status": "measured" if expedition_enabled is not None else "missing",
            },
        }
    return identities, policy_fingerprints


def build_growth(rows: list[sqlite3.Row], start: dt.datetime, end: dt.datetime) -> dict[str, Any]:
    first = rows[0]
    last = rows[-1]
    first_at = db_datetime(first["ts"])
    last_at = db_datetime(last["ts"])
    requested_hours = (end - start).total_seconds() / 3600.0
    observed_hours = (
        (last_at - first_at).total_seconds() / 3600.0 if first_at and last_at else 0.0
    )
    coverage = observed_hours / requested_hours if requested_hours > 0 else 0.0
    observation_status = "measured" if coverage >= 0.9 else "partial"

    resource_values = ("metal", "crystal", "deuterium")
    if all(first[name] is not None and last[name] is not None for name in resource_values):
        resource_delta = measured(
            canonical_number(
                sum(float(last[name]) - float(first[name]) for name in resource_values)
            ),
            "resource_equivalent",
            "기간 내 마지막-첫 snapshot의 metal+crystal+deuterium 재고 차이",
        )
    else:
        resource_delta = missing("resource_equivalent", "자원 snapshot 일부가 NULL")

    if len(rows) < 2:
        unavailable = missing("unknown", "기간 내 snapshot이 2개 미만")
        return {
            "observation": {
                "status": "missing",
                "snapshot_count": len(rows),
                "first_ts": str(first["ts"]),
                "last_ts": str(last["ts"]),
                "observed_hours": 0,
                "requested_hours": round(requested_hours, 3),
                "coverage_ratio": 0,
            },
            "total_score_delta": unavailable,
            "colony": {"planet_count_delta": unavailable},
            "economy": {
                "building_level_sum_delta": unavailable,
                "facility_level_sum_delta": unavailable,
                "resource_stock_delta": unavailable,
            },
            "military": {
                "ship_count_delta": unavailable,
                "defense_count_delta": unavailable,
                "interpretation": "함종/방어종 가치를 반영하지 않는 개수 proxy",
            },
        }

    return {
        "observation": {
            "status": observation_status,
            "snapshot_count": len(rows),
            "first_ts": str(first["ts"]),
            "last_ts": str(last["ts"]),
            "observed_hours": round(observed_hours, 3),
            "requested_hours": round(requested_hours, 3),
            "coverage_ratio": round(coverage, 4),
            "selection": "cutoff 안의 첫 snapshot과 마지막 snapshot; 보간 없음",
        },
        "total_score_delta": delta_metric(first, last, "score", "score"),
        "colony": {"planet_count_delta": delta_metric(first, last, "planets", "count")},
        "economy": {
            "building_level_sum_delta": delta_metric(first, last, "building_sum", "level_sum"),
            "facility_level_sum_delta": delta_metric(first, last, "facility_sum", "level_sum"),
            "resource_stock_delta": resource_delta,
        },
        "military": {
            "ship_count_delta": delta_metric(first, last, "ships_sum", "count_proxy"),
            "defense_count_delta": delta_metric(first, last, "defense_sum", "count_proxy"),
            "interpretation": "함종/방어종 가치를 반영하지 않는 개수 proxy; 두 값을 합산하지 않음",
        },
    }


def empty_combat(reason: str) -> dict[str, Any]:
    return {
        "outgoing_combat_attacks": missing("count", reason),
        "incoming_combat_attacks": missing("count", reason),
        "probe_only_battles": missing("count", reason),
        "outgoing_loot": missing("resource_equivalent", reason),
        "outgoing_attacker_loss": missing("resource_equivalent", reason),
        "outgoing_inflicted_loss": missing("resource_equivalent", reason),
        "incoming_defender_loss": missing("resource_equivalent", reason),
        "debris_harvest_income": missing("resource_equivalent", reason),
        "known_combat_net": missing("resource_equivalent", reason),
    }


def sum_metric(total: float, missing_rows: int, rows: int, label: str) -> dict[str, Any]:
    value = canonical_number(total)
    method = f"분류된 실전 {label} 행의 실제 기록 합"
    if missing_rows:
        return partial(
            value,
            "resource_equivalent",
            method,
            f"{missing_rows}개 행의 값이 NULL; 값은 known-only 합이며 0 대체하지 않음",
            complete_rows=rows - missing_rows,
            missing_rows=missing_rows,
        )
    return measured(value, "resource_equivalent", method, complete_rows=rows, missing_rows=0)


def collect_combat(
    con: sqlite3.Connection,
    emails: set[str],
    start: dt.datetime,
    end: dt.datetime,
) -> tuple[
    dict[str, dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any],
]:
    required = {
        "ts", "battle_id", "loot_metal", "loot_crystal", "loot_deuterium",
        "battle_ts", "attacker_email", "defender_email", "attacker_loss",
        "defender_loss", "is_probe_only", "is_admin_test",
    }
    columns = table_columns(con, "battles")
    if not required.issubset(columns):
        absent = sorted(required - columns)
        reason = f"battles 필수 컬럼 누락: {', '.join(absent)}"
        return (
            {email: empty_combat(reason) for email in emails},
            [],
            [],
            {"status": "missing", "reason": reason},
        )

    stats: dict[str, dict[str, Any]] = {
        email: defaultdict(float, {
            "outgoing": 0, "incoming": 0, "probe_only": 0,
            "loot": 0, "loot_missing": 0, "out_loss": 0, "out_loss_missing": 0,
            "inflicted": 0, "inflicted_missing": 0, "in_loss": 0, "in_loss_missing": 0,
            "unknown_admin": 0, "unknown_probe": 0,
        })
        for email in emails
    }
    events: list[dict[str, Any]] = []
    probe_events: list[dict[str, Any]] = []
    quality: Counter[str] = Counter()
    rows = con.execute(
        """
        SELECT ts,battle_id,loot_metal,loot_crystal,loot_deuterium,battle_ts,
               attacker_email,defender_email,attacker_loss,defender_loss,
               is_probe_only,is_admin_test
        FROM battles
        WHERE COALESCE(battle_ts,ts) >= ? AND COALESCE(battle_ts,ts) < ?
        ORDER BY COALESCE(battle_ts,ts), battle_id
        """,
        (iso(start), iso(end)),
    ).fetchall()
    for row in rows:
        attacker = str(row["attacker_email"] or "")
        defender = str(row["defender_email"] or "")
        involved = [email for email in {attacker, defender} if email in emails]
        if not involved:
            continue
        quality["in_scope_rows"] += 1
        if row["is_admin_test"] is None:
            quality["excluded_unknown_admin_classification"] += 1
            for email in involved:
                stats[email]["unknown_admin"] += 1
            continue
        if int(row["is_admin_test"]) == 1:
            quality["excluded_admin_test"] += 1
            continue
        if row["is_probe_only"] is None:
            quality["excluded_unknown_probe_classification"] += 1
            for email in involved:
                stats[email]["unknown_probe"] += 1
            continue
        if int(row["is_probe_only"]) == 1:
            quality["probe_only_battles"] += 1
            if attacker in emails:
                stats[attacker]["probe_only"] += 1
            probe_when = db_datetime(row["battle_ts"] or row["ts"])
            if probe_when is not None:
                probe_events.append({
                    "battle_id": row["battle_id"],
                    "ts": iso(probe_when),
                    "attacker_email": attacker or None,
                    "defender_email": defender or None,
                })
            else:
                quality["excluded_invalid_probe_timestamp"] += 1
            continue

        when = db_datetime(row["battle_ts"] or row["ts"])
        if when is None:
            quality["excluded_invalid_timestamp"] += 1
            continue
        if row["battle_ts"] is None:
            quality["collection_timestamp_fallback"] += 1
        quality["classified_combat_rows"] += 1
        events.append({
            "battle_id": row["battle_id"],
            "ts": iso(when),
            "attacker_email": attacker or None,
            "defender_email": defender or None,
        })
        if attacker in emails:
            account = stats[attacker]
            account["outgoing"] += 1
            loot_parts = (row["loot_metal"], row["loot_crystal"], row["loot_deuterium"])
            if any(value is None for value in loot_parts):
                account["loot_missing"] += 1
            else:
                account["loot"] += sum(float(value) for value in loot_parts)
            if row["attacker_loss"] is None:
                account["out_loss_missing"] += 1
            else:
                account["out_loss"] += float(row["attacker_loss"])
            if row["defender_loss"] is None:
                account["inflicted_missing"] += 1
            else:
                account["inflicted"] += float(row["defender_loss"])
        if defender in emails:
            account = stats[defender]
            account["incoming"] += 1
            if row["defender_loss"] is None:
                account["in_loss_missing"] += 1
            else:
                account["in_loss"] += float(row["defender_loss"])

    debris_capable = {
        "message_id", "event_ts", "email", "metal", "crystal", "deuterium"
    }.issubset(table_columns(con, "debris_harvests"))
    debris: dict[str, dict[str, float]] = {
        email: {"total": 0.0, "rows": 0, "missing": 0} for email in emails
    }
    if debris_capable:
        harvests = con.execute(
            """
            SELECT email,metal,crystal,deuterium FROM debris_harvests
            WHERE event_ts >= ? AND event_ts < ? ORDER BY event_ts,message_id
            """,
            (iso(start), iso(end)),
        ).fetchall()
        for row in harvests:
            email = str(row["email"] or "")
            if email not in emails:
                continue
            debris[email]["rows"] += 1
            parts = (row["metal"], row["crystal"], row["deuterium"])
            if any(value is None for value in parts):
                debris[email]["missing"] += 1
            else:
                debris[email]["total"] += sum(float(value) for value in parts)

    result: dict[str, dict[str, Any]] = {}
    for email in sorted(emails):
        account = stats[email]
        outgoing = int(account["outgoing"])
        incoming = int(account["incoming"])
        loot = sum_metric(account["loot"], int(account["loot_missing"]), outgoing, "공격 약탈")
        out_loss = sum_metric(
            account["out_loss"], int(account["out_loss_missing"]), outgoing, "공격측 손실"
        )
        inflicted = sum_metric(
            account["inflicted"], int(account["inflicted_missing"]), outgoing, "방어측 파괴 손실"
        )
        in_loss = sum_metric(
            account["in_loss"], int(account["in_loss_missing"]), incoming, "피격 방어측 손실"
        )
        if debris_capable:
            d = debris[email]
            debris_metric = sum_metric(d["total"], int(d["missing"]), int(d["rows"]), "데브리 회수")
        else:
            debris_metric = missing("resource_equivalent", "debris_harvests 스키마가 없음")

        net_inputs = (loot, out_loss, in_loss, debris_metric)
        if all(item["status"] == "measured" for item in net_inputs):
            net = derived(
                canonical_number(
                    float(loot["value"]) + float(debris_metric["value"])
                    - float(out_loss["value"]) - float(in_loss["value"])
                ),
                "resource_equivalent",
                "실제 약탈 + 실제 데브리 회수 - 공격측 손실 - 피격 방어측 손실",
            )
        else:
            net = missing(
                "resource_equivalent",
                "약탈/손실/데브리 중 결측 또는 partial이 있어 순값을 계산하지 않음",
            )
        result[email] = {
            "outgoing_combat_attacks": measured(
                outgoing, "count", "is_admin_test=0 AND is_probe_only=0인 공격측 battle 행"
            ),
            "incoming_combat_attacks": measured(
                incoming, "count", "is_admin_test=0 AND is_probe_only=0인 방어측 battle 행"
            ),
            "probe_only_battles": measured(
                int(account["probe_only"]), "count", "is_admin_test=0 AND is_probe_only=1"
            ),
            "outgoing_loot": loot,
            "outgoing_attacker_loss": out_loss,
            "outgoing_inflicted_loss": inflicted,
            "incoming_defender_loss": in_loss,
            "debris_harvest_income": debris_metric,
            "known_combat_net": net,
            "excluded_unclassified_rows": {
                "unknown_admin_flag": int(account["unknown_admin"]),
                "unknown_probe_flag": int(account["unknown_probe"]),
            },
        }
    return result, events, probe_events, {"status": "measured", **dict(sorted(quality.items()))}


def funnel_rate(numerator: int, denominator: int, label: str) -> dict[str, Any]:
    if denominator <= 0:
        return missing(
            "percent",
            f"{label} 분모가 0이어서 비율이 정의되지 않음",
            numerator=numerator,
            denominator=denominator,
        )
    extra: dict[str, Any] = {}
    if numerator > denominator:
        extra["quality_warning"] = "분자가 분모보다 큼: cutoff 경계 또는 stage 기록 모집단 확인 필요"
    return derived(
        round(numerator * 100.0 / denominator, 1),
        "percent",
        f"attack_funnel stage 행 기준 {label}",
        numerator=numerator,
        denominator=denominator,
        **extra,
    )


def collect_attack_funnel(
    con: sqlite3.Connection,
    emails: set[str],
    start: dt.datetime,
    end: dt.datetime,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    required = {
        "id", "ts", "email", "target", "stage", "reason", "expected_loot",
        "fleet_value", "fleet_value_limit", "metadata_json",
    }
    columns = table_columns(con, "attack_funnel")
    if not columns:
        return {}, missing("capability", "metrics.db에 attack_funnel 테이블이 없음")
    absent = sorted(required - columns)
    if absent:
        return {}, missing(
            "capability",
            f"attack_funnel 필수 컬럼 누락: {', '.join(absent)}",
        )

    rows = con.execute(
        """
        SELECT id,ts,email,target,stage,reason,expected_loot,
               fleet_value,fleet_value_limit,metadata_json
        FROM attack_funnel
        WHERE ts >= ? AND ts < ?
        ORDER BY email,ts,id
        """,
        (iso(start), iso(end)),
    ).fetchall()
    grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        email = str(row["email"] or "")
        if email in emails:
            grouped[email].append(row)

    output: dict[str, dict[str, Any]] = {}
    for email in sorted(emails):
        stage_counts: Counter[str] = Counter()
        reason_counts: Counter[str] = Counter()
        scout_count = 0
        preflight_count = 0
        launched_count = 0
        hold_count = 0
        unknown_count = 0
        for row in grouped[email]:
            raw_stage = str(row["stage"] or "").strip()
            normalized = raw_stage.lower().replace("-", "_").replace(" ", "_")
            stage_counts[raw_stage or "<missing>"] += 1
            is_scout = "scout" in normalized or "spy" in normalized
            is_preflight = "preflight" in normalized
            is_launched = "launch" in normalized
            is_hold = any(
                token in normalized
                for token in ("hold", "held", "skip", "block", "reject", "defer")
            )
            reason = str(row["reason"] or "").strip()
            if reason and not is_launched:
                is_hold = True
            scout_count += int(is_scout)
            preflight_count += int(is_preflight)
            launched_count += int(is_launched)
            hold_count += int(is_hold)
            if is_hold:
                reason_counts[reason or "<unspecified>"] += 1
            if not any((is_scout, is_preflight, is_launched, is_hold)):
                unknown_count += 1

        output[email] = {
            "source": {
                "status": "measured",
                "value": "metrics.attack_funnel",
                "method": "cutoff 내 raider terminal decision stage 행",
            },
            "scout_events": measured(
                scout_count, "count", "stage에 scout 또는 spy 토큰이 있는 attack_funnel 행"
            ),
            "preflight_events": measured(
                preflight_count, "count", "stage에 preflight 토큰이 있는 attack_funnel 행"
            ),
            "launched_events": measured(
                launched_count, "count", "stage에 launch 토큰이 있는 attack_funnel 행"
            ),
            "hold_events": measured(
                hold_count,
                "count",
                "hold 계열 stage 또는 reason이 있고 launched가 아닌 attack_funnel 행",
            ),
            "scout_to_preflight_rate_percent": funnel_rate(
                preflight_count, scout_count, "preflight / scout"
            ),
            "preflight_to_launched_rate_percent": funnel_rate(
                launched_count, preflight_count, "launched / preflight"
            ),
            "scout_to_launched_rate_percent": funnel_rate(
                launched_count, scout_count, "launched / scout"
            ),
            "hold_reason_counts": dict(sorted(reason_counts.items(), key=lambda item: (-item[1], item[0]))),
            "raw_stage_counts": dict(sorted(stage_counts.items())),
            "unknown_stage_rows": measured(
                unknown_count, "count", "인식 토큰이 없는 attack_funnel stage 행"
            ),
            "stage_semantics": (
                "한 행이 여러 토큰을 포함하면 여러 단계 카운트에 포함될 수 있음; "
                "writer stage 원문 빈도를 함께 확인"
            ),
        }
    capability = measured(
        sum(len(grouped[email]) for email in emails),
        "rows",
        "metrics.db attack_funnel 스키마 및 cutoff 조회 성공",
    )
    return output, capability


def scouting_metrics(
    game_data: dict[str, Any],
    combat_events: list[dict[str, Any]],
    probe_events: list[dict[str, Any]],
    emails: set[str],
    window_hours: float,
) -> dict[str, dict[str, Any]]:
    capability = game_data["capabilities"]["successful_scout_reports"]
    by_email: dict[str, list[dict[str, Any]]] = defaultdict(list)
    game_scouts_available = capability["status"] == "measured"
    if game_scouts_available:
        source_name = "game_db successful espionage reports + metrics battles"
        source_status = "derived"
        for row in game_data["scouts"]:
            email = str(row.get("observer_email") or "")
            when = db_datetime(row.get("created_at"))
            if email in emails and when:
                by_email[email].append({
                    "id": int(row["id"]),
                    "ts": when,
                    "target_email": str(row.get("target_email") or "") or None,
                })
    else:
        source_name = "metrics probe-only battles + combat battles fallback"
        source_status = "estimated"
        for row in probe_events:
            email = str(row.get("attacker_email") or "")
            when = db_datetime(row.get("ts"))
            if email in emails and when:
                by_email[email].append({
                    "id": int(row.get("battle_id") or 0),
                    "ts": when,
                    "target_email": str(row.get("defender_email") or "") or None,
                })
    attacks_by_email: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in combat_events:
        email = str(event.get("attacker_email") or "")
        when = db_datetime(event.get("ts"))
        if email in emails and when:
            attacks_by_email[email].append({**event, "when": when})

    window = dt.timedelta(hours=window_hours)
    output: dict[str, dict[str, Any]] = {}
    for email in sorted(emails):
        reports = sorted(by_email[email], key=lambda row: (row["ts"], row["id"]))
        matchable = [row for row in reports if row["target_email"]]
        attacks = sorted(
            attacks_by_email[email], key=lambda row: (row["when"], int(row.get("battle_id") or 0))
        )
        converted: set[int] = set()
        attacks_with_prior = 0
        for attack in attacks:
            candidates = [
                report for report in matchable
                if report["target_email"] == attack.get("defender_email")
                and report["ts"] <= attack["when"] < report["ts"] + window
            ]
            if candidates:
                attacks_with_prior += 1
            unused = [report for report in candidates if report["id"] not in converted]
            if unused:
                converted.add(max(unused, key=lambda row: (row["ts"], row["id"]))["id"])

        denominator = len(matchable)
        if denominator and game_scouts_available:
            rate = derived(
                round(len(converted) * 100.0 / denominator, 1),
                "percent",
                "성공 scout 이후 window 내 동일 대상 실전 공격을 최신 미사용 scout에 1:1 매칭",
                numerator=len(converted), denominator=denominator, window_hours=window_hours,
            )
        elif denominator:
            rate = estimated(
                round(len(converted) * 100.0 / denominator, 1),
                "percent",
                "probe-only battle 이후 window 내 동일 대상 실전 공격 1:1 매칭",
                "probe-only battle은 성공 scout 전체가 아니라 전투로 이어진 probe의 편향된 proxy",
                numerator=len(converted), denominator=denominator, window_hours=window_hours,
            )
        else:
            rate = missing(
                "percent",
                "target email이 있는 scout 입력이 0건이어서 비율이 정의되지 않음",
                numerator=0,
                denominator=0,
                window_hours=window_hours,
            )
        output[email] = {
            "source": {"status": source_status, "value": source_name},
            "scout_events": (
                measured(len(reports), "count", "game DB 성공 espionage report")
                if game_scouts_available
                else estimated(
                    len(reports),
                    "count",
                    "metrics battles의 probe-only 행",
                    "성공/실패 scout 전체가 아닌 전투 발생 probe proxy",
                )
            ),
            "preflight_events": missing("count", "attack_funnel 없이는 preflight를 관측할 수 없음"),
            "launched_events": measured(len(attacks), "count", "분류된 실전 공격 battle 행"),
            "hold_events": missing("count", "attack_funnel 없이는 hold terminal decision을 관측할 수 없음"),
            "scout_to_preflight_rate_percent": missing(
                "percent", "attack_funnel 없이는 preflight를 관측할 수 없음"
            ),
            "preflight_to_launched_rate_percent": missing(
                "percent", "attack_funnel 없이는 preflight를 관측할 수 없음"
            ),
            "scout_to_launched_rate_percent": rate,
            "hold_reason_counts": {},
            "matchable_scout_events": measured(
                denominator, "count", "target email이 있는 scout 입력"
            ),
            "unmatchable_target_events": measured(
                len(reports) - denominator, "count", "target email이 NULL인 scout 입력"
            ),
            "converted_unique_scout_events": measured(
                len(converted), "count", "실전 공격에 1:1 매칭된 scout 입력"
            ),
            "combat_attacks_with_prior_scout": measured(
                attacks_with_prior,
                "count",
                "1:1 소진과 무관하게 window 내 선행 성공 scout가 하나 이상인 실전 공격",
            ),
        }
    return output


def nonnegative_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def resource_triplet(container: Any) -> dict[str, float] | None:
    if not isinstance(container, dict):
        return None
    values: dict[str, float] = {}
    present = False
    for name in ("metal", "crystal", "deuterium"):
        if name in container:
            present = True
            number = nonnegative_number(container[name])
            if number is None:
                return None
            values[name] = number
        else:
            values[name] = 0.0
    return values if present else None


def parse_expedition_resources(key: str, raw_params: Any) -> tuple[str, dict[str, float] | None]:
    lowered = key.lower()
    try:
        params = json.loads(raw_params) if isinstance(raw_params, str) else raw_params
    except json.JSONDecodeError:
        return "unparsed", None
    if not isinstance(params, dict):
        if any(hint in lowered for hint in NO_RESOURCE_EXPEDITION_HINTS):
            return "known_no_resource", {"metal": 0, "crystal": 0, "deuterium": 0}
        return "unparsed", None

    for container_key in RESOURCE_CONTAINER_KEYS:
        triplet = resource_triplet(params.get(container_key))
        if triplet is not None:
            return "parsed", triplet
    if any(hint in lowered for hint in RESOURCE_EXPEDITION_HINTS):
        triplet = resource_triplet(params)
        if triplet is not None:
            return "parsed", triplet
    if any(hint in lowered for hint in NO_RESOURCE_EXPEDITION_HINTS):
        return "known_no_resource", {"metal": 0, "crystal": 0, "deuterium": 0}
    return "unparsed", None


def expedition_metrics(game_data: dict[str, Any], emails: set[str]) -> dict[str, dict[str, Any]]:
    capability = game_data["capabilities"]["expedition_messages"]
    if capability["status"] != "measured":
        reason = capability.get("reason", "expedition message capability 없음")
        return {
            email: {
                "event_count": missing("count", reason),
                "direct_resource_gain": missing("resource_equivalent", reason),
                "message_key_counts": {},
            }
            for email in emails
        }

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in game_data["expeditions"]:
        email = str(row.get("email") or "")
        if email in emails:
            grouped[email].append(row)

    result: dict[str, dict[str, Any]] = {}
    for email in sorted(emails):
        events = grouped[email]
        totals = {"metal": 0.0, "crystal": 0.0, "deuterium": 0.0}
        parse_counts: Counter[str] = Counter()
        key_counts: Counter[str] = Counter()
        for event in events:
            key = str(event.get("key") or "")
            key_counts[key] += 1
            state, resources = parse_expedition_resources(key, event.get("params"))
            parse_counts[state] += 1
            if resources:
                for name in totals:
                    totals[name] += resources[name]
        total = canonical_number(sum(totals.values()))
        components = {name: canonical_number(value) for name, value in totals.items()}
        if parse_counts["unparsed"]:
            gain = partial(
                total,
                "resource_equivalent",
                "expedition message의 명시적 resource triplet 합",
                "해석하지 못한 expedition message가 있어 known-only 합만 제시; 0으로 대체하지 않음",
                components=components,
                parsed_reward_events=parse_counts["parsed"],
                known_no_resource_events=parse_counts["known_no_resource"],
                unparsed_events=parse_counts["unparsed"],
            )
        else:
            gain = measured(
                total,
                "resource_equivalent",
                "expedition message의 명시적 resource triplet 합",
                components=components,
                parsed_reward_events=parse_counts["parsed"],
                known_no_resource_events=parse_counts["known_no_resource"],
                unparsed_events=0,
            )
        result[email] = {
            "event_count": measured(
                len(events), "count", "messages.key에 expedition이 포함된 기간 내 message"
            ),
            "direct_resource_gain": gain,
            "message_key_counts": dict(sorted(key_counts.items())),
            "scope_note": "함선 보상/손실, 다크매터, 연료 및 후속 투자 효과는 포함하지 않음",
        }
    return result


def median_metric(records: list[dict[str, Any]], path: tuple[str, ...], unit: str) -> dict[str, Any]:
    values: list[float] = []
    for record in records:
        item = nested(record, path)
        if isinstance(item, dict) and item.get("value") is not None and item.get("status") != "missing":
            values.append(float(item["value"]))
    if not values:
        return missing(unit, "유효 계정 값이 없음", sample_n=0)
    return derived(
        canonical_number(statistics.median(values)),
        unit,
        "계정별 값의 중앙값",
        sample_n=len(values),
    )


def difference_metric(ai: dict[str, Any], rule: dict[str, Any], unit: str) -> dict[str, Any]:
    if ai.get("value") is None or rule.get("value") is None:
        return missing(unit, "AI 또는 동일 클래스 규칙봇 중앙값이 없음")
    return derived(
        canonical_number(float(ai["value"]) - float(rule["value"])),
        unit,
        "AI 중앙값 - 동일 클래스 규칙봇 중앙값",
    )


def class_comparisons(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    class_names = sorted(
        {str(record["identity"]["character_class"].get("name") or "UNKNOWN") for record in records}
    )
    comparisons: list[dict[str, Any]] = []
    for class_name in class_names:
        same_class = [
            record for record in records
            if (record["identity"]["character_class"].get("name") or "UNKNOWN") == class_name
        ]
        ai_records = [record for record in same_class if record["identity"]["cohort"] == "ai"]
        rule_records = [record for record in same_class if record["identity"]["cohort"] == "rule"]
        ai_summary: dict[str, Any] = {"account_n": len(ai_records)}
        rule_summary: dict[str, Any] = {"account_n": len(rule_records)}
        differences: dict[str, Any] = {}
        for key, path, unit in METRIC_SPECS:
            ai_summary[key] = median_metric(ai_records, path, unit)
            rule_summary[key] = median_metric(rule_records, path, unit)
            differences[key] = difference_metric(ai_summary[key], rule_summary[key], unit)
        comparisons.append({
            "character_class": class_name,
            "ai": ai_summary,
            "same_class_rule": rule_summary,
            "ai_minus_same_class_rule_median": differences,
        })
    return comparisons


def attach_counterfactuals(
    records: list[dict[str, Any]], comparisons: list[dict[str, Any]]
) -> None:
    by_class = {item["character_class"]: item for item in comparisons}
    for record in records:
        score = record["growth"]["total_score_delta"]
        direct_gain = record["expedition"]["direct_resource_gain"]
        class_name = record["identity"]["character_class"].get("name") or "UNKNOWN"
        baseline = by_class[class_name]["same_class_rule"]["total_score_delta"]
        comparison = {
            "same_class_rule_median_score_delta": baseline,
            "score_delta_vs_same_class_rule_median": difference_metric(score, baseline, "score"),
        }
        record["comparison"] = comparison

        if score.get("value") is not None and direct_gain.get("value") is not None:
            resource_score_equivalent = int(round(float(direct_gain["value"]) / 1000.0))
            direct_adjusted = estimated(
                canonical_number(float(score["value"]) - resource_score_equivalent),
                "score",
                "total_score_delta - round(direct expedition resource gain / 1000)",
                "보상 자원이 모두 점수 자산으로 투자됐다고 놓는 resource-only proxy; "
                "실제 인과효과가 아니며 함선 보상/손실·보유 재고·소비 시점을 반영하지 않음",
                direct_resource_score_equivalent=resource_score_equivalent,
                direct_resource_status=direct_gain["status"],
            )
        else:
            direct_adjusted = missing(
                "score",
                "total score 또는 직접 expedition resource gain이 없어 직접 조정 불가",
                method="total_score_delta - expedition resource score-equivalent",
            )

        expedition_enabled = record["identity"]["expedition_enabled"].get("value") is True
        residual = comparison["score_delta_vs_same_class_rule_median"]
        if expedition_enabled and score.get("value") is not None and baseline.get("value") is not None:
            residual_gain = estimated(
                residual["value"],
                "score",
                "total_score_delta - 동일 클래스 규칙봇 score delta 중앙값",
                "원정 외 모든 AI/규칙 차이와 초기조건 차이를 함께 포함하는 교란된 잔여치; "
                "expedition 인과효과로 해석 금지",
            )
            residual_adjusted = estimated(
                baseline["value"],
                "score",
                "total_score_delta - same-class residual estimate",
                "동일 클래스 규칙봇 중앙값을 원정 없는 반사실 기준으로 둔 강한 가정",
            )
        else:
            residual_gain = missing(
                "score",
                "원정 활성 계정이 아니거나 동일 클래스 규칙봇 기준값이 없음",
            )
            residual_adjusted = missing(
                "score",
                "원정 활성 계정이 아니거나 동일 클래스 규칙봇 기준값이 없음",
            )
        record["counterfactual_growth"] = {
            "observed_total_score_delta": score,
            "direct_expedition_resource_gain": direct_gain,
            "direct_resource_adjusted_score_delta_estimate": direct_adjusted,
            "same_class_residual_attributed_gain_estimate": residual_gain,
            "same_class_residual_adjusted_score_delta_estimate": residual_adjusted,
        }


def fmt_value(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        if value.is_integer():
            return f"{int(value):,}"
        return f"{value:,.1f}"
    return str(value)


def fmt_metric(item: Any) -> str:
    if not isinstance(item, dict):
        return "-"
    status = item.get("status", "?")
    return f"{fmt_value(item.get('value'))} ({status})"


def markdown_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    output = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    output.extend("| " + " | ".join(str(value).replace("|", "\\|") for value in row) + " |" for row in rows)
    return output


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Week 4 AI 평가 리포트",
        "",
        f"- 기간: `{report['cutoff']['start']}` 이상, `{report['cutoff']['end']}` 미만 (UTC)",
        f"- 분석 fingerprint: `{report['analysis_fingerprint']}`",
        f"- AI cohort: `{', '.join(report['cohorts']['ai']['selectors'])}`",
        f"- 규칙봇 cohort: `{', '.join(report['cohorts']['rule']['selectors'])}`",
        f"- 계정 수: AI {report['cohorts']['ai']['matched_n']}, 규칙봇 {report['cohorts']['rule']['matched_n']}",
        "",
        "## 판독 규칙",
        "",
        "- `measured`: 입력 행에서 직접 집계한 값입니다.",
        "- `derived`: measured/partial 값을 명시된 산식으로 요약한 값입니다.",
        "- `estimated`: 검증 불가능한 가정을 포함한 추정치이며 실측과 섞지 않습니다.",
        "- `partial`: 일부 원본이 NULL 또는 해석 불가이며 표시값은 known-only 합입니다.",
        "- `missing`: 필요한 입력이 없어 값을 만들지 않았습니다. 0이 아닙니다.",
        "",
        "## 동일 클래스 AI 대 규칙봇",
        "",
    ]
    comparison_rows: list[list[str]] = []
    for item in report["class_comparisons"]:
        comparison_rows.append([
            item["character_class"],
            str(item["ai"]["account_n"]),
            str(item["same_class_rule"]["account_n"]),
            fmt_metric(item["ai_minus_same_class_rule_median"]["total_score_delta"]),
            fmt_metric(item["ai_minus_same_class_rule_median"]["planet_delta"]),
            fmt_metric(item["ai_minus_same_class_rule_median"]["building_delta"]),
            fmt_metric(item["ai_minus_same_class_rule_median"]["ship_delta"]),
            fmt_metric(item["ai_minus_same_class_rule_median"]["attacks"]),
            fmt_metric(item["ai_minus_same_class_rule_median"]["loot"]),
        ])
    lines.extend(markdown_table(
        ["class", "AI n", "rule n", "점수 Δ차", "식민지 Δ차", "건물 Δ차", "함선 Δ차", "공격 Δ차", "약탈 Δ차"],
        comparison_rows,
    ))
    lines.extend(["", "중앙값 차이는 `AI 중앙값 - 동일 클래스 규칙봇 중앙값`입니다.", "", "## AI 계정별 성장", ""])
    ai_records = [record for record in report["accounts"] if record["identity"]["cohort"] == "ai"]
    growth_rows: list[list[str]] = []
    for record in ai_records:
        identity = record["identity"]
        growth = record["growth"]
        growth_rows.append([
            identity["email"],
            str(identity["character_class"].get("name") or "UNKNOWN") + f" ({identity['character_class']['status']})",
            str(identity["personality"].get("value") or "-"),
            fmt_metric(growth["total_score_delta"]),
            fmt_metric(growth["colony"]["planet_count_delta"]),
            fmt_metric(growth["economy"]["building_level_sum_delta"]),
            fmt_metric(growth["economy"]["facility_level_sum_delta"]),
            fmt_metric(growth["military"]["ship_count_delta"]),
            fmt_metric(growth["military"]["defense_count_delta"]),
        ])
    lines.extend(markdown_table(
        ["AI", "class", "personality", "총점 Δ", "행성 Δ", "건물레벨합 Δ", "시설레벨합 Δ", "함선수 Δ", "방어수 Δ"],
        growth_rows,
    ))
    lines.extend(["", "함선수와 방어수는 구성·비용을 반영하지 않는 별도 개수 proxy이며 합산하지 않습니다.", "", "## AI 계정별 실전 전투", ""])
    combat_rows: list[list[str]] = []
    for record in ai_records:
        combat = record["combat"]
        combat_rows.append([
            record["identity"]["email"],
            fmt_metric(combat["outgoing_combat_attacks"]),
            fmt_metric(combat["outgoing_loot"]),
            fmt_metric(combat["outgoing_attacker_loss"]),
            fmt_metric(combat["outgoing_inflicted_loss"]),
            fmt_metric(combat["incoming_defender_loss"]),
            fmt_metric(combat["debris_harvest_income"]),
            fmt_metric(combat["known_combat_net"]),
        ])
    lines.extend(markdown_table(
        ["AI", "실전 공격", "약탈", "공격 손실", "가한 손실", "피격 손실", "데브리 회수", "known 순값"],
        combat_rows,
    ))
    lines.extend([
        "",
        "관리자 테스트, probe-only, 분류 flag가 NULL인 행은 실전 공격에서 제외했습니다. known 순값은 약탈+회수-공격손실-피격손실이며 하나라도 partial/missing이면 계산하지 않습니다.",
        "",
        "## Scout-to-preflight-to-launched funnel",
        "",
    ])
    scout_rows: list[list[str]] = []
    for record in ai_records:
        scout = record["scouting"]
        reasons = scout.get("hold_reason_counts", {})
        reason_text = ", ".join(f"{key}:{value}" for key, value in list(reasons.items())[:3]) or "-"
        scout_rows.append([
            record["identity"]["email"],
            str(scout.get("source", {}).get("value") or "-"),
            fmt_metric(scout["scout_events"]),
            fmt_metric(scout["preflight_events"]),
            fmt_metric(scout["launched_events"]),
            fmt_metric(scout["scout_to_preflight_rate_percent"]),
            fmt_metric(scout["preflight_to_launched_rate_percent"]),
            fmt_metric(scout["scout_to_launched_rate_percent"]),
            fmt_metric(scout["hold_events"]),
            reason_text,
        ])
    lines.extend(markdown_table(
        ["AI", "source", "scout", "preflight", "launched", "S→P %", "P→L %", "S→L %", "hold", "상위 hold reason"],
        scout_rows,
    ))
    lines.extend([
        "",
        "attack_funnel이 있으면 stage 원문 카운트를 사용합니다. 없으면 game DB 성공 scout+실전 battle, 그것도 없으면 probe-only battle+실전 battle을 사용하며 preflight와 hold는 missing입니다.",
        "",
        "## 원정 반사실",
        "",
    ])
    expedition_rows: list[list[str]] = []
    for record in ai_records:
        counter = record["counterfactual_growth"]
        expedition_rows.append([
            record["identity"]["email"],
            fmt_metric(record["expedition"]["event_count"]),
            fmt_metric(record["expedition"]["direct_resource_gain"]),
            fmt_metric(counter["observed_total_score_delta"]),
            fmt_metric(counter["direct_resource_adjusted_score_delta_estimate"]),
            fmt_metric(counter["same_class_residual_attributed_gain_estimate"]),
        ])
    lines.extend(markdown_table(
        ["AI", "원정 메시지", "직접 자원 gain", "관측 총점 Δ", "직접조정 점수 Δ", "동급 잔여 gain"],
        expedition_rows,
    ))
    lines.extend([
        "",
        "직접조정 점수는 `총점 Δ - round(직접 원정 자원/1000)`인 resource-only 추정입니다. 자원이 실제로 투자된 시점, 함선 보상·손실, 연료, 다른 정책 차이를 알 수 없으므로 원정의 인과효과가 아닙니다.",
        "",
        "동급 잔여 gain은 `AI 총점 Δ - 동일 클래스 규칙봇 중앙값`입니다. 원정 외 모든 차이를 포함하므로 원정 효과로 해석하면 안 됩니다.",
        "",
        "## 데이터 품질",
        "",
        "```json",
        json.dumps(report["data_quality"], ensure_ascii=False, sort_keys=True, indent=2),
        "```",
        "",
        "## 한계",
        "",
    ])
    lines.extend(f"- {item}" for item in report["limitations"])
    lines.append("")
    return "\n".join(lines)


def write_atomic(path: pathlib.Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def parse_class_filter(raw: str) -> set[str] | None:
    values = {part.strip().upper() for part in raw.split(",") if part.strip()}
    if not values or values == {"ALL"}:
        return None
    normalized: set[str] = set()
    for value in values:
        if value.isdigit() and int(value) in CLASS_NAMES:
            normalized.add(CLASS_NAMES[int(value)])
        elif value in CLASS_IDS or value == "UNKNOWN":
            normalized.add(value)
        else:
            raise ValueError(f"알 수 없는 class filter: {value}")
    return normalized


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Week 4 AI 대 동일 클래스 규칙봇 평가 리포트")
    parser.add_argument("--metrics-db", type=pathlib.Path, default=DEFAULT_METRICS_DB)
    parser.add_argument("--start", "--cutoff-start", required=True, help="포함 UTC ISO-8601 시각")
    parser.add_argument("--end", "--cutoff-end", required=True, help="미포함 UTC ISO-8601 시각")
    parser.add_argument(
        "--ai-cohort", action="append", help="AI email glob/CSV. 반복 가능. 기본 ai*@bots.local"
    )
    parser.add_argument(
        "--rule-cohort", action="append", help="규칙봇 email glob/CSV. 반복 가능. 기본 bot*@bots.local"
    )
    parser.add_argument(
        "--class", dest="class_filter", default="ALL", help="ALL 또는 COLLECTOR,GENERAL,DISCOVERER CSV"
    )
    parser.add_argument("--policy-dir", type=pathlib.Path, default=DEFAULT_POLICY_DIR)
    parser.add_argument("--scout-window-hours", type=float, default=24.0)
    parser.add_argument("--output-prefix", type=pathlib.Path, default=ROOT / "reports" / "week4_evaluation")
    parser.add_argument("--game-db-host", help="선택적 MariaDB host. 생략하면 game DB 지표는 missing")
    parser.add_argument("--game-db-port", type=int, default=3306)
    parser.add_argument("--game-db-name", default="laravel")
    parser.add_argument("--game-db-user", default="botread")
    parser.add_argument("--game-db-password-env", default="OGAME_REPORT_DB_PASSWORD")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    start = parse_utc(args.start, "--start")
    end = parse_utc(args.end, "--end")
    if start >= end:
        raise ValueError("--start는 --end보다 빨라야 합니다")
    if args.scout_window_hours <= 0:
        raise ValueError("--scout-window-hours는 0보다 커야 합니다")
    class_filter = parse_class_filter(args.class_filter)
    ai_selectors = selectors(args.ai_cohort, "ai*@bots.local")
    rule_selectors = selectors(args.rule_cohort, "bot*@bots.local")

    con = sqlite_ro(args.metrics_db)
    try:
        required_snapshot_columns = {
            "ts", "email", "personality", "planets", "building_sum", "facility_sum",
            "defense_sum", "ships_sum", "metal", "crystal", "deuterium", "score",
        }
        absent = required_snapshot_columns - table_columns(con, "snapshots")
        if absent:
            raise RuntimeError(f"snapshots 필수 컬럼 누락: {', '.join(sorted(absent))}")
        available = [
            str(row[0]) for row in con.execute(
                "SELECT DISTINCT email FROM snapshots WHERE ts >= ? AND ts < ? ORDER BY email",
                (iso(start), iso(end)),
            )
        ]
        ai_emails, unmatched_ai = select_emails(available, ai_selectors)
        rule_emails, unmatched_rule = select_emails(available, rule_selectors)
        overlap = sorted(set(ai_emails) & set(rule_emails))
        if overlap:
            raise ValueError(f"AI와 규칙봇 cohort가 겹칩니다: {', '.join(overlap)}")
        if not ai_emails:
            raise ValueError("cutoff 안에서 AI cohort와 일치하는 snapshot 계정이 없습니다")
        if not rule_emails:
            raise ValueError("cutoff 안에서 규칙봇 cohort와 일치하는 snapshot 계정이 없습니다")

        preliminary = sorted(set(ai_emails) | set(rule_emails))
        marks = ",".join(["?"] * len(preliminary))
        rows = con.execute(
            f"""
            SELECT ts,email,personality,planets,building_sum,facility_sum,defense_sum,
                   ships_sum,metal,crystal,deuterium,score
            FROM snapshots
            WHERE ts >= ? AND ts < ? AND email IN ({marks})
            ORDER BY email,ts,rowid
            """,
            [iso(start), iso(end), *preliminary],
        ).fetchall()
        snapshots: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for row in rows:
            snapshots[str(row["email"])].append(row)

        game_data = load_game_data(args, preliminary, start, end)
        identities, policy_fingerprints = build_identities(
            snapshots, set(ai_emails), game_data, args.policy_dir.expanduser().resolve()
        )
        final_emails = [
            email for email in preliminary
            if class_filter is None
            or (identities[email]["character_class"].get("name") or "UNKNOWN") in class_filter
        ]
        if not final_emails:
            raise ValueError("class filter 적용 후 남은 계정이 없습니다")
        final_ai = sorted(email for email in final_emails if email in set(ai_emails))
        final_rule = sorted(email for email in final_emails if email in set(rule_emails))
        combat, combat_events, probe_events, combat_quality = collect_combat(
            con, set(final_emails), start, end
        )
        funnel_scouting, attack_funnel_capability = collect_attack_funnel(
            con, set(final_emails), start, end
        )
    finally:
        con.rollback()
        con.close()

    if attack_funnel_capability["status"] == "measured":
        scouting = funnel_scouting
    else:
        scouting = scouting_metrics(
            game_data,
            combat_events,
            probe_events,
            set(final_emails),
            args.scout_window_hours,
        )
    expeditions = expedition_metrics(game_data, set(final_emails))
    records: list[dict[str, Any]] = []
    for email in final_emails:
        records.append({
            "identity": identities[email],
            "growth": build_growth(snapshots[email], start, end),
            "combat": combat[email],
            "scouting": scouting[email],
            "expedition": expeditions[email],
        })
    records.sort(key=lambda record: (record["identity"]["cohort"], record["identity"]["email"]))
    comparisons = class_comparisons(records)
    attach_counterfactuals(records, comparisons)

    limitations = [
        "기간은 UTC 반개구간 [start, end)이며 성장 endpoint는 그 안의 첫/마지막 snapshot입니다. 경계 보간은 하지 않습니다.",
        "game DB users.character_class는 현재값만 제공하므로 cutoff 기간 중 class 변경 이력을 증명하지 못합니다. game DB가 없으면 personality 매핑 추정입니다.",
        "동일 class 비교는 무작위 배정 실험이 아닙니다. 시작 자산, 위치, 표본 수, personality와 정책 차이를 통제하지 않습니다.",
        "경제 성장은 건물/시설 레벨합과 자원 재고 차이입니다. 생산량이나 투자 자원가치가 아니며 highscores.economy의 기간 이력이 없습니다.",
        "군사 성장은 함선수와 방어수의 별도 개수 proxy입니다. 함종·방어종 비용과 전투력을 반영하지 않습니다.",
        "실전 전투는 is_admin_test=0 및 is_probe_only=0으로 확정된 battle만 포함합니다. NULL 분류 행은 0으로 간주하지 않고 제외합니다.",
        "scout conversion은 성공 espionage report만 보며 동일 공격자·대상 계정의 시간창 매칭입니다. 좌표 단위 인과관계나 실패한 정찰 시도 전체를 나타내지 않습니다.",
        "원정 직접 gain은 해석 가능한 message resource triplet만 포함합니다. 함선/다크매터 보상, 함대 손실, 연료와 후속 투자 효과는 포함하지 않습니다.",
        "resource/1000 점수 환산과 동일 클래스 잔여치는 estimated입니다. 원정의 인과효과나 정확한 counterfactual로 사용하면 안 됩니다.",
    ]
    report: dict[str, Any] = {
        "schema_version": "week4-evaluation-v1",
        "cutoff": {"start": iso(start), "end": iso(end), "interval": "[start,end)", "timezone": "UTC"},
        "parameters": {
            "metrics_db": str(args.metrics_db),
            "policy_dir": str(args.policy_dir),
            "class_filter": sorted(class_filter) if class_filter else ["ALL"],
            "scout_window_hours": args.scout_window_hours,
            "game_db_enabled": bool(args.game_db_host),
            "game_db_host": args.game_db_host,
            "game_db_port": args.game_db_port if args.game_db_host else None,
            "game_db_name": args.game_db_name if args.game_db_host else None,
            "game_db_user": args.game_db_user if args.game_db_host else None,
        },
        "cohorts": {
            "ai": {
                "selectors": ai_selectors,
                "matched_emails": final_ai,
                "matched_n": len(final_ai),
                "unmatched_selectors": unmatched_ai,
            },
            "rule": {
                "selectors": rule_selectors,
                "matched_emails": final_rule,
                "matched_n": len(final_rule),
                "unmatched_selectors": unmatched_rule,
            },
        },
        "policy_fingerprints": dict(sorted(policy_fingerprints.items())),
        "class_comparisons": comparisons,
        "accounts": records,
        "data_quality": {
            "metrics_combat": combat_quality,
            "attack_funnel": attack_funnel_capability,
            "game_db_capabilities": game_data["capabilities"],
            "snapshot_rows_selected": sum(len(snapshots[email]) for email in final_emails),
            "combat_events_selected": len(combat_events),
        },
        "limitations": limitations,
    }
    fingerprint_source = json.dumps(
        report, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    report["analysis_fingerprint"] = hashlib.sha256(fingerprint_source).hexdigest()

    prefix = args.output_prefix.expanduser()
    json_path = pathlib.Path(str(prefix) + ".json")
    markdown_path = pathlib.Path(str(prefix) + ".md")
    json_text = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    markdown_text = render_markdown(report)
    write_atomic(json_path, json_text)
    write_atomic(markdown_path, markdown_text)
    print(f"JSON: {json_path}")
    print(f"Markdown: {markdown_path}")
    print(f"fingerprint: {report['analysis_fingerprint']}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)
