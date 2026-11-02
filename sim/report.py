#!/usr/bin/env python3
"""Summarise a run: group/personality standings, score trajectories, LLM log.

  python3 -m sim.report runs/pilot            # markdown to stdout
  python3 -m sim.report runs/pilot --json out.json
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
import statistics as st


def load(run_dir: pathlib.Path) -> dict:
    conn = sqlite3.connect(run_dir / "metrics.db")
    last = conn.execute("SELECT MAX(turn) FROM scores").fetchone()[0]
    rows = conn.execute(
        "SELECT account, grp, personality, general, economy, research, military, planets "
        "FROM scores WHERE turn=? AND grp NOT IN ('other')", (last,)).fetchall()
    final = [dict(zip(("account", "group", "personality", "general", "economy", "research",
                       "military", "planets"), r)) for r in rows]
    traj = {}
    for turn, grp, avg in conn.execute(
            "SELECT turn, grp, AVG(general) FROM scores WHERE grp NOT IN ('other','farm') "
            "GROUP BY turn, grp ORDER BY turn"):
        traj.setdefault(grp, []).append([turn, round(avg, 1)])
    llm = [dict(zip(("turn", "account", "ok", "cost", "personality", "note", "error"), r))
           for r in conn.execute("SELECT turn, account, ok, cost, personality, note, error FROM llm_calls "
                                 "ORDER BY turn, account")]
    turns = conn.execute("SELECT COUNT(*), AVG(wall_sec), SUM(bot_errors), SUM(missions), "
                         "SUM(advance_errors) FROM turns").fetchone()
    ledger = {}
    if (run_dir / "llm_ledger.json").exists():
        ledger = json.loads((run_dir / "llm_ledger.json").read_text())
    return {"last_turn": last, "final": final, "trajectory": traj, "llm": llm, "ledger": ledger,
            "turns": dict(zip(("count", "avg_wall_sec", "bot_errors", "missions", "advance_errors"), turns))}


def group_stats(final: list[dict]) -> dict:
    out = {}
    for grp in sorted({r["group"] for r in final}):
        scores = [r["general"] for r in final if r["group"] == grp]
        out[grp] = {"n": len(scores), "mean": round(st.mean(scores), 1),
                    "median": st.median(scores), "max": max(scores), "min": min(scores)}
    return out


def markdown(data: dict) -> str:
    final = data["final"]
    lines = [f"# Run report (last scored turn {data['last_turn']})", ""]
    t = data["turns"]
    lines += [f"- turns played: {t['count']}, avg wall time/turn: {t['avg_wall_sec']:.1f}s",
              f"- bot errors: {t['bot_errors']}, fleet missions settled: {t['missions']}, "
              f"advance errors: {t['advance_errors']}",
              f"- LLM: {data['ledger'].get('calls', 0)} calls, ${data['ledger'].get('spent_usd', 0):.3f}", ""]
    lines += ["## Groups", "", "| group | n | mean | median | min | max |", "|---|---|---|---|---|---|"]
    for grp, s in group_stats(final).items():
        lines.append(f"| {grp} | {s['n']} | {s['mean']} | {s['median']} | {s['min']} | {s['max']} |")
    lines += ["", "## Accounts", "", "| account | group | personality | total | eco | research | military | planets |",
              "|---|---|---|---|---|---|---|---|"]
    for r in sorted(final, key=lambda r: -r["general"]):
        lines.append(f"| {r['account']} | {r['group']} | {r['personality'] or ''} | {r['general']} | "
                     f"{r['economy']} | {r['research']} | {r['military']} | {r['planets']} |")
    lines += ["", "## LLM decisions", ""]
    for c in data["llm"]:
        msg = c["note"] if c["ok"] else f"FAILED: {c['error']}"
        lines.append(f"- t{c['turn']} {c['account']} ({c['personality']}, ${c['cost']:.3f}): {msg}")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument("--json")
    args = ap.parse_args()
    data = load(pathlib.Path(args.run_dir))
    if args.json:
        pathlib.Path(args.json).write_text(json.dumps(data, ensure_ascii=False, indent=1))
    print(markdown(data))


if __name__ == "__main__":
    main()
