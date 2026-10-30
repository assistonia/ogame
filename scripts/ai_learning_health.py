#!/usr/bin/env python3
"""Audit AI colony health, action feedback coverage, and memory integrity."""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bots.ai_agents.action_feedback import lifecycle_status


DEFAULT_STATE_ROOT = ROOT / "bots" / "ai_agents" / "state"
DEFAULT_AGENTS = (
    *(f"ai{index:02d}" for index in range(1, 11)),
    "pirate01",
    "explorer01",
)
MEMORY_ARTIFACT_RE = re.compile(
    r"^(?:@@|\+@@|diff --git|index [0-9a-f]|--- |\+\+\+ )|0\.000000e\+00",
    re.MULTILINE,
)


def _json(path: pathlib.Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _planets(snapshot: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    empire = snapshot.get("empire")
    planets = empire.get("planets") if isinstance(empire, Mapping) else None
    if not isinstance(planets, Sequence) or isinstance(planets, (str, bytes)):
        return []
    return [planet for planet in planets if isinstance(planet, Mapping)]


def audit_agent_state(agent_dir: pathlib.Path) -> dict[str, Any]:
    snapshot = _json(agent_dir / "empire_status.json")
    feedback = _json(agent_dir / "action_feedback.json")
    planets = _planets(snapshot)
    outcomes = feedback.get("outcomes")
    outcomes = (
        [row for row in outcomes if isinstance(row, Mapping)]
        if isinstance(outcomes, list)
        else []
    )
    status_counts = Counter(str(row.get("status") or "unknown") for row in outcomes)
    lifecycle_counts = Counter(
        (
            str(row["lifecycle"])
            if row.get("lifecycle")
            else lifecycle_status(row.get("status"))
        )
        for row in outcomes
    )
    action_counts = Counter(str(row.get("action") or "unknown") for row in outcomes)
    negative = [
        planet
        for planet in planets
        if isinstance(planet.get("energy_balance"), (int, float))
        and float(planet["energy_balance"]) < 0
    ]
    throttled = [
        planet
        for planet in planets
        if isinstance(planet.get("production_factor_pct"), (int, float))
        and float(planet["production_factor_pct"]) < 100
    ]
    zero_mines = [
        planet
        for planet in planets
        if isinstance(planet.get("mines"), list)
        and sum(
            int(value or 0)
            for value in planet["mines"]
            if isinstance(value, (int, float))
        )
        == 0
    ]
    try:
        memory = (agent_dir / "memory.md").read_text(encoding="utf-8")
    except OSError:
        memory = ""
    return {
        "agent": agent_dir.name,
        "planet_count": len(planets),
        "negative_energy_planets": len(negative),
        "throttled_planets": len(throttled),
        "zero_mine_planets": len(zero_mines),
        "production_factor_average": (
            round(
                sum(float(planet.get("production_factor_pct") or 0) for planet in planets)
                / len(planets),
                2,
            )
            if planets
            else None
        ),
        "feedback_count": len(outcomes),
        "development_feedback_count": action_counts["development"],
        "feedback_statuses": dict(sorted(status_counts.items())),
        "feedback_lifecycles": dict(sorted(lifecycle_counts.items())),
        "memory_artifact_count": len(MEMORY_ARTIFACT_RE.findall(memory)),
        "snapshot_available": bool(snapshot),
        "feedback_available": bool(feedback),
    }


def aggregate(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    lifecycle_counts: Counter[str] = Counter()
    for row in rows:
        lifecycles = row.get("feedback_lifecycles")
        if isinstance(lifecycles, Mapping):
            lifecycle_counts.update(
                {
                    str(name): int(count)
                    for name, count in lifecycles.items()
                    if isinstance(count, (int, float)) and not isinstance(count, bool)
                }
            )
    return {
        "agent_count": len(rows),
        "planet_count": sum(int(row.get("planet_count") or 0) for row in rows),
        "negative_energy_planets": sum(
            int(row.get("negative_energy_planets") or 0) for row in rows
        ),
        "throttled_planets": sum(
            int(row.get("throttled_planets") or 0) for row in rows
        ),
        "zero_mine_planets": sum(int(row.get("zero_mine_planets") or 0) for row in rows),
        "feedback_count": sum(int(row.get("feedback_count") or 0) for row in rows),
        "development_feedback_count": sum(
            int(row.get("development_feedback_count") or 0) for row in rows
        ),
        "feedback_lifecycles": dict(sorted(lifecycle_counts.items())),
        "memory_artifact_count": sum(
            int(row.get("memory_artifact_count") or 0) for row in rows
        ),
        "agents_without_snapshot": [
            str(row.get("agent"))
            for row in rows
            if row.get("snapshot_available") is not True
        ],
        "agents_without_feedback": [
            str(row.get("agent"))
            for row in rows
            if row.get("feedback_available") is not True
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", type=pathlib.Path, default=DEFAULT_STATE_ROOT)
    parser.add_argument("--agents", nargs="*", default=list(DEFAULT_AGENTS))
    parser.add_argument("--fail-on-memory-artifacts", action="store_true")
    args = parser.parse_args()
    rows = [audit_agent_state(args.state_root / name) for name in args.agents]
    result = {"summary": aggregate(rows), "agents": rows}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.fail_on_memory_artifacts and result["summary"]["memory_artifact_count"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
