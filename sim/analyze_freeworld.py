#!/usr/bin/env python3
"""What the free (self-directed) LLM players did in a free-world run.

  python3 -m sim.analyze_freeworld runs/freeworld [--json out.json]

Reports, from the final checkpoint and metrics.db:
  - each free player's goals over time (first, a few in between, last)
  - who their priority_targets / excluded_systems pointed at (farm, bot group)
  - the attack matrix between groups
  - reactions: after being attacked, did the victim target the attacker,
    avoid the attacker's system, or attack back?
"""
from __future__ import annotations

import argparse
import collections
import gzip
import json
import pathlib
import sqlite3

from sim.analyze_run import mariadb, rows, standings, combat

FREE = ("fr", "fw")


def group_of(name: str) -> str:
    if name.startswith("farm"):
        return "farm"
    return name[:2]


def load_calls(run_dir: pathlib.Path) -> dict[str, list[dict]]:
    conn = sqlite3.connect(f"file:{run_dir / 'metrics.db'}?mode=ro", uri=True)
    out: dict[str, list[dict]] = collections.defaultdict(list)
    for turn, clock, acc, ok, note, policy in conn.execute(
            "SELECT turn, clock, account, ok, note, policy FROM llm_calls ORDER BY turn"):
        if not acc.startswith(FREE) or not ok or not policy:
            continue
        p = json.loads(policy)
        t = p.get("targeting", {})
        out[acc].append({"turn": turn, "clock": clock, "note": note, "goal": p.get("goal", ""),
                         "priority": t.get("priority_targets", []),
                         "excluded": [tuple(x) for x in t.get("excluded_systems", [])]})
    return out


def analyze(run_dir: pathlib.Path, db: str) -> dict:
    users = {int(i): n for i, n in rows(db, "SELECT id, username FROM users")}
    owner_at: dict[str, str] = {}
    systems: dict[tuple, set] = collections.defaultdict(set)
    for uid, g, s, p in rows(db, "SELECT user_id, galaxy, system, planet FROM planets WHERE destroyed=0"):
        name = users.get(int(uid), "?")
        owner_at[f"{g}:{s}:{p}"] = name
        systems[(int(g), int(s))].add(name)

    attacks = []          # (time, attacker, victim)
    for owner, target, dep in rows(db, """
            SELECT f.user_id, p.user_id, f.time_departure FROM fleet_missions f
            JOIN planets p ON p.id = f.planet_id_to
            WHERE f.parent_id IS NULL AND f.mission_type = 1 AND p.user_id <> f.user_id"""):
        attacks.append((int(dep), users.get(int(owner), "?"), users.get(int(target), "?")))
    attacks.sort()

    matrix = collections.Counter((group_of(a), group_of(v)) for _, a, v in attacks)
    pairs = collections.Counter((a, v) for _, a, v in attacks if group_of(v) != "farm")

    calls = load_calls(run_dir)
    players = {}
    for acc, cs in sorted(calls.items()):
        tgt_groups = collections.Counter()
        for c in cs:
            for coord in c["priority"]:
                tgt_groups[group_of(owner_at.get(coord, "empty"))] += 1
        picks = [0, len(cs) // 4, len(cs) // 2, 3 * len(cs) // 4, len(cs) - 1]
        players[acc] = {
            "goals": [{"turn": cs[i]["turn"], "goal": cs[i]["goal"]} for i in sorted(set(picks))],
            "distinct_goals": len({c["goal"] for c in cs}),
            "priority_target_owner_groups": dict(tgt_groups),
            "bot_targets": sorted({owner_at[c] for x in cs for c in x["priority"]
                                   if c in owner_at and group_of(owner_at[c]) != "farm"}),
            "excluded_ever": sorted({f"{g}:{s}" for x in cs for g, s in x["excluded"]}),
            "attacked_by": dict(collections.Counter(a for _, a, v in attacks if v == acc)),
            "attacked": dict(collections.Counter(v for _, a, v in attacks
                                                 if a == acc and group_of(v) != "farm")),
            "farm_raids": sum(1 for _, a, v in attacks if a == acc and group_of(v) == "farm"),
        }

    # reactions: for each (attacker -> free victim) first hit, what did the victim's next policy do
    reactions = []
    seen = set()
    for t, a, v in attacks:
        if not v.startswith(FREE) or (a, v) in seen or group_of(a) == "farm":
            continue
        seen.add((a, v))
        nxt = next((c for c in calls.get(v, []) if c["clock"] > t), None)
        a_coords = {c for c, n in owner_at.items() if n == a}
        a_systems = {k for k, names in systems.items() if a in names}
        reactions.append({
            "attacker": a, "victim": v, "first_hit_clock": t,
            "next_policy_turn": nxt and nxt["turn"],
            "targeted_attacker": bool(nxt and a_coords & set(nxt["priority"])),
            "avoided_attacker_system": bool(nxt and a_systems & set(nxt["excluded"])),
            "hit_back": any(t2 > t and a2 == v and v2 == a for t2, a2, v2 in attacks),
            "note": nxt and nxt["note"],
        })
    return {"matrix": {f"{a}->{v}": n for (a, v), n in matrix.most_common()},
            "bot_pairs": {f"{a}->{v}": n for (a, v), n in pairs.most_common(40)},
            "players": players, "reactions": reactions}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument("--json")
    args = ap.parse_args()
    run_dir = pathlib.Path(args.run_dir)
    db = "fw_" + run_dir.name.replace("-", "_")
    mariadb(None, f"DROP DATABASE IF EXISTS {db}; CREATE DATABASE {db}")
    try:
        mariadb(db, stdin=gzip.decompress((run_dir / "checkpoint.sql.gz").read_bytes()))
        out = {"run": run_dir.name, **standings(run_dir), "combat": combat(db), **analyze(run_dir, db)}
    finally:
        mariadb(None, f"DROP DATABASE IF EXISTS {db}")
    text = json.dumps(out, indent=1, ensure_ascii=False)
    if args.json:
        pathlib.Path(args.json).write_text(text)
    print(text)


if __name__ == "__main__":
    main()
