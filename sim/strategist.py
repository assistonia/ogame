"""LLM strategist: reads an account's situation and writes its policy JSON.

The rule engine (bots/ai_agents/policy_engine.py) executes the policy every
turn. Control-group accounts get the same initial policy but it never changes,
so the only difference between the groups is who updates the policy.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import pathlib
import re
from typing import Any

from bots.ai_agents import action_feedback, empire_snapshot, policy_engine

PERSONALITIES = ("miner", "raider", "turtle", "explorer", "hybrid")

SYSTEM_PROMPT = """You are the strategy brain of one player in a simulated OGame universe (OGameX server).
You never act in the game directly. A deterministic rule engine executes your policy every turn
(one turn = a few in-game minutes) until you write the next one.

Goal: maximise your empire's long-run total score (economy + research + military) and survive.
Your persona is a preference, not a rule. Change strategy when the evidence says so.

Facts about the engine:
- personality picks the base playbook: miner (economy), raider (attack inactive/weak players for loot),
  turtle (defense heavy), explorer (expeditions), hybrid (economy + opportunistic raids).
- build_priority is tried in order each turn; items blocked by resources/requirements are skipped.
  Energy shortage silently cuts mine output: keep solar_plant up or set energy_target > 0.
  Full storage stops production: build metal_store / crystal_store / deuterium_store in time.
- ship_strategy.reinvestment_ratio = share of liquid resources spent on ships each turn (0-1);
  weights = target fleet value mix by ship id. defense_ratio = share of surplus spent on defense.
- "recent_engine_feedback" lists what the engine actually did or refused. If an action you asked for
  keeps failing, fix the cause (requirements, energy, storage, slots) instead of repeating it.
- Safety limits are enforced by the engine regardless of what you write.

Output ONLY one JSON object, no prose, no code fences."""

# Free-world variant: no assigned role and no score target. The player decides
# what it wants and may name whom to hit first and whom to leave alone.
FREE_SYSTEM_PROMPT = SYSTEM_PROMPT.replace(
    """Goal: maximise your empire's long-run total score (economy + research + military) and survive.
Your persona is a preference, not a rule. Change strategy when the evidence says so.""",
    """Nobody assigns you a goal or a role. Decide for yourself what kind of player you want to be and
what you want from this universe: grow quietly, dominate, hunt, protect yourself, take revenge, spare
some neighbours, explore, or anything else. You may change your mind. Write your current aim in "goal";
you will see it again next time. There is no chat between players: other players only see what you do.""",
) + """

Extra levers you have in this world:
- targeting.priority_targets: list of "galaxy:system:position" planets your raid engine tries first
  (it still needs a fleet that can win and will skip targets it cannot reach).
- targeting.excluded_systems: list of [galaxy, system] your engine will never attack (a one-sided truce).
- "world" in the situation lists every player's planets, score and military, and who attacked whom."""

# Free world with messages: players can talk and can promise not to attack.
CHAT_SYSTEM_PROMPT = FREE_SYSTEM_PROMPT.replace(
    "There is no chat between players: other players only see what you do.",
    """Players can send each other short messages (see "messages" below and "world.inbox").
Messages are cheap talk: nothing forces anyone to keep a promise, and some players may never answer.""",
) + """
- targeting.excluded_players: list of player names your engine will never spy on or attack.
  Everyone can see who declared whom off-limits ("world.no_attack_declarations").
- messages: up to 3 messages per policy update, each to one player name or "all".
  They are delivered at the next policy update of the recipients (about 12 game hours)."""

CHAT_SCHEMA_EXTRA = """
Additional fields in this world:
{
  "goal": "one or two sentences in Korean: what you want and why",
  "targeting": {"priority_targets": ["g:s:p", ...], "excluded_systems": [[g, s], ...],
                "excluded_players": ["player name", ...]},
  "messages": [{"to": "player name or all", "text": "Korean, at most 200 characters"}]
}"""

FREE_SCHEMA_EXTRA = """
Additional fields in this world:
{
  "goal": "one or two sentences in Korean: what you want and why",
  "targeting": {"priority_targets": ["g:s:p", ...], "excluded_systems": [[g, s], ...]}
}"""

POLICY_SCHEMA = """Policy JSON fields (omit a field to keep the engine default):
{
  "personality": "miner|raider|turtle|explorer|hybrid",
  "build_priority": ["metal_mine","crystal_mine","deuterium_synthesizer","solar_plant","fusion_plant",
                     "robot_factory","nano_factory","shipyard","research_lab","metal_store","crystal_store",
                     "deuterium_store", ...],
  "research_priority": [113, 120, 106, 108, 109, 110, 111, 115, 117, 118, 124, ...],
      // 106 espionage, 108 computer, 109 weapons, 110 shielding, 111 armour, 113 energy, 114 hyperspace,
      // 115 combustion drive, 117 impulse drive, 118 hyperspace drive, 120 laser, 121 ion, 122 plasma,
      // 124 astrophysics (colonies + expedition slots), 199 graviton
  "ship_strategy": {"reinvestment_ratio": 0.0-1.0,
                    "doctrine": "balanced|cargo_raider|debris_hunter|expeditioneer",
                    "weights": {"202": n, "203": n, "204": n, "205": n, "206": n, "207": n, "209": n, "210": n}},
      // 202 small cargo, 203 large cargo, 204 light fighter, 205 heavy fighter, 206 cruiser,
      // 207 battleship, 208 colony ship, 209 recycler, 210 espionage probe, 212 solar satellite
  "aggression": {"min_loot": int, "max_active_missions": int},
  "defense_ratio": 0.0-1.0,
  "defense_strategy": {"mode": "off|adaptive|fortress", "stored_loot_multiplier": float, "production_hours": float},
  "fleetsave": {"mode": "off|adaptive|counter|always"},
  "energy_target": int,
  "note": "one sentence in Korean: what you changed and why"
}"""


def _iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def initial_policy(personality: str, now_ts: float, ttl_hours: float) -> dict[str, Any]:
    """Policy every policy-driven account starts from (same for both groups)."""
    assert personality in PERSONALITIES
    return {
        "strategy_version": 3,
        "updated_at": _iso(now_ts),
        "ttl_hours": ttl_hours,
        "personality": personality,
        "autonomy": "full",
        "targeting": copy.deepcopy(policy_engine.FULL_AUTONOMY_TARGETING),
        "limits": copy.deepcopy(policy_engine.FULL_AUTONOMY_LIMITS),
        "fleetsave": {"mode": "adaptive"},
        "defense_strategy": {"mode": "adaptive", "stored_loot_multiplier": 0.5,
                             "production_hours": 6.0, "rebuild_on_attack": True},
        "secondary_goals": {"deathstar": {"mode": "pause", "reason": "sim v2: out of scope"}},
        "plans_version": 1,
        "plans": {"version": 1, "colonize": [], "transfers": [], "development": [], "production": [],
                  "moon_development": [], "phalanx_scans": [], "jump_gate": [], "intercepts": []},
        "note": f"initial {personality} policy",
    }


def _compact_snapshot(email: str) -> dict[str, Any]:
    snap = empire_snapshot.build_empire_snapshot(email)
    snap.pop("secondary_goals", None)
    snap.pop("worlds", None)  # duplicate of planets + moons
    ns = snap.get("normalized_state")
    if isinstance(ns, dict) and isinstance(ns.get("worlds"), list):
        # worlds = planets + moons; keep only the moons so nothing is lost
        ns["worlds"] = [w for w in ns["worlds"] if w.get("world_type") != "planet"]
    return snap


def _recent_feedback(account: str, limit: int = 15) -> list[dict[str, Any]]:
    try:
        rows = action_feedback.load_recent_action_outcomes(account, limit)
    except Exception:  # noqa: BLE001 - feedback is optional context
        return []
    keep = ("action", "status", "lifecycle", "reason", "recorded_at", "detail")
    return [{k: r.get(k) for k in keep if r.get(k) not in (None, "", [], {})} for r in rows]


def build_prompt(*, email: str, persona: str, policy: dict, standings: dict, turn: int,
                 game_time_iso: str, engine_log: list[str], locked: list[str] | None = None,
                 world: dict | None = None, chat: bool = False) -> str:
    account = email.split("@", 1)[0]
    context = {
        "you": account,
        "game_time": game_time_iso,
        "turn": turn,
        "persona": persona,
        "standings": standings,
        "current_policy": {k: policy.get(k) for k in (
            "personality", "build_priority", "research_priority", "ship_strategy", "aggression",
            "defense_ratio", "defense_strategy", "fleetsave", "energy_target", "note", "goal") if k in policy},
        "recent_engine_turns": engine_log[-12:],
        "recent_engine_feedback": _recent_feedback(account),
        "empire": _compact_snapshot(email),
    }
    if world is not None:
        targeting = policy.get("targeting") or {}
        context["current_policy"]["targeting"] = {
            "priority_targets": targeting.get("priority_targets", []),
            "excluded_systems": targeting.get("excluded_systems", []),
            **({"excluded_players": targeting.get("excluded_players", [])} if chat else {})}
        context["world"] = world
    lock_note = ""
    if locked:
        lock_note = ("\n\nThe operator has fixed these policy fields; any change you make to them is "
                     "ignored: " + ", ".join(locked) + ".")
    return (
        POLICY_SCHEMA
        + ((CHAT_SCHEMA_EXTRA if chat else FREE_SCHEMA_EXTRA) if world is not None else "")
        + lock_note
        + "\n\nCurrent situation (JSON):\n"
        + json.dumps(context, ensure_ascii=False, separators=(",", ":"), default=str)
        + "\n\nWrite the next policy JSON now."
    )


_JSON_RE = re.compile(r"\{.*\}", re.S)


def parse_policy(text: str) -> dict[str, Any]:
    match = _JSON_RE.search(text.strip())
    if not match:
        raise ValueError("no JSON object in LLM output")
    data = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise ValueError("policy is not an object")
    return data


ALLOWED_KEYS = ("personality", "build_priority", "research_priority", "ship_strategy", "aggression",
                "defense_ratio", "defense_strategy", "fleetsave", "energy_target", "note")


def merge_policy(base: dict[str, Any], update: dict[str, Any], now_ts: float, ttl_hours: float,
                 free: bool = False) -> dict[str, Any]:
    """Apply the LLM's fields onto the previous policy file (unknown keys dropped).

    free=True also accepts "goal" and the two player-facing targeting lists;
    the engine's other targeting switches are kept as they were."""
    out = copy.deepcopy(base)
    for key in ALLOWED_KEYS:
        if key in update:
            out[key] = update[key]
    if free:
        if "goal" in update:
            out["goal"] = str(update["goal"])[:300]
        tgt = update.get("targeting")
        if isinstance(tgt, dict):
            merged_tgt = dict(out.get("targeting") or {})
            if isinstance(tgt.get("priority_targets"), list):
                merged_tgt["priority_targets"] = [str(x) for x in tgt["priority_targets"]][:20]
            if isinstance(tgt.get("excluded_systems"), list):
                merged_tgt["excluded_systems"] = [x for x in tgt["excluded_systems"]
                                                  if isinstance(x, list) and len(x) == 2][:40]
            if isinstance(tgt.get("excluded_players"), list):
                merged_tgt["excluded_players"] = [str(x)[:40] for x in tgt["excluded_players"]][:40]
            out["targeting"] = merged_tgt
    if out.get("personality") not in PERSONALITIES:
        out["personality"] = base.get("personality", "hybrid")
    out["updated_at"] = _iso(now_ts)
    out["ttl_hours"] = ttl_hours
    out["autonomy"] = "full"
    out["note"] = str(out.get("note", ""))[:200]
    return out


def parse_messages(update: dict[str, Any], sender: str, names: set[str]) -> list[tuple[str, str]]:
    """(recipient, text) pairs from an LLM update; unknown recipients dropped."""
    out = []
    msgs = update.get("messages")
    if not isinstance(msgs, list):
        return out
    for m in msgs[:3]:
        if not isinstance(m, dict):
            continue
        to, text = str(m.get("to", "")).strip(), str(m.get("text", "")).strip()[:200]
        if text and to != sender and (to == "all" or to in names):
            out.append((to, text))
    return out


def write_policy(policy_dir: pathlib.Path, account: str, policy: dict[str, Any]) -> None:
    policy_dir.mkdir(parents=True, exist_ok=True)
    tmp = policy_dir / f".{account}.json.tmp"
    tmp.write_text(json.dumps(policy, ensure_ascii=False, indent=2))
    tmp.replace(policy_dir / f"{account}.json")
