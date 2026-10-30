"""규칙봇 경로와 AI 경로가 계획 계층에 **같은 입력** 을 넘기는지 검증하는 통합 테스트.

## 목적 (왜 이 테스트가 있나)
AI 성장 부진의 지배 원인은 policy_engine 이 규칙봇 경로(miner_bot.tick)의 새 파라미터를
계획 계층(plan_primary/plan_secondary/_queue_actions)에 넘길 때 하나씩 누락한 것이었다
(에너지 위성 경로·연구 사전체크 등). 두 경로를 공용 컨텍스트(TickContext)로 통일한 뒤,
"같은 상태에서 두 경로가 같은 primary/secondary 후보군을 계획 계층에 넘기는가" 를 여기서
못박는다. 앞으로 어느 한쪽 경로만 파라미터를 빠뜨리면(경로 분기 재발) 이 테스트가 깨진다.

HTTP/DB 없이 동작한다: db_reader 조회와 계획 계층 실행(_queue_actions/_try_first)을 가짜로
갈아끼워, 두 tick 함수가 **계획 계층에 넘긴 후보 리스트**만 포착해 비교한다.

## 추가: 조건형 함선 가드 단위검증
정책 ship_priority 의 max_total / min_surplus / 위성 흑자 스킵 / 가드 없는 옛 정책이
각각 의도대로 동작하는지 apply_ship_priority 레벨에서 직접 검증한다.

실행: `python3 -m pytest tests/test_plan_parity.py` 또는 `python3 tests/test_plan_parity.py`.
"""
from __future__ import annotations

import contextlib
import pathlib
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from bots.rule_based import energy
from bots.rule_based import miner_bot
from bots.rule_based import personality
from bots.rule_based import colony
from bots.rule_based import colony_dev
from bots.rule_based import miner_defense
from bots.rule_based import storage
from bots.shared import db_reader
from bots.ai_agents import policy_engine
from bots.ai_agents.policy_engine import apply_ship_priority
from bots.shared.game_objects import SOLAR_SATELLITE, LIGHT_FIGHTER


# ---------------------------------------------------------------------------
# 모의 상태 (HTTP/DB 없이 고정) — miner 중반 제국
# ---------------------------------------------------------------------------
TICK_INDEX = 7  # 홀수: plan_secondary 가 ship+research 순으로 반환(짝/홀 분기 커버)

# 건물/시설/연구/함선 레벨. 조선소4·연구소5 라 연구+함선 후보가 둘 다 오른다.
MOCK_LEVELS: dict[int, int] = {
    1: 8, 2: 6, 3: 3, 4: 6,        # 광산 + 태양광 (모두 floor 초과)
    14: 4, 31: 5, 21: 4,           # 로봇공장/연구소/조선소
    113: 3, 115: 2,                # 에너지기술/연소엔진 (small_cargo 선행 충족)
    202: 2, 212: 0,                # 소형화물 2기 / 위성 0
}

# 에너지 흑자(+500) → 에너지 개입 없음(위성 주입 없음) → 두 경로가 순수 성장 후보만 계획.
MOCK_ENERGY = 500
MOCK_RESOURCE_STATE: dict = {
    "metal": 40000, "crystal": 25000, "deuterium": 8000,
    "energy": MOCK_ENERGY,
    "metal_max": 200000, "crystal_max": 200000, "deuterium_max": 200000,  # 창고 여유(저장고 규칙 비발동)
    "temp_max": 40,
}


class FakeClient:
    """계획 계층을 가짜로 갈아끼운 상태에서 tick 함수가 요구하는 최소 인터페이스만 제공."""

    def __init__(self, email: str) -> None:
        self.email = email

    def login(self) -> None:  # policy_tick/tick 가 client=None 아닐 때는 호출 안 함
        pass

    def get_status(self) -> dict:  # DB 조회를 가짜로 성공시키므로 폴백 경로로는 안 옴
        return {"energy": MOCK_ENERGY}


@contextlib.contextmanager
def _patched(capture: list[tuple[str, list]]):
    """db_reader 조회와 계획 계층 실행을 가짜로 대체. 넘어온 후보 리스트를 capture 에 기록.

    _queue_actions 는 primary(건설) → research 순으로 두 번, _try_first 는 ship 한 번
    호출된다. 두 경로 모두 miner_bot 의 이 함수들을 참조하므로 한 번만 갈아끼우면 된다.
    """
    saved = {}

    def _fake_queue_actions(client, candidates, max_items, **kwargs):
        capture.append(("queue", list(candidates)))
        return [], ""

    def _fake_try_first(client, candidates):
        capture.append(("ship", list(candidates)))
        return None, ""

    patches = {
        (miner_bot, "_queue_actions"): _fake_queue_actions,
        (miner_bot, "_try_first"): _fake_try_first,
        (db_reader, "get_levels_db"): lambda email, *a, **k: dict(MOCK_LEVELS),
        (db_reader, "get_account_ship_totals_db"): lambda email: {
            202: MOCK_LEVELS[202], 212: MOCK_LEVELS[212]
        },
        (db_reader, "get_resource_state_db"): lambda email, *a, **k: dict(MOCK_RESOURCE_STATE),
        (db_reader, "active_build_targets"): lambda email, *a, **k: set(),
        (colony, "colony_tick"): lambda *a, **k: None,  # 후행 훅(HTTP) 무력화
        (colony_dev, "colony_dev_tick"): lambda *a, **k: None,  # 식민지 개발 훅(HTTP/DB) 무력화
    }
    try:
        for (mod, name), fn in patches.items():
            saved[(mod, name)] = getattr(mod, name)
            setattr(mod, name, fn)
        # 저장고 상태파일이 레포를 오염시키지 않도록 임시 디렉토리로 격리.
        saved[(storage, "STATE_DIR")] = storage.STATE_DIR
        storage.STATE_DIR = pathlib.Path(tempfile.mkdtemp()) / "backoff"
        # 라이브 base-defense가 테스트 계정용 영속 상태를 레포에 만들지 않게 격리한다.
        saved[(miner_defense, "MINER_BASE_DEFENSE_ENABLED")] = (
            miner_defense.MINER_BASE_DEFENSE_ENABLED
        )
        miner_defense.MINER_BASE_DEFENSE_ENABLED = False
        yield
    finally:
        for (mod, name), fn in saved.items():
            setattr(mod, name, fn)


def _run_rule_tick() -> list[tuple[str, list]]:
    cap: list[tuple[str, list]] = []
    with _patched(cap):
        miner_bot.tick(
            "bot001@bots.local", client=FakeClient("bot001@bots.local"),
            personality_key="miner", tick_index=TICK_INDEX,
        )
    return cap


def _run_ai_tick() -> list[tuple[str, list]]:
    cap: list[tuple[str, list]] = []
    with _patched(cap):
        # 정책 파일 없음 → load_policy 가 miner 기본값으로 폴백(정책=성격 기본값).
        policy_engine.policy_tick(
            "ai_test_parity@bots.local", client=FakeClient("ai_test_parity@bots.local"),
            tick_index=TICK_INDEX,
        )
    return cap


# ---------------------------------------------------------------------------
# 1) 경로 패리티: 규칙봇 틱 == AI 틱(정책=성격 기본값)
# ---------------------------------------------------------------------------
def test_primary_secondary_candidates_match():
    """같은 상태에서 두 경로가 계획 계층에 넘긴 primary/research/ship 후보군이 동일해야 한다."""
    rule = _run_rule_tick()
    ai = _run_ai_tick()

    # 두 경로 모두 (queue=primary, queue=research, ship) 순으로 3번 계획 계층을 부른다.
    kinds_rule = [k for k, _ in rule]
    kinds_ai = [k for k, _ in ai]
    assert kinds_rule == kinds_ai == ["queue", "queue", "ship"], (
        f"계획 계층 호출 순서/횟수 불일치: rule={kinds_rule} ai={kinds_ai}"
    )

    labels = ["primary(건설)", "research(연구)", "ship(함선)"]
    for label, (_, r_cands), (_, a_cands) in zip(labels, rule, ai):
        assert r_cands == a_cands, (
            f"{label} 후보군이 경로별로 다르다 — 경로 분기 재발.\n"
            f"  규칙봇: {r_cands}\n  AI    : {a_cands}"
        )

    # 후보가 비어 있으면 테스트가 무의미 → 실제로 연구·함선 후보가 올라왔는지 확인.
    assert ai[1][1], "research 후보가 비었다(모의 상태가 연구 후보를 못 만듦 — 픽스처 점검)"
    assert ai[2][1], "ship 후보가 비었다(모의 상태가 함선 후보를 못 만듦 — 픽스처 점검)"


def test_secondary_plan_counts_already_queued_satellites(monkeypatch):
    """위성 큐가 있으면 undefined alias 없이 중복 위성 후보를 억제해야 한다."""
    queue = SimpleNamespace(
        available=True,
        unit_queue_seconds=0,
        unit_items=[SimpleNamespace(
            object_id=SOLAR_SATELLITE,
            remaining_amount=3,
        )],
    )
    monkeypatch.setattr(
        personality,
        "production_facility_recommendation",
        lambda *a, **k: (queue, None),
    )
    monkeypatch.setattr(personality, "_plan_primary_base", lambda *a, **k: [])
    monkeypatch.setattr(
        energy,
        "plan_energy",
        lambda *a, **k: SimpleNamespace(
            satellite_action=("ship", SOLAR_SATELLITE, 4)
        ),
    )

    actions = personality.plan_secondary(
        "turtle",
        {21: 1},
        tick_index=1,
        energy=-100,
        resource_state={"temp_max": 40},
    )

    assert not any(
        action[0] == "ship" and action[1] == SOLAR_SATELLITE
        for action in actions
    )


# ---------------------------------------------------------------------------
# 2) 조건형 함선 가드 단위검증
# ---------------------------------------------------------------------------
def _ship_ids(candidates: list[tuple]) -> list[int]:
    return [c[1] for c in candidates]


# 함선 선행조건 충족용 최소 레벨(전투기 204=조선소1·연소1, 위성 212=조선소1).
# 가드 로직만 떼어 검증하기 위해 선행조건은 항상 만족시킨다.
_PREREQ = {21: 4, 115: 2}


def test_guard_max_total_skips_when_reached():
    """max_total 도달 시(보유≥상한) 그 함선은 후보에서 빠진다."""
    ship_pri = [{"id": LIGHT_FIGHTER, "batch": 5, "max_total": 50}]
    levels = {**_PREREQ, LIGHT_FIGHTER: 50}  # 이미 50기 보유
    out = apply_ship_priority([], ship_pri, levels, resource_state=MOCK_RESOURCE_STATE,
                              energy=MOCK_ENERGY, energy_target=0)
    assert LIGHT_FIGHTER not in _ship_ids(out), "max_total 도달했는데 건조 후보에 남음"

    levels2 = {**_PREREQ, LIGHT_FIGHTER: 49}  # 상한 미만이면 통과
    out2 = apply_ship_priority([], ship_pri, levels2, resource_state=MOCK_RESOURCE_STATE,
                               energy=MOCK_ENERGY, energy_target=0)
    assert LIGHT_FIGHTER in _ship_ids(out2), "max_total 미달인데 건조 후보에 없음"


def test_guard_min_surplus_skips_when_below():
    """min_surplus 미달(지정 자원 보유<임계) 시 스킵, 충족 시 통과."""
    ship_pri = [{"id": LIGHT_FIGHTER, "batch": 5, "min_surplus": {"metal": 30000}}]
    low = {**MOCK_RESOURCE_STATE, "metal": 10000}   # 임계 미만
    high = {**MOCK_RESOURCE_STATE, "metal": 50000}  # 임계 이상
    out_low = apply_ship_priority([], ship_pri, dict(_PREREQ), resource_state=low, energy=MOCK_ENERGY)
    out_high = apply_ship_priority([], ship_pri, dict(_PREREQ), resource_state=high, energy=MOCK_ENERGY)
    assert LIGHT_FIGHTER not in _ship_ids(out_low), "min_surplus 미달인데 건조됨"
    assert LIGHT_FIGHTER in _ship_ids(out_high), "min_surplus 충족인데 건조 안 됨"


def test_guard_satellite_skipped_on_energy_surplus():
    """위성(212)은 에너지 흑자(energy>energy_target)면 정책이 시켜도 스킵(가드 무관)."""
    ship_pri = [{"id": SOLAR_SATELLITE, "batch": 4}]  # 가드 없는 옛 형식
    # 흑자(+500 > target 0) → 스킵
    out_surplus = apply_ship_priority([], ship_pri, dict(_PREREQ), resource_state=MOCK_RESOURCE_STATE,
                                      energy=500, energy_target=0)
    assert SOLAR_SATELLITE not in _ship_ids(out_surplus), "에너지 흑자인데 위성이 건조됨"
    # 적자(-200 < target 0) → 건조
    out_deficit = apply_ship_priority([], ship_pri, dict(_PREREQ), resource_state=MOCK_RESOURCE_STATE,
                                      energy=-200, energy_target=0)
    assert SOLAR_SATELLITE in _ship_ids(out_deficit), "에너지 적자인데 위성이 안 나옴"


def test_guardless_policy_still_executes_every_tick():
    """가드 없는 옛 ship_priority 항목은 그대로 매 틱 집행된다(하위호환)."""
    ship_pri = [{"id": LIGHT_FIGHTER, "batch": 5}]  # max_total/min_surplus 없음
    # 자원상태를 몰라도(None) 위성이 아니면 통과해야 한다.
    out = apply_ship_priority([], ship_pri, dict(_PREREQ), resource_state=None, energy=None)
    assert LIGHT_FIGHTER in _ship_ids(out), "가드 없는 옛 정책이 집행되지 않음"


def test_clean_ship_list_parses_guards():
    """load_policy 정규화가 max_total/min_surplus 를 보존하고 잘못된 값은 버린다."""
    raw = [
        {"id": 204, "batch": 5, "max_total": 50, "min_surplus": {"metal": 30000, "bogus": 1}},
        {"id": 202, "batch": 3, "max_total": 0},          # max_total<1 → 버림
        {"id": 212, "batch": 2, "min_surplus": "nope"},   # min_surplus 형식오류 → 버림
    ]
    cleaned = policy_engine._clean_ship_list(raw)
    by_id = {c["id"]: c for c in cleaned}
    assert by_id[204]["max_total"] == 50
    assert by_id[204]["min_surplus"] == {"metal": 30000}  # bogus 키 제거
    assert "max_total" not in by_id[202]                  # 무효값 → 가드 없음
    assert "min_surplus" not in by_id[212]                # 무효값 → 가드 없음


# ---------------------------------------------------------------------------
# 단독 실행 러너 (pytest 없이도 통과 확인 가능)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {t.__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
