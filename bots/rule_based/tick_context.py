"""틱 컨텍스트(TickContext) — 규칙봇/AI 양쪽 성장 계획 계층의 공용 상태 꾸러미.

## 왜 존재하는가 (배경)
성장 계획 계층(personality.plan_primary / plan_secondary, miner_bot._queue_actions)은
"이번 틱의 상태"(레벨/에너지/자원현황/틱인덱스/저장고 상태키/큐 대상/위성 허용 등)를
여러 개의 개별 인자로 받아 왔다. 이 상태를 호출하는 경로가 둘이다:
  - 규칙봇 경로: miner_bot.tick
  - AI 경로    : ai_agents/policy_engine.policy_tick
두 경로가 **각자** 인자를 채워 넘기다 보니, 규칙봇 쪽에 새 파라미터(allow_satellite,
plan_secondary 의 energy/resource_state, _queue_actions 의 사전체크 인자 등)가 추가될 때
AI 경로가 그 인자를 빠뜨리는 "한쪽만 누락" 회귀가 반복됐다(실측: AI 계정 9/10 에너지 적자).

이 모듈은 그 상태를 **하나의 객체(TickContext)** 로 묶고, 양쪽 경로가 **같은 빌더
함수(build_tick_context)** 로 만든 **같은 객체** 를 계획 계층에 넘기게 한다. 앞으로
계획 계층에 상태가 하나 더 필요해지면 여기 필드 하나 + 빌더 한 줄만 고치면 양쪽 경로에
동시에 전달된다 — 한쪽만 누락되는 구조 자체가 사라진다.

## 하위호환
계획 계층 함수들(plan_primary/plan_secondary/_queue_actions)은 기존 개별 kwargs 시그니처를
그대로 유지하되, `ctx=TickContext(...)` 를 받으면 그 필드를 우선 사용한다. 즉 기존 호출부
(테스트/외부 스크립트)는 손대지 않아도 동작하고, 두 실가동 경로만 ctx 방식으로 전환한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TickContext:
    """한 틱의 성장 계획에 필요한 상태 꾸러미.

    필드:
      personality_key : 집행할 성격 키(miner/raider/turtle/explorer/hybrid).
      levels          : 건물/시설/연구/함선 레벨·수량 dict (technologyId -> level).
      tick_index      : 논리 시계(성격 사이클·백오프·스트릭 판정에 쓰임).
      energy          : 현재 에너지 여유(양수=흑자). 조회 실패 시 None.
      resource_state  : 자원 현황+용량 dict(get_resource_state_db). 저장고/사전체크/위성
                        판정용. 조회 실패 시 None.
      state_key       : 저장고 반응규칙(스트릭)·백오프 상태 파일 키(보통 이메일).
      active_targets  : 이미 건설 큐에 있는 대상 id 집합(저장고 중복 큐잉 방지).
      allow_satellite : 에너지 개입에서 태양광 위성(조선소 큐) 경로를 허용할지.
                        규칙봇/AI 모두 plan_secondary 로 위성을 병행 건조하므로 True.
      email           : 계정 이메일(로그/상태키 원본). state_key 와 보통 동일.
      account_ship_totals: 모든 행성+비행+미완료 큐를 합친 계정 함선 수량. DB 조회
                        실패 시 None.
      require_account_ship_totals: True면 함선 상한 계획에서 위 합계가 필수다. 실가동
                        규칙봇/AI 틱은 True로 두어 조회 실패 때 함선 건조만 fail-closed한다.
                        직접 계획 함수를 부르는 구 코드/테스트는 기본 False로 현재 행성
                        ``levels``를 쓰는 하위호환을 유지한다.
      ship_strategy   : 고정 함선 상한 대신 쓸 동적 투자 설정. None이면 계정 성격과
                        이메일에서 재현 가능한 개인 전략을 계산한다.
    """

    personality_key: str
    levels: dict[int, int]
    tick_index: int
    energy: int | None = None
    resource_state: dict | None = None
    state_key: str | None = None
    active_targets: set[int] = field(default_factory=set)
    allow_satellite: bool = True
    email: str | None = None
    account_ship_totals: dict[int, int] | None = None
    require_account_ship_totals: bool = False
    ship_strategy: dict | None = None


def build_tick_context(
    personality_key: str,
    levels: dict[int, int],
    tick_index: int,
    *,
    energy: int | None = None,
    resource_state: dict | None = None,
    email: str | None = None,
    active_targets: set[int] | None = None,
    allow_satellite: bool = True,
    account_ship_totals: dict[int, int] | None = None,
    require_account_ship_totals: bool = False,
    ship_strategy: dict | None = None,
) -> TickContext:
    """규칙봇/AI 경로가 공용으로 쓰는 TickContext 빌더.

    두 경로가 이 함수 하나로 컨텍스트를 만들어 계획 계층에 넘긴다. 상태 파라미터가
    추가될 때 이 빌더와 TickContext 필드만 고치면 양쪽 경로에 동시에 반영된다.
    state_key 는 email 을 그대로 쓴다(저장고 상태 파일 키 = 계정).
    """
    return TickContext(
        personality_key=personality_key,
        levels=levels,
        tick_index=tick_index,
        energy=energy,
        resource_state=resource_state,
        state_key=email,
        active_targets=active_targets if active_targets is not None else set(),
        allow_satellite=allow_satellite,
        email=email,
        account_ship_totals=account_ship_totals,
        require_account_ship_totals=require_account_ship_totals,
        ship_strategy=ship_strategy,
    )
