#!/usr/bin/env python3
"""probe-only 손실·debris-only 수입으로 오염된 과거 관측만 선택 정리한다."""
from __future__ import annotations

import argparse
import datetime as dt
import difflib
import json
import pathlib
import shutil
import sqlite3
import sys
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bots.ai_agents import experience_scorecard as sc


def _interval(item: dict[str, Any]) -> tuple[str | None, str | None]:
    interval = item.get("interval")
    if isinstance(interval, dict):
        return interval.get("start"), interval.get("end")
    observation_id = item.get("id")
    start = observation_id.rsplit("|", 1)[-1] if isinstance(observation_id, str) else None
    return start, item.get("observed_at")


def _probe_evidence(
    con: sqlite3.Connection,
    email: str,
    start: str,
    end: str,
) -> tuple[int, int]:
    columns = sc._table_columns(con, "battles")
    if not {"battle_ts", "attacker_email", "defender_email", "is_probe_only"}.issubset(columns):
        return 0, 0
    admin_filter = (
        " AND COALESCE(is_admin_test,0)=0" if "is_admin_test" in columns else ""
    )
    row = con.execute(
        f"""SELECT
                  SUM(CASE WHEN COALESCE(is_probe_only,0)=1 THEN 1 ELSE 0 END) AS probes,
                  SUM(CASE WHEN COALESCE(is_probe_only,0)=0 THEN 1 ELSE 0 END) AS conventional
             FROM battles
            WHERE (attacker_email=? OR defender_email=?)
              AND battle_ts>=? AND battle_ts<=?{admin_filter}""",
        (email, email, start, end),
    ).fetchone()
    return int(row[0] or 0), int(row[1] or 0)


def _directional_conventional_evidence(
    con: sqlite3.Connection,
    email: str,
    start: str,
    end: str,
) -> tuple[int, int]:
    """Return conventional battles as attacker and defender separately."""
    columns = sc._table_columns(con, "battles")
    needed = {"battle_ts", "attacker_email", "defender_email", "is_probe_only"}
    if not needed.issubset(columns):
        return 0, 0
    admin_filter = (
        " AND COALESCE(is_admin_test,0)=0" if "is_admin_test" in columns else ""
    )
    row = con.execute(
        f"""SELECT
                  SUM(CASE WHEN attacker_email=? THEN 1 ELSE 0 END) AS attacks,
                  SUM(CASE WHEN defender_email=? THEN 1 ELSE 0 END) AS defenses
             FROM battles
            WHERE (attacker_email=? OR defender_email=?)
              AND battle_ts>=? AND battle_ts<=?
              AND COALESCE(is_probe_only,0)=0{admin_filter}""",
        (email, email, email, email, start, end),
    ).fetchone()
    return int(row[0] or 0), int(row[1] or 0)


def _debris_evidence(
    con: sqlite3.Connection,
    email: str,
    start: str,
    end: str,
) -> tuple[int, float]:
    if not {"event_ts", "email", "metal", "crystal", "deuterium"}.issubset(
        sc._table_columns(con, "debris_harvests")
    ):
        return 0, 0
    row = con.execute(
        """SELECT COUNT(*), COALESCE(SUM(metal+crystal+deuterium),0)
             FROM debris_harvests
            WHERE email=? AND event_ts>=? AND event_ts<=?""",
        (email, start, end),
    ).fetchone()
    return int(row[0] or 0), float(row[1] or 0)


def _load_evidence_window(
    con: sqlite3.Connection,
    email: str,
    intervals: list[tuple[str, str]],
) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    """Load one account's evidence once instead of rescanning large tables per interval."""
    if not intervals:
        return [], []
    lower = min(start for start, _ in intervals)
    upper = max(end for _, end in intervals)
    battle_rows: list[tuple[Any, ...]] = []
    battle_columns = sc._table_columns(con, "battles")
    required = {
        "battle_ts",
        "attacker_email",
        "defender_email",
        "is_probe_only",
    }
    if required.issubset(battle_columns):
        admin = (
            "COALESCE(is_admin_test,0)"
            if "is_admin_test" in battle_columns
            else "0"
        )
        battle_rows = list(
            con.execute(
                f"""SELECT battle_ts, attacker_email, defender_email,
                           COALESCE(is_probe_only,0), {admin}
                      FROM battles
                     WHERE battle_ts>=? AND battle_ts<=?
                       AND (attacker_email=? OR defender_email=?)""",
                (lower, upper, email, email),
            )
        )
    debris_rows: list[tuple[Any, ...]] = []
    debris_required = {"event_ts", "email", "metal", "crystal", "deuterium"}
    if debris_required.issubset(sc._table_columns(con, "debris_harvests")):
        debris_rows = list(
            con.execute(
                """SELECT event_ts, metal, crystal, deuterium
                     FROM debris_harvests
                    WHERE event_ts>=? AND event_ts<=? AND email=?""",
                (lower, upper, email),
            )
        )
    return battle_rows, debris_rows


def _window_evidence(
    battle_rows: list[tuple[Any, ...]],
    debris_rows: list[tuple[Any, ...]],
    email: str,
    start: str,
    end: str,
) -> tuple[int, int, int, int, int, float]:
    relevant_battles = [
        row
        for row in battle_rows
        if isinstance(row[0], str) and start <= row[0] <= end and not int(row[4] or 0)
    ]
    probes = sum(1 for row in relevant_battles if int(row[3] or 0) == 1)
    conventional_rows = [
        row for row in relevant_battles if int(row[3] or 0) == 0
    ]
    attacks = sum(1 for row in conventional_rows if row[1] == email)
    defenses = sum(1 for row in conventional_rows if row[2] == email)
    relevant_harvests = [
        row
        for row in debris_rows
        if isinstance(row[0], str) and start <= row[0] <= end
    ]
    debris_income = sum(
        float(row[1] or 0) + float(row[2] or 0) + float(row[3] or 0)
        for row in relevant_harvests
    )
    return (
        probes,
        len(conventional_rows),
        attacks,
        defenses,
        len(relevant_harvests),
        debris_income,
    )


def repair_observations(
    ledger: dict[str, Any],
    metrics_db: pathlib.Path,
    email: str,
    *,
    cleaned_at: str,
    kind: str = "probe-only-negative",
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    observations = ledger.get("observations")
    if not isinstance(observations, list):
        observations = []
    repaired = [dict(item) if isinstance(item, dict) else item for item in observations]
    touched: list[dict[str, Any]] = []
    con = sqlite3.connect(f"file:{metrics_db.resolve()}?mode=ro", uri=True)
    try:
        intervals = [
            (start, end)
            for item in repaired
            if isinstance(item, dict)
            for start, end in [_interval(item)]
            if isinstance(start, str) and isinstance(end, str)
        ]
        battle_rows, debris_rows = _load_evidence_window(con, email, intervals)
        for item in repaired:
            if not isinstance(item, dict):
                continue
            # Legacy probe/debris cleanup must not rewrite already split
            # evidence. The defense-only repair deliberately targets the
            # current split schema because its old raid formula subtracted
            # unrelated losses suffered while defending.
            if kind != "defense-only-negative" and item.get("raid") is not None:
                continue
            start, end = _interval(item)
            if not isinstance(start, str) or not isinstance(end, str):
                continue
            (
                probes,
                conventional,
                attacks,
                defenses,
                harvests,
                debris_income,
            ) = _window_evidence(
                battle_rows, debris_rows, email, start, end
            )
            old = item.get("raid", item.get("military"))
            if kind == "probe-only-negative":
                if old != "negative" or probes <= 0 or conventional != 0:
                    continue
                raid, harvest = "neutral", "unavailable"
                reason = "probe-only interval cannot be raid failure"
            elif kind == "debris-only-positive":
                if old != "positive" or conventional != 0 or harvests <= 0:
                    continue
                raid = "neutral" if probes > 0 else "no-activity"
                harvest = "positive" if debris_income > 0 else "neutral"
                reason = "debris-only income cannot validate raid policy"
            elif kind == "defense-only-negative":
                if old != "negative" or attacks != 0 or defenses <= 0:
                    continue
                raid = "no-activity"
                harvest = str(item.get("harvest") or "unavailable")
                reason = "defense-only losses cannot be attributed to raid policy"
            else:
                raise ValueError(f"unsupported repair kind: {kind}")
            if "raid" in item:
                item["legacy_raid"] = old
            else:
                item["legacy_military"] = old
            item["raid"] = raid
            item["harvest"] = harvest
            item["military"] = raid
            item["contamination_cleanup"] = {
                "reason": reason,
                "cleaned_at": cleaned_at,
                "probe_only_reports": probes,
                "conventional_battles": conventional,
                "conventional_attacks": attacks,
                "conventional_defenses": defenses,
                "harvests": harvests,
                "debris_income": debris_income,
            }
            touched.append({
                "id": item.get("id"),
                "before": old,
                "after": raid,
                "probe_only_reports": probes,
                "conventional_attacks": attacks,
                "conventional_defenses": defenses,
                "harvests": harvests,
                "debris_income": debris_income,
            })
    finally:
        con.close()

    promoted = sc.promoted_lessons_from_observations(repaired)
    result = dict(ledger)
    result.update({
        "schema_version": sc.SCHEMA_VERSION,
        "promotion_rule": (
            "same controllable policy facet + same economy/raid direction >=3, "
            "no opposite evidence; legacy combined military excluded"
        ),
        "observations": repaired,
        "promoted_lessons": promoted,
        "last_contamination_cleanup": {
            "cleaned_at": cleaned_at,
            "email": email,
            "touched_observations": len(touched),
            "scope": kind,
        },
        "post_cleanup_observation_count": 0,
        "post_cleanup_observation_target": 4,
    })
    return result, touched


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", default="ai10@bots.local")
    parser.add_argument("--metrics-db", type=pathlib.Path, default=ROOT / "scripts/metrics.db")
    parser.add_argument("--ledger", type=pathlib.Path)
    parser.add_argument("--memory", type=pathlib.Path)
    parser.add_argument("--backup-root", type=pathlib.Path, default=ROOT / "scripts/backups")
    parser.add_argument(
        "--kind",
        choices=(
            "probe-only-negative",
            "debris-only-positive",
            "defense-only-negative",
        ),
        default="probe-only-negative",
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    agent = args.email.split("@", 1)[0]
    ledger_path = args.ledger or ROOT / "bots/ai_agents/state" / agent / "experience_ledger.json"
    memory_path = args.memory or ROOT / "bots/ai_agents/state" / agent / "memory.md"
    ledger = sc._load_json_dict(ledger_path)
    if ledger is None:
        raise SystemExit(f"ledger missing or invalid: {ledger_path}")
    before_lessons = list(ledger.get("promoted_lessons") or [])
    cleaned_at = sc._iso(dt.datetime.now(dt.timezone.utc))
    repaired, touched = repair_observations(
        ledger, args.metrics_db, args.email, cleaned_at=cleaned_at, kind=args.kind
    )
    after_lessons = list(repaired.get("promoted_lessons") or [])

    diff = "".join(difflib.unified_diff(
        _json_text(ledger).splitlines(keepends=True),
        _json_text(repaired).splitlines(keepends=True),
        fromfile=str(ledger_path) + ".before",
        tofile=str(ledger_path) + ".after",
    ))
    print(json.dumps({
        "mode": "apply" if args.apply else "dry-run",
        "email": args.email,
        "kind": args.kind,
        "touched_observations": touched,
        "promoted_lessons_before": before_lessons,
        "promoted_lessons_after": after_lessons,
        "policy_file_touched": False,
    }, ensure_ascii=False, indent=2))
    print(diff or "(no diff)")

    if args.apply:
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = args.backup_root / f"scorecard_cleanup_{stamp}" / agent
        backup.mkdir(parents=True, exist_ok=False)
        shutil.copy2(ledger_path, backup / ledger_path.name)
        if memory_path.exists():
            shutil.copy2(memory_path, backup / memory_path.name)
        sc._atomic_json(ledger_path, repaired)
        sc._update_memory(memory_path, after_lessons)
        (backup / "ledger.diff").write_text(diff, encoding="utf-8")
        (backup / "summary.json").write_text(json.dumps({
            "email": args.email,
            "kind": args.kind,
            "touched_observations": touched,
            "promoted_lessons_before": before_lessons,
            "promoted_lessons_after": after_lessons,
            "policy_file_touched": False,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"backup={backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
