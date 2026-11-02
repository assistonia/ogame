#!/usr/bin/env python3
"""Run several seeds of one config back to back under a shared LLM budget
and a wall-clock deadline.

  python3 -m sim.run_series --template sim/configs/series.json \
      --seeds 3 4 5 6 --total-usd 90 --per-run-usd 26 --deadline 2026-11-05T04:00Z

Each seed becomes run `<template name>_s<seed>`. Before a run starts, the
spend of the series' earlier runs is summed from their ledgers; the run is
skipped when what is left is below --min-run-usd or the deadline is too close.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]


def spent(run_names: list[str]) -> float:
    total = 0.0
    for name in run_names:
        ledger = ROOT / "runs" / name / "llm_ledger.json"
        if ledger.exists():
            total += json.loads(ledger.read_text()).get("spent_usd", 0.0)
    return total


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--template", required=True)
    ap.add_argument("--seeds", type=int, nargs="+", required=True)
    ap.add_argument("--total-usd", type=float, required=True)
    ap.add_argument("--per-run-usd", type=float, required=True)
    ap.add_argument("--min-run-usd", type=float, default=15.0)
    ap.add_argument("--deadline", required=True, help="UTC, e.g. 2026-11-05T04:00Z")
    ap.add_argument("--run-hours", type=float, default=9.0, help="expected hours per run")
    args = ap.parse_args()

    deadline = dt.datetime.fromisoformat(args.deadline.replace("Z", "+00:00"))
    template = json.loads(pathlib.Path(args.template).read_text())
    done: list[str] = []
    for seed in args.seeds:
        name = f"{template['name']}_s{seed}"
        left = args.total_usd - spent(done)
        hours_left = (deadline - dt.datetime.now(dt.timezone.utc)).total_seconds() / 3600
        if left < args.min_run_usd or hours_left < args.run_hours:
            print(f"series stop before {name}: ${left:.2f} left, {hours_left:.1f}h to deadline", flush=True)
            break
        cfg = dict(template, name=name, seed=seed)
        cfg["llm"] = dict(template["llm"], budget_usd=round(min(args.per_run_usd, left), 2))
        path = ROOT / "sim" / "configs" / "generated" / f"{name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
        print(f"series start {name}: budget ${cfg['llm']['budget_usd']}, "
              f"series spent ${spent(done):.2f}", flush=True)
        rc = subprocess.call([sys.executable, "-m", "sim.run_sim", "--config", str(path), "--fresh"], cwd=ROOT)
        done.append(name)
        print(f"series end {name}: rc={rc}, series spent ${spent(done):.2f}", flush=True)
        if rc != 0:
            print("series stop: run failed", flush=True)
            break
    print("series finished", flush=True)


if __name__ == "__main__":
    main()
