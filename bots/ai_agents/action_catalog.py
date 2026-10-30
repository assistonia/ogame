"""LLM 정책이 선택할 수 있는 게임 행동의 단일 명세.

성격(personality)은 기본 가중치일 뿐 행동 allowlist가 아니다. ``action_plan``이 없는
guided 정책은 종전 성격별 파이프라인을 그대로 쓰고, full 정책은 아래 모든 행동을
사용할 수 있다. 서버가 강제하는 선행조건·소유권·함대 슬롯 검사는 어느 모드에서도
우회하지 않는다.
"""
from __future__ import annotations

from collections.abc import Iterable


ACTION_CATALOG: tuple[dict[str, str], ...] = (
    {"name": "build", "phase": "planet", "description": "건물·시설 건설"},
    {"name": "research", "phase": "account", "description": "연구 큐 진행"},
    {"name": "ship", "phase": "planet", "description": "민간·전투 함선 건조"},
    {"name": "expedition", "phase": "fleet", "description": "16번 슬롯 원정"},
    {"name": "spy", "phase": "fleet", "description": "남는 슬롯으로 행성 정찰과 full-intel 갱신"},
    {"name": "raid", "phase": "fleet", "description": "남는 슬롯으로 정찰 결과 기반 약탈 공격"},
    {"name": "harvest", "phase": "fleet", "description": "남는 슬롯으로 데브리 필드 수거"},
    {"name": "colonize", "phase": "fleet", "description": "천체물리·식민선 기반 확장"},
    {
        "name": "transfers",
        "phase": "fleet",
        "description": "구조화 계획에 따른 행성 간 자원 수송",
    },
    {
        "name": "development",
        "phase": "empire",
        "description": "구조화 계획에 따른 지정 행성 개발",
    },
    {
        "name": "production",
        "phase": "empire",
        "description": "구조화 계획에 따른 다행성 함선 생산 배분",
    },
    {
        "name": "moon_development",
        "phase": "empire",
        "description": "AI 구조화 계획에 따른 지정 달 시설 개발",
    },
    {
        "name": "phalanx_scans",
        "phase": "fleet",
        "description": "AI 구조화 계획의 지정 달·좌표 팔랑크스 스캔",
    },
    {
        "name": "jump_gate",
        "phase": "fleet",
        "description": "AI 구조화 계획의 지정 달 간 점프 게이트 수송",
    },
    {
        "name": "intercepts",
        "phase": "fleet",
        "description": "AI 구조화 계획의 관측 근거 귀환 함대 요격",
    },
    {"name": "defense", "phase": "planet", "description": "행성 방어시설 건설"},
    {"name": "fleetsave", "phase": "fleet", "description": "남는 함대 왕복 대피"},
    {
        "name": "develop_colonies",
        "phase": "empire",
        "description": "식민지 순회 건설·연구·함선 생산",
    },
    {
        "name": "diplomacy",
        "phase": "social",
        "description": "AI·실제 유저에게 제한된 메시지/NAP 제안·응답",
    },
)

ACTION_NAMES = frozenset(item["name"] for item in ACTION_CATALOG)
FULL_ACTION_PLAN = tuple(item["name"] for item in ACTION_CATALOG)

# A present structured section is authoritative.  In particular, an explicit
# empty list disables that structured action instead of falling through to the
# full-autonomy catalog.  ``develop_colonies`` is the legacy implementation
# replaced by a present development section; ship production is deliberately
# not replaced globally because only explicitly covered planets are suppressed.
STRUCTURED_PLAN_ACTIONS: dict[str, str] = {
    "colonize": "colonize",
    "transfers": "transfers",
    "development": "development",
    "production": "production",
    "moon_development": "moon_development",
    "phalanx_scans": "phalanx_scans",
    "jump_gate": "jump_gate",
    "intercepts": "intercepts",
}

# These actions have no legacy/rule-bot fallback.  Merely adding them to the
# catalog must not make a full-autonomy policy synthesize moon activity.
AI_STRUCTURED_ACTIONS = frozenset({
    "moon_development",
    "phalanx_scans",
    "jump_gate",
    "intercepts",
})
AI_STRUCTURED_CONTEXT_KEY = "ai_structured_context"


def clean_action_plan(value) -> list[str] | None:
    """정책 action_plan을 중복 없는 유효 행동 목록으로 정규화.

    ``None``은 구 정책의 성격별 동작을 유지한다. 명시적 빈 목록은 아무 행동도 선택하지
    않은 정책이므로 그대로 빈 목록으로 보존한다.
    """
    if value is None:
        return None
    if not isinstance(value, list):
        return None
    out: list[str] = []
    for raw in value:
        if not isinstance(raw, str):
            continue
        name = raw.strip()
        if name in ACTION_NAMES and name not in out:
            out.append(name)
    return out


def effective_action_plan(policy: dict | None) -> list[str] | None:
    """집행용 행동 목록.

    full+미지정은 전체 카탈로그, guided+미지정은 legacy(None)다. full 정책에서
    ``defense``/``fleetsave`` 누락은 명시적인 포기로 보지 않는다. 두 행동은 기본이
    adaptive라서 새 피격·노출이 생긴 정책 갱신 사이에도 반응해야 한다. 정말 끄려면
    각 설정의 ``mode=off``를 명시해야 한다.
    """
    if not isinstance(policy, dict):
        return None
    configured = policy.get("action_plan")
    if isinstance(configured, list):
        plan = [name for name in configured if name in ACTION_NAMES]
        if policy.get("autonomy") == "full":
            defense = policy.get("defense_strategy")
            defense_mode = defense.get("mode") if isinstance(defense, dict) else "adaptive"
            if defense_mode != "off" and "defense" not in plan:
                plan.append("defense")
            fleetsave = policy.get("fleetsave")
            fleetsave_mode = (
                fleetsave.get("mode") if isinstance(fleetsave, dict) else "adaptive"
            )
            if fleetsave_mode != "off" and "fleetsave" not in plan:
                plan.append("fleetsave")
        return _apply_structured_plan_authority(policy, plan)
    if policy.get("autonomy") == "full":
        return _apply_structured_plan_authority(policy, list(FULL_ACTION_PLAN))
    return None


def _apply_structured_plan_authority(policy: dict, plan: list[str]) -> list[str]:
    plans = policy.get("plans")
    authoritative = list(plan)
    ai_structured = policy.get(AI_STRUCTURED_CONTEXT_KEY) is True
    if not isinstance(plans, dict):
        return [name for name in authoritative if name not in AI_STRUCTURED_ACTIONS]
    for action, section in STRUCTURED_PLAN_ACTIONS.items():
        if action in AI_STRUCTURED_ACTIONS:
            if not ai_structured or section not in plans or not plans.get(section):
                authoritative = [name for name in authoritative if name != action]
            continue
        # A non-empty structured colonization plan replaces legacy target
        # selection. Engine-added empty contract sections retain legacy growth.
        if section in plans and plans.get(section) and action == "colonize":
            authoritative = [name for name in authoritative if name != action]
    if plans.get("development"):
        authoritative = [name for name in authoritative if name != "develop_colonies"]
    return authoritative


def action_enabled(policy: dict | None, name: str, *, legacy_default: bool = True) -> bool:
    if isinstance(policy, dict):
        plans = policy.get("plans")
        section = STRUCTURED_PLAN_ACTIONS.get(name)
        if name in AI_STRUCTURED_ACTIONS:
            return bool(
                policy.get(AI_STRUCTURED_CONTEXT_KEY) is True
                and isinstance(plans, dict)
                and section in plans
                and plans.get(section)
            )
        if isinstance(plans, dict) and section in plans:
            # Empty colonization is the engine's schema default, not a request
            # to stop the proven legacy colonizer. Remove colonize from
            # action_plan when the AI explicitly wants no colonization.
            if name != "colonize" or plans.get(section):
                return bool(plans.get(section))
    elif name in AI_STRUCTURED_ACTIONS:
        return False
    plan = effective_action_plan(policy)
    if plan is None:
        return legacy_default
    return name in plan


def render_catalog() -> str:
    lines = ["## LLM 행동 카탈로그 (action_plan에서 선택·정렬)"]
    for item in ACTION_CATALOG:
        lines.append(f"- {item['name']} [{item['phase']}]: {item['description']}")
    lines.extend((
        "- autonomy=full이고 action_plan을 생략하면 위 행동을 전부 사용한다.",
        "- 원정 슬롯을 먼저 수익화하고, 남는 일반 함대 슬롯은 정찰·약탈·수거에 사용한다.",
        "- full에서 defense/fleetsave는 adaptive 기본 반응이다. 정말 끄려면 각각 mode=off를 명시한다.",
        "- 성격은 행동 금지가 아니라 우선 성향이다. full에서는 어느 성격도 모든 행동을 선택할 수 있다.",
        "- 서버 선행조건·소유권·함대 슬롯·휴가/관리자 금지는 정책으로 우회할 수 없다.",
    ))
    return "\n".join(lines)


def main() -> int:
    print(render_catalog())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
