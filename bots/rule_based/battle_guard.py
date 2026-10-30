"""전투 시뮬 예측과 실제 battle_reports를 연결하는 안전핀 배선.

`battle_sim.decide()`는 한 시점의 예측만 만든다. 실제 공격은 몇 분 뒤 도착하므로, 안전핀을
작동시키려면 파견 당시 예측을 보관했다가 battle_reports의 실제 공격측 손실과 맞춰야 한다.
이 모듈이 그 지연 상관관계를 담당한다.

운영 원칙:
  - log 모드 예측도 기록/대조해 오차 관측에 쓰되 안전핀 스트릭에는 넣지 않는다.
  - live 모드만 실제손실 > max(예측×2, 10k) 연속 3건 규칙에 연결한다.
  - 트립 순간 metrics.db/stall_events에 한 행을 남긴다.
  - 좌표 + 공격 함대 구성 + 파견 이후 시각이 모두 맞아야 같은 전투로 본다.
  - DB/파일 오류는 공격 루프를 깨지 않으며, 미매칭 예측은 6시간 뒤 만료한다.
"""
from __future__ import annotations

import json
import os
import pathlib
import sqlite3
import time
from typing import Any

from bots.rule_based import battle_sim
from bots.shared import db_reader
from bots.shared.game_objects import NAME_TO_ID


STATE_DIR = pathlib.Path(__file__).resolve().parent / ".battle_guard_state"
METRICS_DB = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "metrics.db"
PENDING_TTL_SEC = 6 * 3600
RECENT_RESOLVED_CAP = 50


def _state_path(email: str) -> pathlib.Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in email)
    return STATE_DIR / f"{safe}.json"


def _empty_state() -> dict[str, Any]:
    return {"pending": [], "resolved": []}


def _load(email: str) -> dict[str, Any]:
    try:
        data = json.loads(_state_path(email).read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("pending", [])
            data.setdefault("resolved", [])
            return data
    except (OSError, ValueError):
        pass
    return _empty_state()


def _save(email: str, state: dict[str, Any]) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        path = _state_path(email)
        tmp = path.with_suffix(f".tmp.{os.getpid()}")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def record_prediction(
    email: str,
    target: tuple[int, int, int],
    attacker: dict[int, int],
    decision: dict[str, Any],
    *,
    mode: str,
    sent_at: float | None = None,
    cohort: str | None = None,
) -> None:
    """성공적으로 파견된 공격 1건의 예측을 보관한다.

    ``cohort``는 같은 집행 경로에서 새로 생성된 표본만 별도 채점하기 위한 관측
    메타데이터다. 안전핀 동작에는 관여하지 않으며, 길이가 제한된 단순 문자열만
    저장한다.
    """
    if mode not in {"log", "live"}:
        return
    sim = decision.get("sim") if isinstance(decision, dict) else None
    if not isinstance(sim, dict):
        return
    state = _load(email)
    prediction = {
        "email": email,
        "target": [int(x) for x in target],
        "attacker": {str(int(k)): int(v) for k, v in attacker.items() if int(v) > 0},
        "predicted_loss": float(sim.get("attacker_loss_value", 0.0) or 0.0),
        "win_prob": float(sim.get("win_prob", 0.0) or 0.0),
        "ev": float(decision.get("ev", 0.0) or 0.0),
        "recommend": bool(decision.get("recommend", False)),
        "mode": mode,
        "sent_at": float(time.time() if sent_at is None else sent_at),
    }
    if isinstance(cohort, str) and cohort and len(cohort) <= 64:
        prediction["cohort"] = cohort
    state["pending"].append(prediction)
    # 비정상 중복/장기 장애가 파일을 무한히 키우지 않게 최근 100건만 유지한다.
    state["pending"] = state["pending"][-100:]
    _save(email, state)


def _parse_units(raw: Any) -> dict[int, int]:
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "ignore")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[int, int] = {}
    for name, amount in raw.items():
        uid = NAME_TO_ID.get(str(name))
        if uid is None:
            continue
        try:
            n = int(amount)
        except (TypeError, ValueError):
            continue
        if n > 0:
            out[uid] = n
    return out


def _fetch_reports(email: str, since: float) -> list[dict[str, Any]]:
    """이 계정이 공격자인 최근 실전 보고서. 테스트에서 monkeypatch 가능한 경계."""
    conn = db_reader._conn()
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE email = %s", (email,))
        user = cur.fetchone()
        if not user:
            return []
        cur.execute(
            "SELECT id, planet_galaxy g, planet_system s, planet_position p, "
            "attacker, UNIX_TIMESTAMP(created_at) created_ts "
            "FROM battle_reports "
            "WHERE CAST(JSON_UNQUOTE(JSON_EXTRACT(attacker, '$.player_id')) AS UNSIGNED) = %s "
            "AND created_at >= FROM_UNIXTIME(%s) ORDER BY id",
            (int(user["id"]), int(since)),
        )
        return list(cur.fetchall())


def _report_payload(row: dict[str, Any]) -> tuple[dict[int, int], float]:
    raw = row.get("attacker")
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "ignore")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}, 0.0
    if not isinstance(raw, dict):
        return {}, 0.0
    return _parse_units(raw.get("units")), float(raw.get("resource_loss", 0) or 0)


def _record_trip_event(email: str, predicted: float, actual: float) -> None:
    """안전핀 트립을 기존 stall_events 관측 테이블에 기록."""
    try:
        con = sqlite3.connect(METRICS_DB, timeout=5)
        con.execute(
            "INSERT INTO stall_events(ts,cancel_rate,building_sum,detail) VALUES(?,?,?,?)",
            (
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                None,
                None,
                f"battle_sim safety trip {email}: predicted={predicted:.0f} actual={actual:.0f}",
            ),
        )
        con.commit()
        con.close()
    except (OSError, sqlite3.Error):
        pass


def reconcile(email: str, *, now: float | None = None) -> list[dict[str, Any]]:
    """보관된 예측을 실제 보고서와 대조하고 새로 해결된 관측 목록을 반환."""
    now = time.time() if now is None else float(now)
    state = _load(email)
    pending = [p for p in state.get("pending", []) if isinstance(p, dict)]
    if not pending:
        return []

    oldest = min(float(p.get("sent_at", now)) for p in pending)
    try:
        reports = _fetch_reports(email, oldest - 10)
    except Exception:  # noqa: BLE001 - 관측 실패가 레이더 틱을 깨면 안 됨
        return []

    used_report_ids: set[int] = set()
    resolved_now: list[dict[str, Any]] = []
    still_pending: list[dict[str, Any]] = []
    for pred in pending:
        sent_at = float(pred.get("sent_at", 0.0) or 0.0)
        target = tuple(int(x) for x in pred.get("target", []))
        expected_units = {int(k): int(v) for k, v in (pred.get("attacker") or {}).items()}
        match: tuple[dict[str, Any], float] | None = None
        for row in reports:
            rid = int(row["id"])
            if rid in used_report_ids:
                continue
            created_ts = float(row.get("created_ts", 0.0) or 0.0)
            coord = (int(row["g"]), int(row["s"]), int(row["p"]))
            units, actual_loss = _report_payload(row)
            if created_ts + 10 < sent_at or coord != target or units != expected_units:
                continue
            match = (row, actual_loss)
            used_report_ids.add(rid)
            break

        if match is None:
            if now - sent_at <= PENDING_TTL_SEC:
                still_pending.append(pred)
            continue

        row, actual_loss = match
        predicted_loss = float(pred.get("predicted_loss", 0.0) or 0.0)
        outcome: dict[str, Any] = {
            "report_id": int(row["id"]),
            "email": pred.get("email", email),
            "mode": pred.get("mode", "log"),
            "predicted_loss": predicted_loss,
            "actual_loss": actual_loss,
            "ratio": actual_loss / max(1.0, predicted_loss),
            "resolved_at": now,
            # 재채점기가 파견 시점 이후의 코호트만 정확히 고를 수 있게 exact-match에
            # 사용한 입력을 결과에도 보존한다. 기존 상태 파일은 이 키가 없어도 호환된다.
            "sent_at": sent_at,
            "target": list(target),
            "attacker": {str(k): v for k, v in expected_units.items()},
        }
        cohort = pred.get("cohort")
        if isinstance(cohort, str) and cohort:
            outcome["cohort"] = cohort
        if pred.get("mode") == "live":
            safety = battle_sim.record_outcome(email, predicted_loss, actual_loss)
            outcome["safety"] = safety
            if safety.get("just_tripped"):
                _record_trip_event(email, predicted_loss, actual_loss)
        resolved_now.append(outcome)

    state["pending"] = still_pending
    state.setdefault("resolved", []).extend(resolved_now)
    state["resolved"] = state["resolved"][-RECENT_RESOLVED_CAP:]
    _save(email, state)
    return resolved_now


def recent_observations(email: str) -> list[dict[str, Any]]:
    """계정별 최근 해결 관측(상태/status 도구용)."""
    return list(_load(email).get("resolved", []))
