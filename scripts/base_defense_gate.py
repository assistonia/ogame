#!/usr/bin/env python3
"""채굴러 idle-fallback 관측 뒤 base-defense 활성 시점을 판정한다.

판정 계약:
  * miner 실질자산(AI 코호트 / 규칙봇 코호트) 비율이 충분한 관측창에서 상승하면 즉시 READY.
  * 상승이 확인되지 않아도 idle-fallback 활성 뒤 24시간이 지나면 READY_DEADLINE.
  * 이 스크립트는 읽기 전용이다. 플래그 변경/프로세스 재시작은 하지 않고 정확한 운영 명령만
    출력한다.

exit code: 0=활성 가능, 1=아직 대기, 2=데이터/인자 오류.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import sqlite3
import statistics
import sys
from dataclasses import asdict, dataclass


ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "scripts" / "metrics.db"
DEFAULT_POLICY_DIR = ROOT / "bots" / "ai_agents" / "policies"

# MINER_IDLE_FALLBACK_ENABLED=True 로 플립한 시각(파일 mtime + 운영 로그 대조).
GATE_STARTED_AT = dt.datetime(2026, 7, 12, 7, 17, 58, tzinfo=dt.timezone.utc)
MAX_WAIT = dt.timedelta(hours=24)

# metrics_collector=5분, universe=4분. 최소 30분/7개 post 스냅샷이면 여러 독립 바퀴를 포함한다.
# 1%p 개선 + 양의 회귀기울기 + 후반 중앙값 상승을 동시에 요구해 단일 스냅샷 튐을 배제한다.
MIN_DATA_WINDOW = dt.timedelta(minutes=30)
MIN_POST_POINTS = 7
MIN_RATIO_GAIN = 0.01
MIN_SLOPE_PER_HOUR = 0.005

FLAG_FILE = ROOT / "bots" / "rule_based" / "miner_defense.py"
FLAG_FROM = "MINER_BASE_DEFENSE_ENABLED = False"
FLAG_TO = "MINER_BASE_DEFENSE_ENABLED = True"


@dataclass(frozen=True)
class GateResult:
    decision: str
    ready: bool
    active: bool
    reason: str
    gate_started_at: str
    deadline_at: str
    checked_at: str
    baseline_ts: str
    latest_ts: str
    data_age_minutes: float
    observation_minutes: float
    post_points: int
    ai_miners: int
    rule_miners: int
    baseline_ratio: float
    latest_ratio: float
    ratio_gain: float
    slope_per_hour: float
    first_half_median: float
    second_half_median: float
    trend_confirmed: bool


def _iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(raw: str) -> dt.datetime:
    value = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def _ai_miners(policy_dir: pathlib.Path) -> list[str]:
    emails: list[str] = []
    for path in sorted(policy_dir.glob("ai*.json")):
        try:
            policy = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(policy, dict) and policy.get("personality", "miner") == "miner":
            emails.append(f"{path.stem}@bots.local")
    return emails


def _flag_is_active(flag_file: pathlib.Path) -> bool:
    """실제 모듈 대입문 한 줄만 읽는다(문서 문자열의 False 언급은 무시)."""
    text = flag_file.read_text(encoding="utf-8")
    matches = re.findall(
        r"^MINER_BASE_DEFENSE_ENABLED\s*=\s*(True|False)\s*(?:#.*)?$",
        text,
        flags=re.MULTILINE,
    )
    if len(matches) != 1:
        raise ValueError("MINER_BASE_DEFENSE_ENABLED 대입문을 정확히 1개 찾지 못함")
    return matches[0] == "True"


def _ratio_series(
    db_path: pathlib.Path, policy_dir: pathlib.Path, start: dt.datetime
) -> tuple[list[tuple[dt.datetime, float]], int, int]:
    ai_emails = _ai_miners(policy_dir)
    if not ai_emails:
        raise ValueError("miner 성격 AI 정책을 찾지 못함")

    con = sqlite3.connect(db_path)
    try:
        rule_emails = [
            row[0]
            for row in con.execute(
                "SELECT DISTINCT email FROM snapshots "
                "WHERE personality='miner' AND email LIKE 'bot%@bots.local'"
            )
        ]
        if not rule_emails:
            raise ValueError("miner 규칙봇 스냅샷을 찾지 못함")

        start_key = _iso(start)
        row = con.execute(
            "SELECT MAX(ts) FROM snapshots WHERE ts <= ?", (start_key,)
        ).fetchone()
        baseline_ts = row[0] if row else None
        if not baseline_ts:
            raise ValueError("게이트 시작 전 baseline 스냅샷이 없음")

        wanted = set(ai_emails) | set(rule_emails)
        placeholders = ",".join("?" for _ in wanted)
        rows = con.execute(
            "SELECT ts,email,COALESCE(score,0) + "
            "(COALESCE(metal,0)+COALESCE(crystal,0)+COALESCE(deuterium,0))/1000.0 "
            f"FROM snapshots WHERE ts >= ? AND email IN ({placeholders}) ORDER BY ts",
            (baseline_ts, *sorted(wanted)),
        ).fetchall()
    finally:
        con.close()

    by_ts: dict[str, dict[str, float]] = {}
    for stamp, email, real_asset in rows:
        by_ts.setdefault(str(stamp), {})[str(email)] = float(real_asset)

    series: list[tuple[dt.datetime, float]] = []
    for stamp, values in sorted(by_ts.items()):
        ai_values = [values[e] for e in ai_emails if e in values]
        rb_values = [values[e] for e in rule_emails if e in values]
        # 부분 수집으로 코호트 평균이 튀는 것을 막는다. 정상 수집은 매 시각 100계정 전부다.
        if len(ai_values) != len(ai_emails) or len(rb_values) != len(rule_emails):
            continue
        rb_mean = statistics.fmean(rb_values)
        if rb_mean <= 0:
            continue
        series.append((_parse_time(stamp), statistics.fmean(ai_values) / rb_mean))

    if len(series) < 2:
        raise ValueError("비율 추세를 계산할 완전한 스냅샷이 부족함")
    return series, len(ai_emails), len(rule_emails)


def _slope_per_hour(series: list[tuple[dt.datetime, float]]) -> float:
    origin = series[0][0]
    xs = [(stamp - origin).total_seconds() / 3600.0 for stamp, _ in series]
    ys = [ratio for _, ratio in series]
    xbar = statistics.fmean(xs)
    ybar = statistics.fmean(ys)
    denom = sum((x - xbar) ** 2 for x in xs)
    if denom <= 0:
        return 0.0
    return sum((x - xbar) * (y - ybar) for x, y in zip(xs, ys)) / denom


def evaluate(
    db_path: pathlib.Path,
    policy_dir: pathlib.Path,
    *,
    now: dt.datetime,
    start: dt.datetime = GATE_STARTED_AT,
    flag_file: pathlib.Path = FLAG_FILE,
) -> GateResult:
    series, ai_count, rb_count = _ratio_series(db_path, policy_dir, start)
    active = _flag_is_active(flag_file)
    baseline_ts, baseline_ratio = series[0]
    latest_ts, latest_ratio = series[-1]
    post = [(stamp, ratio) for stamp, ratio in series if stamp > start]
    slope = _slope_per_hour(series)
    ratios = [ratio for _, ratio in series]
    midpoint = max(1, len(ratios) // 2)
    first_median = statistics.median(ratios[:midpoint])
    second_median = statistics.median(ratios[midpoint:])
    observation = max(dt.timedelta(0), latest_ts - start)
    gain = latest_ratio - baseline_ratio

    trend = (
        len(post) >= MIN_POST_POINTS
        and observation >= MIN_DATA_WINDOW
        and gain >= MIN_RATIO_GAIN
        and slope >= MIN_SLOPE_PER_HOUR
        and second_median > first_median
    )
    deadline = start + MAX_WAIT
    if active:
        decision = "ACTIVE"
        reason = "base-defense가 이미 활성 상태"
        ready = True
    elif now >= deadline:
        decision = "READY_DEADLINE"
        reason = "24h 절대 상한 도달"
        ready = True
    elif trend:
        decision = "READY_TREND"
        reason = "miner 실질자산 AI/RB 비율 상승 추세 확인"
        ready = True
    else:
        decision = "WAIT"
        reason = "추세 확인 전이며 24h 상한 미도달"
        ready = False

    return GateResult(
        decision=decision,
        ready=ready,
        active=active,
        reason=reason,
        gate_started_at=_iso(start),
        deadline_at=_iso(deadline),
        checked_at=_iso(now),
        baseline_ts=_iso(baseline_ts),
        latest_ts=_iso(latest_ts),
        data_age_minutes=max(0.0, (now - latest_ts).total_seconds() / 60.0),
        observation_minutes=observation.total_seconds() / 60.0,
        post_points=len(post),
        ai_miners=ai_count,
        rule_miners=rb_count,
        baseline_ratio=baseline_ratio,
        latest_ratio=latest_ratio,
        ratio_gain=gain,
        slope_per_hour=slope,
        first_half_median=first_median,
        second_half_median=second_median,
        trend_confirmed=trend,
    )


def _operations(flag_file: pathlib.Path = FLAG_FILE) -> list[str]:
    return [
        f"{flag_file}: `{FLAG_FROM}` -> `{FLAG_TO}`",
        "python3 -m py_compile bots/rule_based/miner_defense.py",
        "pytest -q tests/test_miner_defense.py tests/test_base_defense_gate.py",
        "pkill -f '[r]un_universe.py'",
        "./scripts/watchdog.sh",
        "./scripts/status.sh",
        "rg -m 5 'basedef (BASE-DEF|ADAPT-DEF)' scripts/universe.log",
    ]


def _print_human(result: GateResult, flag_file: pathlib.Path = FLAG_FILE) -> None:
    deadline_kst = _parse_time(result.deadline_at).astimezone(
        dt.timezone(dt.timedelta(hours=9))
    )
    print(f"BASE-DEFENSE GATE: {result.decision} — {result.reason}")
    print(
        "miner 실질자산 AI/RB: "
        f"{result.baseline_ratio:.4f} -> {result.latest_ratio:.4f} "
        f"({result.ratio_gain:+.4f}, slope {result.slope_per_hour:+.4f}/h)"
    )
    print(
        f"표본: post {result.post_points}점 / 관측 {result.observation_minutes:.1f}분 / "
        f"AI n={result.ai_miners}, RB n={result.rule_miners} / 데이터 지연 {result.data_age_minutes:.1f}분"
    )
    print(f"24h 절대 마감(KST): {deadline_kst.strftime('%Y-%m-%d %H:%M:%S %z')}")
    if result.active:
        print("이미 활성 상태: 플래그 변경/재시작 절차를 다시 실행하지 않음")
    elif result.ready:
        print("운영 절차(이 스크립트는 실행하지 않음):")
        for index, command in enumerate(_operations(flag_file), start=1):
            print(f"  {index}. {command}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=pathlib.Path, default=DEFAULT_DB)
    parser.add_argument("--policy-dir", type=pathlib.Path, default=DEFAULT_POLICY_DIR)
    parser.add_argument("--flag-file", type=pathlib.Path, default=FLAG_FILE)
    parser.add_argument("--now", help="재현/테스트용 ISO 시각(기본: 현재 UTC)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        now = _parse_time(args.now) if args.now else dt.datetime.now(dt.timezone.utc)
        result = evaluate(
            args.db, args.policy_dir, now=now, flag_file=args.flag_file
        )
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"BASE-DEFENSE GATE ERROR: {exc}", file=sys.stderr)
        return 2

    if args.json:
        payload = asdict(result)
        payload["operations"] = (
            _operations(args.flag_file) if result.ready and not result.active else []
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_human(result, args.flag_file)
    return 0 if result.ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
