"""계정별 동적 함대 투자와 약탈 기회 피드백.

고정 ``ship_targets``는 초반에 필요한 함선을 해금하는 부트스트랩일 뿐, 제국이 성장한
뒤의 종착점이 되어서는 안 된다. 이 모듈은 현재 유동자원과 계정 전체 함대 구성을 보고
매 틱 새 투자 후보를 계산한다. 성격은 함종별 투자 비율의 차이일 뿐 함선 사용 금지가
아니며, 이메일 해시로 같은 성격 안에서도 투자 강도가 조금씩 달라진다.

또한 약탈 편성기가 발견한 화물 부족을 계정별 상태로 남긴다. 다음 함선 계획과 LLM
스냅샷은 이 값을 읽어 대형/소형 화물선 비중을 실제 놓친 수익에 맞춰 올릴 수 있다.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import pathlib
import time
from collections.abc import Mapping
from typing import Any

from bots.rule_based.personality import prereqs_met
from bots.rule_based import production_capacity


STATE_DIR = pathlib.Path(__file__).resolve().parent / ".strategy_state"

# 서버 Civil/MilitaryShipObjects 가격. (metal, crystal, deuterium)
SHIP_COST: dict[int, tuple[int, int, int]] = {
    202: (2_000, 2_000, 0),       # small cargo
    203: (6_000, 6_000, 0),       # large cargo
    204: (3_000, 1_000, 0),       # light fighter
    205: (6_000, 4_000, 0),       # heavy fighter
    206: (20_000, 7_000, 2_000),  # cruiser
    207: (45_000, 15_000, 0),     # battleship
    209: (10_000, 6_000, 2_000),  # recycler
    210: (0, 1_000, 0),           # espionage probe
    211: (50_000, 25_000, 15_000),
    213: (60_000, 50_000, 15_000),
    215: (30_000, 40_000, 15_000),
    218: (85_000, 55_000, 20_000),
    219: (8_000, 15_000, 8_000),
}

# 함선 가치 중 각 함종에 둘 장기 비율. 합은 정규화되므로 읽기 쉬운 상대 가중치다.
# 모든 성격에 화물·정찰·전투·수거 능력이 존재하고 강조점만 다르다.
PERSONALITY_SHIP_WEIGHTS: dict[str, dict[int, float]] = {
    "miner": {
        203: 34, 202: 16, 204: 10, 205: 5, 206: 10, 207: 5,
        209: 6, 210: 6, 215: 4, 219: 4,
    },
    "raider": {
        203: 25, 202: 15, 204: 15, 205: 7, 206: 12, 207: 8,
        209: 5, 210: 5, 215: 8,
    },
    "turtle": {
        203: 30, 202: 15, 204: 10, 205: 6, 206: 10, 207: 7,
        209: 6, 210: 6, 215: 5, 219: 5,
    },
    "explorer": {
        203: 36, 202: 10, 204: 8, 205: 5, 206: 8, 207: 4,
        209: 5, 210: 6, 215: 4, 219: 14,
    },
    "explorer_aggressive": {
        203: 36, 202: 8, 204: 8, 205: 4, 206: 8, 207: 4,
        209: 5, 210: 5, 215: 4, 219: 18,
    },
    "hybrid": {
        203: 28, 202: 14, 204: 12, 205: 6, 206: 11, 207: 6,
        209: 6, 210: 6, 215: 6, 219: 5,
    },
}

# 현재 유동자원 중 이번 함선 큐가 사용할 수 있는 비율. 고정 수량 상한이 아니라
# 매 틱의 기회비용이며, 이메일별 변동은 아래 individual_reinvestment_ratio가 준다.
PERSONALITY_REINVESTMENT: dict[str, float] = {
    "miner": 0.12,
    "raider": 0.45,
    "turtle": 0.10,
    "explorer": 0.30,
    "explorer_aggressive": 0.40,
    "hybrid": 0.24,
}

DOCTRINES = ("cargo_raider", "debris_hunter", "expeditioneer", "balanced")

# Pathfinder expedition bonuses do not stack: one ship in each expedition is
# sufficient.  Keep one additional reserve, then redirect further investment to
# cargo/combat instead of letting an LLM weight accumulate hundreds of idle PFs.
PATHFINDER = 219
ASTROPHYSICS = 124
DISCOVERER_EXPEDITION_SLOTS = 2
PATHFINDER_RESERVE = 1

SMALL_CARGO = 202
LARGE_CARGO = 203
COMBAT_SHIPS = frozenset({204, 205, 206, 207, 211, 213, 215, 218})
COMBAT_GROWTH_ORDER = (215, 206, 207, 218, 213, 211, 205, 204)
MIN_COMBAT_GROWTH_WEIGHT = 0.20

# 공용 연구 마일스톤이 끝난 뒤에도 연구가 멈추지 않게 하는 성격별 상대 우선순위.
# 선행조건을 통과한 것 중 현재 레벨이 낮은 연구가 먼저 선택된다.
STRATEGIC_RESEARCH: dict[str, tuple[int, ...]] = {
    "miner": (108, 115, 117, 106, 109, 111, 110, 113, 120, 121, 114, 118),
    "raider": (109, 111, 110, 115, 117, 118, 106, 108, 120, 121, 114, 113),
    "turtle": (110, 111, 109, 120, 121, 113, 108, 115, 117, 118, 106, 114),
    "explorer": (115, 117, 118, 108, 106, 110, 111, 109, 114, 113, 120, 121),
    "explorer_aggressive": (115, 117, 118, 108, 106, 110, 111, 109, 114, 113, 120, 121),
    "hybrid": (108, 115, 117, 109, 111, 110, 106, 118, 113, 120, 121, 114),
}


def _stable_fraction(email: str, salt: str) -> float:
    digest = hashlib.sha256(f"{salt}:{email.lower()}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / float(2**64 - 1)


def individual_reinvestment_ratio(email: str | None, personality_key: str) -> float:
    """같은 성격 안에서도 ±20% 다른, 재현 가능한 함대 투자율."""
    base = PERSONALITY_REINVESTMENT.get(personality_key, 0.20)
    if not email:
        return base
    multiplier = 0.8 + 0.4 * _stable_fraction(email, "fleet-reinvestment")
    return max(0.0, min(1.0, base * multiplier))


def individual_doctrine(email: str | None, personality_key: str) -> str:
    """계정마다 재현 가능한 전투/수익 교리.

    성격은 기본 확률에만 영향을 주며 어떤 교리도 금지하지 않는다. 레이더는 잔해/화물형,
    탐험가는 원정형 확률이 높지만 같은 성격 안에서도 서로 다른 선택을 한다.
    """
    if not email:
        return "balanced"
    r = _stable_fraction(email, "combat-doctrine")
    if personality_key == "raider":
        cuts = (0.35, 0.75, 0.82)
    elif personality_key.startswith("explorer"):
        cuts = (0.15, 0.30, 0.75)
    elif personality_key == "miner":
        cuts = (0.20, 0.35, 0.55)
    elif personality_key == "turtle":
        cuts = (0.10, 0.30, 0.45)
    else:
        cuts = (0.25, 0.50, 0.70)
    if r < cuts[0]:
        return "cargo_raider"
    if r < cuts[1]:
        return "debris_hunter"
    if r < cuts[2]:
        return "expeditioneer"
    return "balanced"


def _doctrine_weights(weights: dict[int, float], doctrine: str) -> dict[int, float]:
    adjusted = dict(weights)
    if doctrine == "debris_hunter":
        for tid in (204, 205, 206, 207, 211, 213, 215, 218):
            adjusted[tid] = adjusted.get(tid, 0.0) * 1.65
        adjusted[209] = adjusted.get(209, 0.0) * 2.0
        for tid in (202, 203):
            adjusted[tid] = adjusted.get(tid, 0.0) * 0.65
    elif doctrine == "cargo_raider":
        adjusted[202] = adjusted.get(202, 0.0) * 1.35
        adjusted[203] = adjusted.get(203, 0.0) * 1.75
    elif doctrine == "expeditioneer":
        adjusted[203] = adjusted.get(203, 0.0) * 1.35
        adjusted[219] = adjusted.get(219, 0.0) * 2.0
    total = sum(adjusted.values())
    return {tid: value / total for tid, value in adjusted.items()} if total > 0 else weights


def expedition_pathfinder_target(levels: Mapping[int, int]) -> int:
    """PF production target: one per Discoverer expedition slot plus one spare."""
    astro = max(0, int(levels.get(ASTROPHYSICS, 0) or 0))
    slots = math.isqrt(astro) + DISCOVERER_EXPEDITION_SLOTS
    return max(1, slots + PATHFINDER_RESERVE)


def _keep_combat_growth(
    weights: dict[int, float], levels: Mapping[int, int]
) -> dict[int, float]:
    """Keep a raid/defense combat lane when a policy omits every combat ship.

    Cargo and Pathfinder investment grows income, but an empire made entirely
    of civilian ships is a zero-loss raid target.  Preserve a modest combat
    value lane for deterrence, counter-defense and profitable attacks without
    overriding policies that already chose their own combat mix.
    """
    if any(weights.get(tid, 0.0) > 0 for tid in COMBAT_SHIPS):
        return weights
    combat_tid = next(
        (tid for tid in COMBAT_GROWTH_ORDER if prereqs_met(tid, dict(levels))),
        None,
    )
    if combat_tid is None:
        return weights or {LARGE_CARGO: 1.0}
    if not weights:
        return {LARGE_CARGO: 1.0 - MIN_COMBAT_GROWTH_WEIGHT,
                combat_tid: MIN_COMBAT_GROWTH_WEIGHT}
    adjusted = {
        tid: weight * (1.0 - MIN_COMBAT_GROWTH_WEIGHT)
        for tid, weight in weights.items()
    }
    adjusted[combat_tid] = MIN_COMBAT_GROWTH_WEIGHT
    return adjusted


def _cap_surplus_pathfinder_weight(
    weights: dict[int, float],
    levels: Mapping[int, int],
    account_ship_totals: Mapping[int, int],
) -> dict[int, float]:
    """Stop PF overproduction once every expedition slot and a spare are covered.

    The expedition engine uses at most one Pathfinder per mission and the server
    bonus is boolean, not proportional to PF count.  Removing only the surplus PF
    weight preserves the LLM's remaining fleet mix and naturally redirects its
    current reinvestment budget to cargo/escort ships.
    """
    if int(account_ship_totals.get(PATHFINDER, 0) or 0) < expedition_pathfinder_target(levels):
        return weights
    adjusted = {tid: weight for tid, weight in weights.items() if tid != PATHFINDER}
    total = sum(adjusted.values())
    if total <= 0:
        return {}
    return {tid: weight / total for tid, weight in adjusted.items()}


def normalize_weights(raw: Any, personality_key: str) -> dict[int, float]:
    source = raw if isinstance(raw, Mapping) else PERSONALITY_SHIP_WEIGHTS.get(
        personality_key, PERSONALITY_SHIP_WEIGHTS["hybrid"]
    )
    clean: dict[int, float] = {}
    for key, value in source.items():
        try:
            tid = int(key)
            weight = float(value)
        except (TypeError, ValueError):
            continue
        if tid in SHIP_COST and math.isfinite(weight) and weight > 0:
            clean[tid] = weight
    total = sum(clean.values())
    if total <= 0:
        return {}
    return {tid: value / total for tid, value in clean.items()}


def clean_ship_strategy(raw: Any) -> dict[str, Any]:
    """LLM 정책의 동적 함대 투자 설정을 방어적으로 정규화."""
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, Any] = {}
    ratio = raw.get("reinvestment_ratio")
    if isinstance(ratio, (int, float)) and not isinstance(ratio, bool):
        out["reinvestment_ratio"] = max(0.0, min(1.0, float(ratio)))
    if isinstance(raw.get("weights"), Mapping):
        weights = normalize_weights(raw.get("weights"), "hybrid")
        if weights:
            out["weights"] = weights
    doctrine = raw.get("doctrine")
    if doctrine in DOCTRINES:
        out["doctrine"] = doctrine
    horizon = raw.get("queue_horizon_seconds")
    if isinstance(horizon, (int, float)) and not isinstance(horizon, bool):
        if math.isfinite(float(horizon)) and float(horizon) > 0:
            out["queue_horizon_seconds"] = max(
                production_capacity.MIN_QUEUE_HORIZON_SECONDS,
                min(production_capacity.MAX_QUEUE_HORIZON_SECONDS, int(horizon)),
            )
    return out


def _state_path(email: str) -> pathlib.Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in email)
    return STATE_DIR / f"{safe}.json"


def _read_state(email: str) -> dict[str, Any]:
    try:
        data = json.loads(_state_path(email).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def record_raid_opportunity(
    email: str,
    *,
    potential_loot: int,
    realized_loot: int,
    target: tuple[int, int, int] | None = None,
    outcome: str = "evaluated",
) -> None:
    """편성 평가 결과를 다음 생산 틱과 LLM에 전달한다."""
    try:
        potential = max(0, int(potential_loot))
        realized = max(0, min(potential, int(realized_loot)))
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        state = _read_state(email)
        state.update({
            "updated_at": time.time(),
            "target": ":".join(map(str, target)) if target else None,
            "potential_loot": potential,
            "realized_loot": realized,
            "cargo_shortfall": max(0, potential - realized),
            "cargo_saturation": (
                round(realized / potential, 4) if potential > 0 else 1.0
            ),
            "outcome": str(outcome)[:80],
        })
        path = _state_path(email)
        tmp = path.with_suffix(f".tmp.{os.getpid()}")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except (OSError, TypeError, ValueError):
        pass


def opportunity_snapshot(email: str, *, max_age_sec: int = 6 * 3600) -> dict[str, Any]:
    state = _read_state(email)
    updated = float(state.get("updated_at", 0.0) or 0.0)
    if updated <= 0 or time.time() - updated > max_age_sec:
        return {"status": "none"}
    return {"status": "ready", **state}


def _cargo_biased_weights(
    weights: dict[int, float], opportunity: Mapping[str, Any] | None
) -> dict[int, float]:
    """최근 화물 부족이 있으면 cargo 비중을 손실 비율만큼 동적으로 올린다."""
    if not opportunity or opportunity.get("status") != "ready":
        return weights
    potential = float(opportunity.get("potential_loot", 0) or 0)
    shortfall = float(opportunity.get("cargo_shortfall", 0) or 0)
    if potential <= 0 or shortfall <= 0:
        return weights
    boost = min(2.0, shortfall / potential * 2.0)
    adjusted = dict(weights)
    # 큰 약탈은 대형화물이 비용 대비 효율적이고, 소형은 소액/속도 유연성을 유지한다.
    adjusted[203] = adjusted.get(203, 0.0) * (1.0 + boost)
    adjusted[202] = adjusted.get(202, 0.0) * (1.0 + boost * 0.35)
    total = sum(adjusted.values())
    return {tid: value / total for tid, value in adjusted.items()} if total > 0 else weights


def _affordable(cost: tuple[int, int, int], budget: Mapping[str, int]) -> int:
    limits: list[int] = []
    for resource, unit in zip(("metal", "crystal", "deuterium"), cost):
        if unit > 0:
            limits.append(max(0, int(budget.get(resource, 0))) // unit)
    return min(limits) if limits else 0


def strategic_ship_candidates(
    personality_key: str,
    levels: Mapping[int, int],
    resource_state: Mapping[str, Any] | None,
    account_ship_totals: Mapping[int, int] | None,
    *,
    email: str | None = None,
    strategy: Mapping[str, Any] | None = None,
) -> list[tuple[str, int, int]]:
    """현재 자원·구성에서 가장 부족한 함종부터 동적 건조 후보를 만든다.

    고정 목표 수량은 사용하지 않는다. 이번 틱 투자 예산과 원하는 가치 비율의 차이가 곧
    수요이며, 실제 자원이 줄거나 구성이 채워지면 자동으로 다른 함종/경제로 넘어간다.
    """
    if not resource_state or account_ship_totals is None or int(levels.get(21, 0)) < 1:
        return []
    configured = strategy if isinstance(strategy, Mapping) else {}
    ratio = configured.get("reinvestment_ratio")
    if not isinstance(ratio, (int, float)) or isinstance(ratio, bool):
        ratio = individual_reinvestment_ratio(email, personality_key)
    ratio = max(0.0, min(1.0, float(ratio)))
    if ratio <= 0:
        return []

    raw_weights = configured.get("weights")
    weights = normalize_weights(raw_weights, personality_key)
    if not weights:
        return []
    doctrine = configured.get("doctrine")
    if doctrine not in DOCTRINES:
        doctrine = individual_doctrine(email, personality_key)
    weights = _doctrine_weights(weights, doctrine)
    weights = _cap_surplus_pathfinder_weight(weights, levels, account_ship_totals)
    weights = _keep_combat_growth(weights, levels)
    if not weights:
        return []
    opportunity = opportunity_snapshot(email) if email else {"status": "none"}
    weights = _cargo_biased_weights(weights, opportunity)

    # Preserve the cheapest next economic upgrade before fleet reinvestment.
    # Without this floor, low-income raiders spend each new crystal on fighters
    # and can never accumulate enough for a mine, research, or colony bootstrap.
    from bots.shared.game_objects import object_cost

    growth_costs = [
        object_cost(tid, int(levels.get(tid, 0)) + 1)
        for tid in (1, 2, 3, 4)
    ]
    growth_costs = [cost for cost in growth_costs if cost is not None]
    reserve = (
        min(growth_costs, key=lambda cost: sum(int(v) for v in cost.values()))
        if growth_costs else {}
    )
    budget = {
        key: max(
            0,
            int(
                max(
                    0.0,
                    float(resource_state.get(key, 0) or 0) - float(reserve.get(key, 0)),
                ) * ratio
            ),
        )
        for key in ("metal", "crystal", "deuterium")
    }
    budget_value = sum(budget.values())
    if budget_value <= 0:
        return []

    # One live selected-planet snapshot caps every candidate consistently.  An
    # unavailable snapshot is not equivalent to an empty queue: dynamic fleet
    # growth fails closed while the tiny fixed bootstrap batches remain untouched
    # in personality.plan_secondary.
    queue = production_capacity.production_queue_snapshot(email or "")
    horizon = configured.get("queue_horizon_seconds")

    current_values = {
        tid: max(0, int(account_ship_totals.get(tid, 0))) * sum(cost)
        for tid, cost in SHIP_COST.items()
    }
    current_total = sum(current_values.values())
    desired_total = current_total + budget_value

    ranked: list[tuple[float, tuple[str, int, int]]] = []
    for tid, weight in weights.items():
        if not prereqs_met(tid, dict(levels)):
            continue
        cost = SHIP_COST[tid]
        affordable = _affordable(cost, budget)
        if affordable <= 0:
            continue
        deficit_value = max(0.0, desired_total * weight - current_values.get(tid, 0))
        if deficit_value <= 0:
            continue
        unit_value = sum(cost)
        needed = max(1, math.ceil(deficit_value / unit_value))
        amount = min(needed, affordable)
        amount = production_capacity.cap_ship_amount_from_snapshot(
            queue,
            tid,
            amount,
            levels=levels,
            horizon_seconds=horizon,
        )
        if amount > 0:
            ranked.append((deficit_value, ("ship", tid, amount)))
    ranked.sort(key=lambda item: (-item[0], item[1][1]))
    return [action for _, action in ranked]


def strategic_research_candidates(
    personality_key: str, levels: Mapping[int, int]
) -> list[tuple[str, int]]:
    """유한 커리큘럼 뒤에도 가장 뒤처진 유효 연구를 계속 발전시킨다."""
    order = STRATEGIC_RESEARCH.get(
        personality_key, STRATEGIC_RESEARCH["hybrid"]
    )
    rank = {tid: index for index, tid in enumerate(order)}
    eligible = [tid for tid in order if prereqs_met(tid, dict(levels))]
    eligible.sort(key=lambda tid: (int(levels.get(tid, 0)), rank[tid]))
    return [("research", tid) for tid in eligible]
