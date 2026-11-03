#!/usr/bin/env python3
"""Per-run combat and standings summary from a finished run's final checkpoint.

  python3 -m sim.analyze_run runs/series_s4 [--json out.json]

Loads runs/<run>/checkpoint.sql.gz into a throwaway MariaDB database (the
live game DB is untouched), then reports:
  - group mean scores and slot-by-slot comparison against the static group
  - attack missions: farm raids, attacks on other bots, expeditions
  - battle reports: probe-only losses, real fleet fights, farm loot,
    expedition (pirate/alien) fights
"""
from __future__ import annotations

import argparse
import collections
import gzip
import json
import pathlib
import sqlite3
import subprocess

DB_CONTAINER = "ogamex-ogamex-db-1"
GROUPS = {"cl": "classic", "st": "static", "ll": "llm", "lk": "llm_locked", "le": "llm_econ"}


def mariadb(db: str | None, sql: str | None = None, stdin: bytes | None = None) -> str:
    cmd = ["docker", "exec", "-i", DB_CONTAINER, "mariadb", "-uroot", "-ptoor", "-N", "-B"]
    if db:
        cmd.append(db)
    if sql is not None:
        cmd += ["-e", sql]
    return subprocess.run(cmd, input=stdin, capture_output=True, check=True).stdout.decode()


def rows(db: str, sql: str) -> list[list[str]]:
    return [line.split("\t") for line in mariadb(db, sql).splitlines() if line]


def standings(run_dir: pathlib.Path) -> dict:
    conn = sqlite3.connect(f"file:{run_dir / 'metrics.db'}?mode=ro", uri=True)
    last = conn.execute("SELECT MAX(turn) FROM scores").fetchone()[0]
    by: dict[str, list] = collections.defaultdict(list)
    for acc, grp, pers, score in conn.execute(
            "SELECT account, grp, personality, general FROM scores "
            "WHERE turn=? AND grp NOT IN ('farm','other') ORDER BY account", (last,)):
        by[grp].append((acc, pers, score))
    means = {g: round(sum(s for *_, s in v) / len(v)) for g, v in by.items()}
    static = sorted(by.get("static", []))
    paired = {}
    for g, v in by.items():
        if g == "static" or not static:
            continue
        slots = []
        for (_, pers, s0), (acc, _, s1) in zip(static, sorted(v)):
            slots.append({"slot": acc[-2:], "personality": pers, "static": s0, "score": s1,
                          "diff_pct": round((s1 - s0) / s0 * 100, 1)})
        paired[g] = {"wins": sum(x["score"] > x["static"] for x in slots), "slots": slots}
    return {"turn": last, "means": means, "paired": paired}


def combat(db: str) -> dict:
    users = {int(i): n for i, n in rows(db, "SELECT id, username FROM users")}

    def grp(pid) -> str:
        name = users.get(pid, "")
        if name.startswith("farm"):
            return "farm"
        return name[:2] if name[:2] in GROUPS else "other"

    missions = collections.defaultdict(collections.Counter)
    for owner, mtype, target in rows(db, """
            SELECT f.user_id, f.mission_type, COALESCE(p.user_id, 0)
            FROM fleet_missions f LEFT JOIN planets p ON p.id = f.planet_id_to
            WHERE f.parent_id IS NULL AND f.mission_type IN (1, 15)"""):
        g = grp(int(owner))
        if g not in GROUPS:
            continue
        if mtype == "15":
            missions[g]["expeditions"] += 1
        elif grp(int(target)) == "farm":
            missions[g]["farm_raids"] += 1
        elif int(target) != int(owner):
            missions[g]["attacks_on_bots"] += 1
            missions[grp(int(target))]["attacked"] += 1

    battles = collections.defaultdict(collections.Counter)
    for att, dfn, loot in rows(db, "SELECT attacker, defender, loot FROM battle_reports"):
        a, d, l = json.loads(att), json.loads(dfn), json.loads(loot)
        ag, dg = grp(a.get("player_id")), grp(d.get("player_id"))
        gained = sum(l.get(r, 0) for r in ("metal", "crystal", "deuterium"))
        if ag == "other" and dg in GROUPS:          # expedition pirates / aliens
            b = battles[dg]
            b["expedition_fights"] += 1
            b["expedition_lost"] += d.get("resource_loss", 0)
            b["expedition_fights_lost"] += a.get("resource_loss", 0) < d.get("resource_loss", 0)
        elif ag in GROUPS and set(a.get("units", {})) <= {"espionage_probe"}:
            battles[ag]["probe_losses"] += a.get("resource_loss", 0)
        elif ag in GROUPS and dg == "farm":
            battles[ag]["farm_loot"] += gained
        elif ag in GROUPS and dg in GROUPS:
            b = battles[ag]
            b["fleet_fights"] += 1
            b["fleet_fights_won"] += a.get("resource_loss", 0) < d.get("resource_loss", 0) or gained > 0
            b["fleet_fight_loot"] += gained
    return {g: {**missions[g], **battles[g]} for g in GROUPS if g in missions or g in battles}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument("--json")
    args = ap.parse_args()
    run_dir = pathlib.Path(args.run_dir)
    db = "an_" + run_dir.name.replace("-", "_")
    mariadb(None, f"DROP DATABASE IF EXISTS {db}; CREATE DATABASE {db}")
    try:
        mariadb(db, stdin=gzip.decompress((run_dir / "checkpoint.sql.gz").read_bytes()))
        out = {"run": run_dir.name, **standings(run_dir), "combat": combat(db)}
    finally:
        mariadb(None, f"DROP DATABASE IF EXISTS {db}")
    ledger = run_dir / "llm_ledger.json"
    if ledger.exists():
        out["llm_usd"] = round(json.loads(ledger.read_text()).get("spent_usd", 0), 2)
    text = json.dumps(out, indent=1, ensure_ascii=False)
    if args.json:
        pathlib.Path(args.json).write_text(text)
    print(text)


if __name__ == "__main__":
    main()
