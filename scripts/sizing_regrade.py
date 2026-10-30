#!/usr/bin/env python3
"""Preflight 이후 battle_guard exact-match만 채점하는 읽기 전용 sizing 게이트.

게임 DB나 라이브 플래그는 절대 쓰지 않는다. 기본 실행은 상태 파일조차 쓰지 않고 현재
표본·속도·preflight 차단 근거만 출력한다. ``--notify``일 때에만 알림 dedupe용 로컬 JSON을
원자적으로 갱신하며, N>=57 최초 도달 또는 이후 PASS/FAIL 판정 변경 때만 OGame Discord
webhook으로 한 번 알린다. webhook 값과 네트워크 예외 문자열은 출력하지 않는다.

새 prediction은 동적 위험 한도를 분리한 명시적 ``preflight_v2_dynamic_cap`` cohort를
사용한다. 고정 100k 상한의 v1 표본은 더 큰 편대가 포함되는 새 위험분포와 섞지 않는다.
아래 legacy 복원은 과거 v1을 재현할 때만 쓰며 현재 v2 채점에서는 비활성이다.

1. universe.log의 실제 ``ATTACK[PREFLIGHT]`` 파견 로그
2. 같은 계정에서 15분 안에 생성된 battle report 시각(metrics.db)
3. battle_guard가 좌표+함대+파견시각으로 이미 exact-match한 resolved outcome

어느 하나라도 없으면 코호트에서 제외한다.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import pathlib
import re
import sqlite3
import time
from collections import Counter
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo


PROJECT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_GUARD_STATE_DIR = PROJECT / "bots" / "rule_based" / ".battle_guard_state"
DEFAULT_LOG_PATH = PROJECT / "scripts" / "universe.log"
DEFAULT_METRICS_DB = PROJECT / "scripts" / "metrics.db"
DEFAULT_NOTIFY_STATE = PROJECT / "scripts" / ".sizing_regrade_notify_state.json"
DEFAULT_WEBHOOK_FILE = PROJECT / ".discord_webhook_url"

COHORT = "preflight_v2_dynamic_cap"
LEGACY_INFERENCE_COHORT = "preflight_v1"
DEFAULT_MIN_REPORTS = 57
DEFAULT_MAX_LOSS_ERROR = 0.15
LEGACY_JOIN_WINDOW_SEC = 15 * 60
LOCAL_TZ = ZoneInfo("Asia/Seoul")

_LINE_RE = re.compile(
    r"^(?P<stamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+"
    r"\[\d+/\d+\]\s+(?P<email>\S+).*?\braid\s+"
)
_COORD_RE = re.compile(r"(?P<g>\d+):(?P<s>\d+):(?P<p>\d+)")


def _safe_email(email: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in email)


def _local_epoch(stamp: str) -> float | None:
    try:
        parsed = dt.datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return parsed.replace(tzinfo=LOCAL_TZ).timestamp()


def _iso_epoch(stamp: object) -> float | None:
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        parsed = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.timestamp()


def _event_reason(kind: str, tail: str) -> str:
    if kind == "ATTACK":
        return "파견 성공"
    if kind == "SPY":
        return "공격 직전 재정찰"
    match = re.search(r"\(([^)]*)\)", tail)
    return match.group(1).strip() if match else "사유 미기록"


def parse_preflight_events(log_path: pathlib.Path = DEFAULT_LOG_PATH) -> list[dict[str, Any]]:
    """현재 로그와 ``.1``을 시간순으로 읽고 preflight 이벤트를 반환한다."""
    rotated = log_path.with_name(f"{log_path.name}.1")
    paths = [path for path in (rotated, log_path) if path.is_file()]
    events: list[dict[str, Any]] = []
    seen: set[tuple[float, str, str, tuple[int, int, int] | None, str]] = set()
    for path in paths:
        try:
            handle = path.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                head = _LINE_RE.search(line)
                if head is None:
                    continue
                tail = line[head.end():]
                if tail.startswith("ATTACK[PREFLIGHT]"):
                    kind = "ATTACK"
                    detail = tail[len("ATTACK[PREFLIGHT]"):]
                elif tail.startswith("PREFLIGHT SPY"):
                    kind = "SPY"
                    detail = tail[len("PREFLIGHT SPY"):]
                elif tail.startswith("PREFLIGHT WAIT"):
                    kind = "WAIT"
                    detail = tail[len("PREFLIGHT WAIT"):]
                elif tail.startswith("PREFLIGHT HOLD"):
                    kind = "HOLD"
                    detail = tail[len("PREFLIGHT HOLD"):]
                else:
                    continue
                event_ts = _local_epoch(head.group("stamp"))
                if event_ts is None:
                    continue
                coord_match = _COORD_RE.search(detail)
                coord = (
                    tuple(int(coord_match.group(key)) for key in ("g", "s", "p"))
                    if coord_match else None
                )
                reason = _event_reason(kind, detail)
                key = (event_ts, head.group("email"), kind, coord, reason)
                if key in seen:
                    continue
                seen.add(key)
                events.append({
                    "ts": event_ts,
                    "stamp": head.group("stamp"),
                    "email": head.group("email"),
                    "kind": kind,
                    "coord": list(coord) if coord else None,
                    "reason": reason,
                })
    return sorted(events, key=lambda item: (float(item["ts"]), str(item["email"])))


def summarize_events(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = list(events)
    counts = Counter(str(row.get("kind", "UNKNOWN")) for row in rows)
    reasons = Counter(
        f"{row.get('kind', 'UNKNOWN')}:{row.get('reason', '사유 미기록')}"
        for row in rows
    )
    holds = [row for row in rows if row.get("kind") == "HOLD"]
    returned = sum(
        "복귀함대" in str(row.get("reason")) or "재정찰에 방어" in str(row.get("reason"))
        for row in holds
    )
    peak = sum("24h peak stress" in str(row.get("reason")) for row in holds)
    return {
        "counts": {kind: int(counts.get(kind, 0)) for kind in ("SPY", "WAIT", "HOLD", "ATTACK")},
        "reasons": dict(sorted(reasons.items())),
        "stale_return_evidence": {
            # 모든 SPY는 과거 intel만으로 바로 공격하지 않고 새 보고서를 요구한 증거다.
            "fresh_respy_requests": int(counts.get("SPY", 0)),
            "full_intel_waits": int(counts.get("WAIT", 0)),
            "returned_fleet_or_defense_blocks": returned,
            "history_peak_stress_blocks": peak,
            "direct_risk_blocks": returned + peak,
        },
    }


def _read_json(path: pathlib.Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _state_email(
    path: pathlib.Path,
    state: dict[str, Any],
    known_emails: Iterable[str],
) -> str | None:
    for section in ("pending", "resolved"):
        for row in state.get(section, []):
            email = row.get("email") if isinstance(row, dict) else None
            if isinstance(email, str) and email:
                return email
    stem = path.stem
    return next((email for email in known_emails if _safe_email(email) == stem), None)


def load_guard_rows(
    state_dir: pathlib.Path,
    known_emails: Iterable[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    resolved: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    for path in sorted(state_dir.glob("*.json")):
        state = _read_json(path)
        if state is None:
            continue
        fallback_email = _state_email(path, state, known_emails)
        for key, destination in (("resolved", resolved), ("pending", pending)):
            for raw in state.get(key, []):
                if not isinstance(raw, dict):
                    continue
                row = dict(raw)
                if not isinstance(row.get("email"), str) and fallback_email:
                    row["email"] = fallback_email
                row["_state_file"] = path.name
                destination.append(row)
    return resolved, pending


def load_battle_times(metrics_db: pathlib.Path) -> dict[int, tuple[float, str | None]]:
    try:
        con = sqlite3.connect(f"file:{metrics_db}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return {}
    try:
        columns = {str(row[1]) for row in con.execute("PRAGMA table_info(battles)")}
        if not {"battle_id", "battle_ts", "attacker_email"}.issubset(columns):
            return {}
        admin_test_filter = (
            " AND COALESCE(is_admin_test,0)=0" if "is_admin_test" in columns else ""
        )
        rows = con.execute(
            "SELECT battle_id,battle_ts,attacker_email FROM battles "
            "WHERE battle_id IS NOT NULL AND battle_ts IS NOT NULL"
            + admin_test_filter
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        con.close()
    result: dict[int, tuple[float, str | None]] = {}
    for battle_id, battle_ts, email in rows:
        epoch = _iso_epoch(battle_ts)
        if epoch is not None:
            result[int(battle_id)] = (epoch, email if isinstance(email, str) else None)
    return result


def _coord_tuple(raw: object) -> tuple[int, int, int] | None:
    if not isinstance(raw, (list, tuple)) or len(raw) != 3:
        return None
    try:
        return tuple(int(value) for value in raw)  # type: ignore[return-value]
    except (TypeError, ValueError):
        return None


def select_preflight_cohort(
    resolved: list[dict[str, Any]],
    pending: list[dict[str, Any]],
    events: list[dict[str, Any]],
    battle_times: dict[int, tuple[float, str | None]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """명시적 cohort와 배포 직후 legacy log join을 합쳐 중복 없는 표본을 고른다."""
    attack_events = [dict(row) for row in events if row.get("kind") == "ATTACK"]
    used_events: set[int] = set()
    selected_resolved: list[dict[str, Any]] = []
    selected_pending: list[dict[str, Any]] = []

    def mark_explicit_event(row: dict[str, Any]) -> None:
        try:
            sent_at = float(row.get("sent_at"))
        except (TypeError, ValueError):
            return
        email = row.get("email")
        coord = _coord_tuple(row.get("target"))
        candidates = [
            (abs(float(event["ts"]) - sent_at), index)
            for index, event in enumerate(attack_events)
            if index not in used_events
            and event.get("email") == email
            and (coord is None or _coord_tuple(event.get("coord")) == coord)
        ]
        if candidates and min(candidates)[0] <= 5:
            used_events.add(min(candidates)[1])

    seen_reports: set[int] = set()
    for row in resolved:
        if row.get("cohort") != COHORT:
            continue
        try:
            report_id = int(row["report_id"])
        except (KeyError, TypeError, ValueError):
            continue
        if report_id in seen_reports:
            continue
        item = dict(row)
        item["_cohort_origin"] = "explicit"
        selected_resolved.append(item)
        seen_reports.add(report_id)
        mark_explicit_event(item)

    for row in pending:
        if row.get("cohort") != COHORT:
            continue
        item = dict(row)
        item["_cohort_origin"] = "explicit"
        selected_pending.append(item)
        mark_explicit_event(item)

    # cohort 메타 배포 전의 초기 파견만 복원한다. exact-match 결과+실전 시각+파견 로그의
    # 같은 계정 조건을 만족하고, 각 ATTACK 이벤트는 한 번만 소비한다.
    legacy_candidates: list[tuple[float, dict[str, Any], int]] = []
    for row in (resolved if COHORT == LEGACY_INFERENCE_COHORT else []):
        if row.get("cohort") == COHORT:
            continue
        try:
            report_id = int(row["report_id"])
        except (KeyError, TypeError, ValueError):
            continue
        battle = battle_times.get(report_id)
        if battle is None:
            continue
        battle_ts, battle_email = battle
        email = row.get("email") or battle_email
        if not isinstance(email, str) or (battle_email and email != battle_email):
            continue
        for index, event in enumerate(attack_events):
            if index in used_events or event.get("email") != email:
                continue
            delay = battle_ts - float(event["ts"])
            if 0 <= delay <= LEGACY_JOIN_WINDOW_SEC:
                legacy_candidates.append((delay, row, index))

    # 가장 가까운 파견-전투 조합부터 일대일로 고른다.
    used_legacy_reports: set[int] = set()
    for _delay, row, event_index in sorted(legacy_candidates, key=lambda item: item[0]):
        report_id = int(row["report_id"])
        if report_id in seen_reports or report_id in used_legacy_reports or event_index in used_events:
            continue
        event = attack_events[event_index]
        item = dict(row)
        item["email"] = event["email"]
        item["sent_at"] = float(event["ts"])
        item["target"] = event.get("coord")
        item["cohort"] = COHORT
        item["_cohort_origin"] = "legacy_log_join"
        selected_resolved.append(item)
        used_legacy_reports.add(report_id)
        used_events.add(event_index)

    # 아주 최근 legacy prediction이 아직 pending이면 sent_at+계정+좌표를 직접 맞춘다.
    for row in (pending if COHORT == LEGACY_INFERENCE_COHORT else []):
        if row.get("cohort") == COHORT:
            continue
        try:
            sent_at = float(row.get("sent_at"))
        except (TypeError, ValueError):
            continue
        email = row.get("email")
        coord = _coord_tuple(row.get("target"))
        candidates = [
            (abs(float(event["ts"]) - sent_at), index)
            for index, event in enumerate(attack_events)
            if index not in used_events and event.get("email") == email
            and (coord is None or _coord_tuple(event.get("coord")) == coord)
        ]
        if not candidates or min(candidates)[0] > 5:
            continue
        _, event_index = min(candidates)
        item = dict(row)
        item["cohort"] = COHORT
        item["_cohort_origin"] = "legacy_log_join"
        selected_pending.append(item)
        used_events.add(event_index)

    selected_resolved.sort(key=lambda row: (float(row.get("sent_at", 0) or 0), int(row["report_id"])))
    selected_pending.sort(key=lambda row: float(row.get("sent_at", 0) or 0))
    return selected_resolved, selected_pending, {
        "explicit_resolved": sum(row.get("_cohort_origin") == "explicit" for row in selected_resolved),
        "legacy_inferred_resolved": sum(
            row.get("_cohort_origin") == "legacy_log_join" for row in selected_resolved
        ),
        "explicit_pending": sum(row.get("_cohort_origin") == "explicit" for row in selected_pending),
        "legacy_inferred_pending": sum(
            row.get("_cohort_origin") == "legacy_log_join" for row in selected_pending
        ),
        "unmatched_attack_logs": len(attack_events) - len(used_events),
    }


def _number(row: dict[str, Any], key: str) -> float | None:
    try:
        value = float(row.get(key))
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def _format_local(epoch: float | None) -> str | None:
    if epoch is None or not math.isfinite(epoch):
        return None
    return dt.datetime.fromtimestamp(epoch, LOCAL_TZ).isoformat(timespec="seconds")


def score_cohort(
    resolved: list[dict[str, Any]],
    pending: list[dict[str, Any]],
    *,
    now: float,
    min_reports: int,
    max_loss_error: float,
) -> dict[str, Any]:
    valid = [
        row for row in resolved
        if _number(row, "predicted_loss") is not None and _number(row, "actual_loss") is not None
    ]
    predicted_total = sum(_number(row, "predicted_loss") or 0.0 for row in valid)
    actual_total = sum(_number(row, "actual_loss") or 0.0 for row in valid)
    if actual_total > 0:
        bias: float | None = (predicted_total - actual_total) / actual_total
        error: float | None = abs(bias)
    elif predicted_total == 0:
        bias = error = 0.0
    else:
        bias = error = None

    eligible = len(valid)
    if eligible < min_reports:
        status = "COLLECTING"
    elif error is not None and error <= max_loss_error:
        status = "PASS"
    else:
        status = "FAIL"

    sent_times = [
        value for row in valid + pending
        if (value := _number(row, "sent_at")) is not None and value <= now + 60
    ]
    started_at = min(sent_times) if sent_times else None
    elapsed_hours = max(0.0, (now - started_at) / 3600) if started_at is not None else 0.0
    resolved_rate = eligible / elapsed_hours if elapsed_hours > 0 else 0.0
    prediction_rate = (eligible + len(pending)) / elapsed_hours if elapsed_hours > 0 else 0.0
    if eligible >= min_reports:
        reached = sorted(
            value for row in valid
            if (value := _number(row, "sent_at")) is not None
        )
        n57_at = reached[min_reports - 1] if len(reached) >= min_reports else now
        eta_hours = 0.0
    elif resolved_rate > 0:
        eta_hours = (min_reports - eligible) / resolved_rate
        n57_at = now + eta_hours * 3600
    else:
        eta_hours = None
        n57_at = None

    return {
        "cohort": COHORT,
        "eligible_reports": eligible,
        "invalid_resolved": len(resolved) - eligible,
        "pending_predictions": len(pending),
        "predicted_loss_total": predicted_total,
        "actual_loss_total": actual_total,
        "aggregate_loss_bias": bias,
        "aggregate_loss_error": error,
        "gate_status": status,
        "gate_passed": status == "PASS",
        "gate_min_reports": min_reports,
        "gate_max_loss_error": max_loss_error,
        "cohort_started_at": _format_local(started_at),
        "elapsed_hours": elapsed_hours,
        "exact_match_rate_per_hour": resolved_rate,
        "prediction_rate_per_hour": prediction_rate,
        "estimated_hours_to_n57": eta_hours,
        "estimated_n57_at": _format_local(n57_at),
    }


def run_regrade(
    *,
    state_dir: pathlib.Path = DEFAULT_GUARD_STATE_DIR,
    log_path: pathlib.Path = DEFAULT_LOG_PATH,
    metrics_db: pathlib.Path = DEFAULT_METRICS_DB,
    now: float | None = None,
    min_reports: int = DEFAULT_MIN_REPORTS,
    max_loss_error: float = DEFAULT_MAX_LOSS_ERROR,
) -> dict[str, Any]:
    now = time.time() if now is None else float(now)
    events = parse_preflight_events(log_path)
    known_emails = {str(row["email"]) for row in events if isinstance(row.get("email"), str)}
    resolved, pending = load_guard_rows(state_dir, known_emails)
    selected, selected_pending, origins = select_preflight_cohort(
        resolved, pending, events, load_battle_times(metrics_db),
    )
    result = score_cohort(
        selected, selected_pending, now=now,
        min_reports=max(1, int(min_reports)), max_loss_error=max(0.0, float(max_loss_error)),
    )
    result["cohort_origin_counts"] = origins
    result["preflight"] = summarize_events(events)
    result["sizing_live_auto_changed"] = False
    result["generated_at"] = _format_local(now)
    return result


def _atomic_json(path: pathlib.Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)


def _notification_text(result: dict[str, Any], reasons: list[str]) -> str:
    error = result.get("aggregate_loss_error")
    error_text = "∞" if error is None else f"{float(error):.1%}"
    evidence = result["preflight"]["stale_return_evidence"]
    reason_text = ", ".join(reasons)
    return (
        "📐 OGame sizing 재채점\n"
        f"• 트리거: {reason_text}\n"
        f"• cohort: {result.get('cohort', COHORT)}\n"
        f"• preflight exact-match N={result['eligible_reports']}/{result['gate_min_reports']} "
        f"(pending {result['pending_predictions']})\n"
        f"• 공격손실 예측 {result['predicted_loss_total']:.0f} / "
        f"실제 {result['actual_loss_total']:.0f} / 합계오차 {error_text}\n"
        f"• 판정 {result['gate_status']} "
        f"(기준 ≤{result['gate_max_loss_error']:.0%})\n"
        f"• stale/return 직접차단 {evidence['direct_risk_blocks']}건 "
        f"(복귀·방어 {evidence['returned_fleet_or_defense_blocks']}, "
        f"24h peak {evidence['history_peak_stress_blocks']})\n"
        "• sizing live는 자동 변경하지 않음 — 수동 체크리스트 필요"
    )


def maybe_notify(
    result: dict[str, Any],
    *,
    state_path: pathlib.Path = DEFAULT_NOTIFY_STATE,
    webhook_path: pathlib.Path = DEFAULT_WEBHOOK_FILE,
    sender: Callable[[str, str], None] | None = None,
) -> str:
    """N57 최초/판정변경만 알린다. 실패 시 dedupe를 전진시키지 않아 다음 주기 재시도한다."""
    state = _read_json(state_path) or {}
    cohort = str(result.get("cohort") or COHORT)
    # 다른 위험분포의 N57 알림을 이전 cohort의 dedupe 상태가 삼키지 않게 분리한다.
    if state and state.get("cohort") != cohort:
        state = {}
    status = str(result.get("gate_status"))
    eligible = int(result.get("eligible_reports", 0) or 0)
    threshold = int(result.get("gate_min_reports", DEFAULT_MIN_REPORTS) or DEFAULT_MIN_REPORTS)
    reasons: list[str] = []
    threshold_due = eligible >= threshold and state.get("threshold_notified") is not True
    verdict_due = status in {"PASS", "FAIL"} and state.get("notified_verdict") != status
    if threshold_due:
        reasons.append(f"N≥{threshold} 최초 도달")
    if verdict_due:
        reasons.append(f"판정→{status}")
    if not reasons:
        return "idle"

    # 기존 notifier의 검증/전송기를 재사용한다. 예외 객체에는 URL이 포함될 수 있으므로
    # 호출자에게 문자열화하지 않고 고정된 상태만 반환한다.
    try:
        from scripts import discord_battle_notifier as notifier
    except ImportError:  # 직접 실행 호환
        import discord_battle_notifier as notifier  # type: ignore[no-redef]
    webhook = notifier._read_webhook(webhook_path)
    if webhook is None:
        return "webhook-unavailable"
    send = notifier.discord_sender if sender is None else sender
    try:
        send(webhook, _notification_text(result, reasons))
    except Exception:  # noqa: BLE001 - secret가 든 예외 문자열을 절대 노출하지 않는다.
        return "retry"

    if threshold_due:
        state["threshold_notified"] = True
    if verdict_due:
        state["notified_verdict"] = status
    state["cohort"] = cohort
    state["notified_eligible"] = eligible
    state["updated_at"] = result.get("generated_at")
    try:
        _atomic_json(state_path, state)
    except OSError:
        # POST 후 상태쓰기 실패는 다음 주기에 중복될 수 있지만, 누락보다 안전하다.
        return "retry-state-write"
    return "sent"


def _pct(value: object) -> str:
    return "∞" if value is None else f"{float(value):.1%}"


def print_text(result: dict[str, Any]) -> None:
    print(
        f"preflight exact-match N={result['eligible_reports']}/{result['gate_min_reports']} "
        f"(pending={result['pending_predictions']}, "
        f"명시={result['cohort_origin_counts']['explicit_resolved']}, "
        f"초기복원={result['cohort_origin_counts']['legacy_inferred_resolved']})"
    )
    print(
        f"공격손실 합계: 예측 {result['predicted_loss_total']:.0f} / "
        f"실제 {result['actual_loss_total']:.0f} / "
        f"오차 {_pct(result['aggregate_loss_error'])}"
    )
    print(
        f"생산속도: exact-match {result['exact_match_rate_per_hour']:.2f}건/h / "
        f"prediction {result['prediction_rate_per_hour']:.2f}건/h"
    )
    if result.get("estimated_n57_at"):
        print(
            f"N57 예상: {result['estimated_n57_at']} "
            f"(약 {float(result['estimated_hours_to_n57']):.1f}h)"
        )
    else:
        print("N57 예상: 표본 생산속도 부족으로 계산 불가")
    counts = result["preflight"]["counts"]
    evidence = result["preflight"]["stale_return_evidence"]
    print(
        f"PREFLIGHT: SPY {counts['SPY']} / WAIT {counts['WAIT']} / "
        f"HOLD {counts['HOLD']} / ATTACK {counts['ATTACK']}"
    )
    for reason, count in result["preflight"]["reasons"].items():
        print(f"  - {reason}: {count}")
    print(
        f"stale/return 근거: 재정찰 {evidence['fresh_respy_requests']}, "
        f"복귀·방어 직접차단 {evidence['returned_fleet_or_defense_blocks']}, "
        f"24h peak 차단 {evidence['history_peak_stress_blocks']}"
    )
    print(
        f"GATE {result['gate_status']} "
        f"(N≥{result['gate_min_reports']}, 손실합계 오차≤{result['gate_max_loss_error']:.0%}); "
        "sizing live 자동변경 없음"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=pathlib.Path, default=DEFAULT_GUARD_STATE_DIR)
    parser.add_argument("--log-path", type=pathlib.Path, default=DEFAULT_LOG_PATH)
    parser.add_argument("--metrics-db", type=pathlib.Path, default=DEFAULT_METRICS_DB)
    parser.add_argument("--min-reports", type=int, default=DEFAULT_MIN_REPORTS)
    parser.add_argument("--max-loss-error", type=float, default=DEFAULT_MAX_LOSS_ERROR)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--notify", action="store_true")
    args = parser.parse_args()
    result = run_regrade(
        state_dir=args.state_dir, log_path=args.log_path, metrics_db=args.metrics_db,
        min_reports=args.min_reports, max_loss_error=args.max_loss_error,
    )
    if args.notify:
        result["notification"] = maybe_notify(result)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    else:
        print_text(result)
        if args.notify:
            print(f"알림: {result['notification']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
