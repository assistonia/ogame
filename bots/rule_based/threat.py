"""터틀 반사(turtle reflex) — 피격/방어 손실 감지 (roadmap A-b).

energy.plan_energy 가 "energy<0" **상태**에 반응해 원인 무관하게(전투보고를 읽지 않고)
태양광/위성을 재건하듯, 이 모듈은 "현재 방어 가치 < 최근 피크" 상태에 반응해 원인 무관하게
방어 손실을 감지한다. 방어 유닛은 전투로만 파괴되므로(광산 업글 같은 자연 감소가 없다) 이
상태 자체가 곧 "최근 피격" 신호다. 이것이 사용자가 지정한 상태 기반 트리거(선호)다.

## 주 신호(상태 기반, 선호): 방어 가치 피크
계정별 방어 가치 피크를 가볍게 파일 한 개에 보관하고, 매 틱 현재 방어 가치와 비교한다.
  - 현재 ≥ 피크 : 성장(신규 방어) → 피크 상향, 손실 아님.
  - 현재 < 피크 : 손실 발생 → 그 차이(deficit)가 재건해야 할 방어 가치.
피격 후 REFLEX_COOLDOWN_SEC 창 동안 반사를 유지하고, 방어가 피크로 회복되면 자동 해제한다
(deficit=0). 창이 지나도록 재건하지 못하면(자원 부족 등) 피크를 현재로 리셋해 경제를 영구히
굶기지 않는다(에너지 재건이 위성이 계속 터지면 끝없이 재건하는 것과 달리, 방어는 재무장
불가 상황에서 경제까지 마비되지 않도록 상한을 둔다).

첫 관측 틱은 last_value 가 없어 손실을 판정하지 않고 기준선(피크=현재)만 세운다 — 플래그를
켜는 순간 기존 방어를 '손실'로 오인해 반사가 헛발동하지 않게 하기 위함.

## 폴백(전투보고): 방어측 battle_reports
현재 방어 가치를 신뢰할 수 없을 때(방어 수량 조회 실패 → current_value=None)만 쓴다. 계정이
최근 DEFENDER_REPORT_WINDOW_SEC 안에 **방어측**으로 등장한 battle_reports 를 조회한다
(defender.player_id = 내 유저, defender.resource_loss>0). battle_guard._fetch_reports(공격측
player_id 로 조회)의 대칭이며, 리포트가 시간 창에서 만료되면 반사가 자연히 해제된다.

## 안전
이 모듈의 어떤 함수도 예외를 올리지 않는다(틱/루프 보호). 상태 파일 IO·DB 오류는 조용히
'반사 없음'으로 닫는다. 호출은 turtle.defense_tick 이 TURTLE_REFLEX_ENABLED=True 일 때만 한다.
"""
from __future__ import annotations

import json
import os
import pathlib
import time
from typing import Any, NamedTuple

from bots.shared import db_reader


STATE_DIR = pathlib.Path(__file__).resolve().parent / ".turtle_reflex_state"

# 피격 후 방어 재건을 최우선화하는 창. 이 안에 새 피격이 없고 방어가 피크로 회복되지 않으면
# 피크를 현재로 리셋해 반사를 해제한다(재무장 불가 시 경제 영구 기아 방지).
REFLEX_COOLDOWN_SEC = 6 * 3600

# 폴백(전투보고) 조회 창. 이 안의 방어측 손실 보고가 있으면 반사 활성(리포트 만료 시 해제).
DEFENDER_REPORT_WINDOW_SEC = 6 * 3600


class ThreatState(NamedTuple):
    """이번 틱 반사 판정.

    under_reflex : 최근 피격으로 방어를 잃어 재건을 최우선화해야 하는가.
    lost_value   : 재건해야 할 방어 가치(메탈+크리스탈+듀테륨 합). 로그/사이징용.
    source       : 판정 근거("state-peak" | "battle-report" | "none").
    """
    under_reflex: bool
    lost_value: int
    source: str


_NO_REFLEX = ThreatState(False, 0, "none")


# ---------------------------------------------------------------------------
# 상태 파일(계정별 방어 피크) — battle_guard 의 원자적 쓰기 패턴 재사용
# ---------------------------------------------------------------------------
def _state_path(email: str) -> pathlib.Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in email)
    return STATE_DIR / f"{safe}.json"


def _load(email: str) -> dict[str, Any]:
    try:
        data = json.loads(_state_path(email).read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def _save(email: str, state: dict[str, Any]) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        path = _state_path(email)
        tmp = path.with_suffix(f".tmp.{os.getpid()}")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 판정
# ---------------------------------------------------------------------------
def assess_turtle_threat(
    email: str, current_value: int | None, *, now: float | None = None
) -> ThreatState:
    """최근 피격(방어 손실) 여부를 판정한다.

    current_value : 현재 방어 유닛의 총 건조 가치(호출부 turtle._defense_value 가 계산).
                    방어 수량 조회 실패로 신뢰할 수 없으면 None → 전투보고 폴백.
    """
    now = time.time() if now is None else float(now)
    if current_value is not None:
        try:
            return _assess_from_peak(email, int(current_value), now)
        except Exception:  # noqa: BLE001 - 상태 파일 문제 → 전투보고 폴백
            pass
    try:
        return _assess_from_reports(email, now)
    except Exception:  # noqa: BLE001 - 관측 실패가 방어 틱을 깨면 안 됨
        return _NO_REFLEX


def _assess_from_peak(email: str, current_value: int, now: float) -> ThreatState:
    """주 신호: 현재 방어 가치와 보관된 피크를 비교한다."""
    state = _load(email)
    peak = int(state.get("peak_value", 0) or 0)
    last = state.get("last_value")
    last_hit_ts = float(state.get("last_hit_ts", 0.0) or 0.0)

    # 지난 틱보다 방어가 줄었으면 새 피격 → 피격 시각 갱신(연속 피격이면 창이 계속 갱신됨).
    if last is not None and current_value < int(last):
        last_hit_ts = now
    # 성장(신규 방어 건조)은 피크를 올린다 — 손실이 아니다.
    if current_value > peak:
        peak = current_value

    deficit = peak - current_value
    under = False
    lost = 0
    if deficit > 0:
        if now - last_hit_ts <= REFLEX_COOLDOWN_SEC:
            under = True
            lost = deficit
        else:
            # 쿨다운 경과: 재건 실패 → 새 기준선 수용(경제 영구 기아 방지).
            peak = current_value

    _save(email, {
        "peak_value": peak,
        "last_value": current_value,
        "last_hit_ts": last_hit_ts,
    })
    return ThreatState(under, lost, "state-peak")


def _fetch_defender_reports(email: str, since: float) -> list[dict[str, Any]]:
    """이 계정이 **방어자**인 최근 실전 보고서. 테스트에서 monkeypatch 가능한 경계.

    battle_guard._fetch_reports 의 대칭(그쪽은 attacker.player_id). 여기선 defender.player_id
    로 조회하고, defender.resource_loss(파괴된 방어/함선 가치 합)를 loss 로 돌려준다.
    """
    conn = db_reader._conn()
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE email = %s", (email,))
        user = cur.fetchone()
        if not user:
            return []
        cur.execute(
            "SELECT id, "
            "CAST(JSON_UNQUOTE(JSON_EXTRACT(defender, '$.resource_loss')) AS DECIMAL(30,2)) loss, "
            "UNIX_TIMESTAMP(created_at) created_ts "
            "FROM battle_reports "
            "WHERE CAST(JSON_UNQUOTE(JSON_EXTRACT(defender, '$.player_id')) AS UNSIGNED) = %s "
            "AND created_at >= FROM_UNIXTIME(%s) ORDER BY id",
            (int(user["id"]), int(since)),
        )
        return list(cur.fetchall())


def _assess_from_reports(email: str, now: float) -> ThreatState:
    """폴백: 최근 방어측 보고서의 손실 합으로 반사를 판정한다."""
    reports = _fetch_defender_reports(email, now - DEFENDER_REPORT_WINDOW_SEC)
    lost = 0.0
    for row in reports:
        try:
            lost += float(row.get("loss") or 0.0)
        except (TypeError, ValueError):
            continue
    if lost > 0:
        return ThreatState(True, int(lost), "battle-report")
    return _NO_REFLEX
