#!/usr/bin/env python3
"""기존 수동 Admin 테스트 전투를 보존·표시하고 규칙봇의 연료만 멱등 환급한다."""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sqlite3
import sys

import pymysql


PROJECT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from bots.shared import db_reader  # noqa: E402
from scripts import metrics_collector  # noqa: E402


def _connect():
    password = os.environ.get("OGAME_DB_ADMIN_PASSWORD")
    if not password:
        raise RuntimeError("OGAME_DB_ADMIN_PASSWORD is required")
    return pymysql.connect(
        host=db_reader.DB_HOST,
        port=db_reader.DB_PORT,
        user=os.environ.get("OGAME_DB_ADMIN_USER", "root"),
        password=password,
        database=db_reader.DB_NAME,
        autocommit=False,
        cursorclass=pymysql.cursors.DictCursor,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--battle-id", type=int, required=True)
    parser.add_argument("--mission-id", type=int, required=True)
    parser.add_argument("--expected-email", required=True)
    args = parser.parse_args()

    conn = _connect()
    reimbursed = 0.0
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT br.id,br.general,
                          CAST(JSON_UNQUOTE(JSON_EXTRACT(br.attacker,'$.player_id')) AS UNSIGNED)
                              attacker_id,
                          u.email,fm.planet_id_from,fm.deuterium_consumption
                   FROM battle_reports br
                   JOIN fleet_missions fm ON fm.id=%s
                   JOIN users u ON u.id=fm.user_id
                   WHERE br.id=%s""",
                (args.mission_id, args.battle_id),
            )
            row = cur.fetchone()
            if not row or row["email"] != args.expected_email:
                raise RuntimeError("battle/mission/account identity mismatch")
            if int(row["attacker_id"] or 0) != int(
                _mission_user_id(cur, args.mission_id)
            ):
                raise RuntimeError("battle attacker does not match mission owner")

            general = json.loads(row["general"] or "{}")
            if not general.get("admin_test_fuel_reimbursed"):
                reimbursed = float(row["deuterium_consumption"] or 0)
                cur.execute(
                    "UPDATE planets SET deuterium=deuterium+%s WHERE id=%s",
                    (reimbursed, int(row["planet_id_from"])),
                )

            cur.execute(
                """UPDATE battle_reports
                   SET general=JSON_SET(
                       COALESCE(general,JSON_OBJECT()),
                       '$.admin_test',TRUE,
                       '$.admin_test_reason','legacy manual simulation attack',
                       '$.admin_test_fuel_reimbursed',TRUE
                   )
                   WHERE id=%s""",
                (args.battle_id,),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    metrics = sqlite3.connect(PROJECT / "scripts" / "metrics.db")
    try:
        metrics_collector.init_db(metrics)
        metrics.execute(
            "UPDATE battles SET is_admin_test=1 WHERE battle_id=?", (args.battle_id,)
        )
        metrics.commit()
    finally:
        metrics.close()

    print(json.dumps({
        "battle_id": args.battle_id,
        "mission_id": args.mission_id,
        "classified_admin_test": True,
        "fuel_reimbursed": round(reimbursed, 3),
    }, ensure_ascii=False))
    return 0


def _mission_user_id(cur, mission_id: int) -> int:
    cur.execute("SELECT user_id FROM fleet_missions WHERE id=%s", (mission_id,))
    row = cur.fetchone()
    if not row:
        raise RuntimeError("mission not found")
    return int(row["user_id"])


if __name__ == "__main__":
    raise SystemExit(main())
