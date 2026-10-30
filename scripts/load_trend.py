#!/usr/bin/env python3
"""유니버스 바퀴 시간의 일중 추세·배포 계단·rolling p90을 계산한다.

운영 규칙:

* ``built+idle+skipped >= 90``인 완주 바퀴만 용량 판정에 쓴다. 배포 확인용 1계정
  ``--once`` 실행은 제외한다.
* 경보 기준은 최근 15개 완주 바퀴(4분 start-to-start 기준 약 1시간)의 선형보간 p90
  150초다. 이 스크립트는 인터벌이나 게임 플래그를 절대 변경하지 않는다.
* sizing tier 계산은 이미 shadow에서 수행 중이다. LIVE는 그 결과를 재사용해야 tier 계산
  증분이 0초가 된다. 현재 함수는 로그 문자열만 반환하므로 플립 구현 때 선택 편대/판정 반환
  리팩터가 필요하다. 재사용/이중계산 두 시나리오를 모두 출력한다.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import pathlib
import re
from dataclasses import dataclass
from typing import Iterable


PROJECT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_LOG = PROJECT / "scripts" / "universe.log"
DEFAULT_EVENTS = PROJECT / "scripts" / "load_events.json"
DEFAULT_WINDOW = 15
DEFAULT_THRESHOLD = 150.0
MIN_COMPLETE_ACCOUNTS = 90

ROUND_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) "
    r"--- 바퀴 완료: built=(?P<built>\d+) idle=(?P<idle>\d+) "
    r"skipped=(?P<skipped>\d+) \((?P<seconds>[0-9.]+)s\) ---$"
)


@dataclass(frozen=True)
class Round:
    at: dt.datetime
    seconds: float
    accounts: int
    sizing_calls: int = 0


@dataclass(frozen=True)
class Event:
    at: dt.datetime
    label: str


def _parse_timestamp(value: str) -> dt.datetime:
    return dt.datetime.strptime(value, "%Y-%m-%d %H:%M:%S")


def parse_rounds(lines: Iterable[str], *, min_accounts: int = MIN_COMPLETE_ACCOUNTS) -> list[Round]:
    """로그에서 완주 바퀴와 해당 바퀴 안의 sizing shadow 호출 수를 복원한다."""
    rounds: list[Round] = []
    sizing_calls = 0
    for raw in lines:
        line = raw.rstrip("\n")
        if "=== universe 시작:" in line:
            # 중간에 끊긴 바퀴의 shadow 호출을 다음 완주 바퀴에 넘기지 않는다.
            sizing_calls = 0
        if "[sizing/shadow]" in line:
            sizing_calls += 1
        match = ROUND_RE.match(line)
        if not match:
            continue
        accounts = sum(int(match.group(key)) for key in ("built", "idle", "skipped"))
        if accounts >= min_accounts:
            rounds.append(Round(
                at=_parse_timestamp(match.group("ts")),
                seconds=float(match.group("seconds")),
                accounts=accounts,
                sizing_calls=sizing_calls,
            ))
        sizing_calls = 0
    return rounds


def read_recent_rounds(path: pathlib.Path, *, tail_bytes: int = 2_000_000) -> list[Round]:
    """watchdog/status용. 성장하는 로그 전체 대신 마지막 2MB만 읽는다."""
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, 2)
            handle.seek(max(0, size - tail_bytes))
            data = handle.read()
    except OSError:
        return []
    text = data.decode("utf-8", errors="ignore")
    if size > tail_bytes:
        text = text.split("\n", 1)[-1]
    return parse_rounds(text.splitlines())


def read_all_rounds(path: pathlib.Path) -> list[Round]:
    try:
        with path.open(encoding="utf-8", errors="ignore") as handle:
            return parse_rounds(handle)
    except OSError:
        return []


def percentile(values: Iterable[float], q: float) -> float | None:
    """numpy 기본 방식과 같은 선형보간 percentile."""
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def stats(rounds: Iterable[Round]) -> dict[str, float | int | None]:
    items = list(rounds)
    values = [row.seconds for row in items]
    return {
        "n": len(items),
        "p50": percentile(values, 0.50),
        "p90": percentile(values, 0.90),
        "max": max(values) if values else None,
        "sizing_calls_per_round": (
            sum(row.sizing_calls for row in items) / len(items) if items else None
        ),
    }


def slope_seconds_per_hour(rounds: Iterable[Round]) -> float | None:
    """바퀴시간 OLS 추세 기울기(sec/hour)."""
    items = list(rounds)
    if len(items) < 2:
        return None
    origin = items[0].at
    xs = [(row.at - origin).total_seconds() / 3600 for row in items]
    ys = [row.seconds for row in items]
    x_mean = sum(xs) / len(xs)
    y_mean = sum(ys) / len(ys)
    denominator = sum((x - x_mean) ** 2 for x in xs)
    if denominator == 0:
        return None
    return sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys)) / denominator


def sizing_seconds_per_call(rounds: Iterable[Round]) -> float | None:
    """단순 관측 회귀. 인과 추정이 아니며 음수도 그대로 보존한다."""
    items = [row for row in rounds if row.sizing_calls >= 0]
    if len(items) < 3 or len({row.sizing_calls for row in items}) < 2:
        return None
    xs = [float(row.sizing_calls) for row in items]
    ys = [row.seconds for row in items]
    x_mean = sum(xs) / len(xs)
    y_mean = sum(ys) / len(ys)
    denominator = sum((x - x_mean) ** 2 for x in xs)
    if denominator == 0:
        return None
    return sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys)) / denominator


def within_segment_slope_seconds_per_hour(
    rounds: list[Round], events: list[Event], day: dt.date,
) -> float | None:
    """배포 계단의 절편을 제거한 구간 내 공통 OLS 기울기(sec/hour)."""
    day_events = [event for event in events if event.at.date() == day]
    if not day_events:
        return None
    end_of_day = dt.datetime.combine(day + dt.timedelta(days=1), dt.time.min)
    numerator = 0.0
    denominator = 0.0
    for index, event in enumerate(day_events):
        end = day_events[index + 1].at if index + 1 < len(day_events) else end_of_day
        segment = [row for row in rounds if event.at <= row.at < end]
        if len(segment) < 2:
            continue
        xs = [(row.at - event.at).total_seconds() / 3600 for row in segment]
        ys = [row.seconds for row in segment]
        x_mean = sum(xs) / len(xs)
        y_mean = sum(ys) / len(ys)
        numerator += sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
        denominator += sum((x - x_mean) ** 2 for x in xs)
    return numerator / denominator if denominator else None


def load_events(path: pathlib.Path) -> list[Event]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    events: list[Event] = []
    for raw in payload.get("events", []):
        try:
            events.append(Event(at=_parse_timestamp(str(raw["at"])), label=str(raw["label"])))
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(events, key=lambda event: event.at)


def event_segments(rounds: list[Round], events: list[Event], day: dt.date) -> list[dict]:
    day_events = [event for event in events if event.at.date() == day]
    if not day_events:
        return []
    end_of_day = dt.datetime.combine(day + dt.timedelta(days=1), dt.time.min)
    rows: list[dict] = []
    for index, event in enumerate(day_events):
        end = day_events[index + 1].at if index + 1 < len(day_events) else end_of_day
        segment = [row for row in rounds if event.at <= row.at < end]
        rows.append({
            "at": event.at.strftime("%H:%M:%S"),
            "label": event.label,
            **stats(segment),
        })
    return rows


def hourly_stats(rounds: list[Round], day: dt.date) -> list[dict]:
    result: list[dict] = []
    for hour in range(24):
        bucket = [row for row in rounds if row.at.date() == day and row.at.hour == hour]
        if bucket:
            result.append({"hour": f"{hour:02d}", **stats(bucket)})
    return result


def report(rounds: list[Round], events: list[Event], day: dt.date, window: int, threshold: float) -> dict:
    day_rounds = [row for row in rounds if row.at.date() == day]
    recent = rounds[-window:]
    recent_stats = stats(recent)
    day_events = [event for event in events if event.at.date() == day]
    first_event_at = day_events[0].at if day_events else dt.datetime.combine(day, dt.time.min)
    post_shadow = [row for row in day_rounds if row.at >= first_event_at]
    stable_at = dt.datetime.combine(day, dt.time(17, 48))
    post_stable = [row for row in day_rounds if row.at >= stable_at]
    p90 = recent_stats["p90"]
    return {
        "date": day.isoformat(),
        "eligible_rounds": len(day_rounds),
        "day_slope_seconds_per_hour": slope_seconds_per_hour(day_rounds),
        "post_first_event_slope_seconds_per_hour": slope_seconds_per_hour(post_shadow),
        "within_event_slope_seconds_per_hour": within_segment_slope_seconds_per_hour(
            day_rounds, day_events, day,
        ),
        "post_stable_slope_seconds_per_hour": slope_seconds_per_hour(post_stable),
        "hourly": hourly_stats(day_rounds, day),
        "events": event_segments(day_rounds, events, day),
        "rolling": {
            **recent_stats,
            "window": window,
            "threshold": threshold,
            "status": "BREACH" if p90 is not None and p90 >= threshold else "OK",
        },
        "sizing": {
            "shadow_seconds_per_call_observed": sizing_seconds_per_call(post_shadow),
            "shadow_calls_per_round_rolling": recent_stats["sizing_calls_per_round"],
            "microbenchmark_seconds_per_call": {
                "defense_zero": 0.001,
                "rocket_launcher_100": 0.299,
                "mixed_defense": 0.509,
                "heavy_return_fleet": 1.287,
            },
            "live_projection_seconds_per_round": {
                "reuse_tier_cpu": [0.0, 0.0],
                "reuse_total": [3.0, 10.0],
                "recompute_tier_cpu_typical": [1.3, 5.7],
                "recompute_tier_cpu_burst_max": 10.3,
                "recompute_total": [5.0, 20.0],
            },
            "reuse_ready_now": False,
            "reason": (
                "현재 _sizing_shadow_log는 문자열만 반환; LIVE 플립 전에 선택 편대/판정을 "
                "반환·재사용해야 함"
            ),
        },
        "auto_interval_changed": False,
    }


def _fmt(value: float | int | None) -> str:
    return "-" if value is None else f"{value:.1f}"


def print_text(result: dict) -> None:
    rolling = result["rolling"]
    print(
        f"LOAD {result['date']} n={result['eligible_rounds']} "
        f"day={_fmt(result['day_slope_seconds_per_hour'])}s/h "
        f"post-event={_fmt(result['post_first_event_slope_seconds_per_hour'])}s/h "
        f"within-step={_fmt(result['within_event_slope_seconds_per_hour'])}s/h "
        f"stable={_fmt(result['post_stable_slope_seconds_per_hour'])}s/h"
    )
    print(
        f"rolling{rolling['window']} p50={_fmt(rolling['p50'])}s "
        f"p90={_fmt(rolling['p90'])}s max={_fmt(rolling['max'])}s "
        f"=> {rolling['status']} (임계 {rolling['threshold']:.0f}s, 자동변경 없음)"
    )
    print("배포 계단:")
    for row in result["events"]:
        print(
            f"  {row['at']} {row['label']} | n={row['n']} "
            f"p50={_fmt(row['p50'])} p90={_fmt(row['p90'])} max={_fmt(row['max'])}"
        )
    print("시간대 추세:")
    for row in result["hourly"]:
        print(
            f"  {row['hour']}시 n={row['n']} p50={_fmt(row['p50'])} "
            f"p90={_fmt(row['p90'])} max={_fmt(row['max'])}"
        )
    sizing = result["sizing"]
    projection = sizing["live_projection_seconds_per_round"]
    print(
        "sizing LIVE 예상: 결과 재사용 시 총 "
        f"+{projection['reuse_total'][0]:.0f}~{projection['reuse_total'][1]:.0f}s/round, "
        f"이중계산 시 +{projection['recompute_total'][0]:.0f}~"
        f"{projection['recompute_total'][1]:.0f}s/round; "
        f"shadow 관측회귀={_fmt(sizing['shadow_seconds_per_call_observed'])}s/call"
    )
    print(f"  LIVE 재사용 준비={sizing['reuse_ready_now']} — {sizing['reason']}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", type=pathlib.Path, default=DEFAULT_LOG)
    parser.add_argument("--events", type=pathlib.Path, default=DEFAULT_EVENTS)
    parser.add_argument("--date", type=dt.date.fromisoformat)
    parser.add_argument("--window", type=int, default=DEFAULT_WINDOW)
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--metric", choices=("rolling_p90",),
        help="watchdog/status용 숫자 한 개만 출력",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="rolling p90이 임계 이상이면 exit 2, 표본 부족이면 exit 3",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.window < 1:
        raise SystemExit("--window must be >= 1")
    if args.metric or args.check:
        rounds = read_recent_rounds(args.log)
    else:
        rounds = read_all_rounds(args.log)
    if args.metric == "rolling_p90":
        value = percentile((row.seconds for row in rounds[-args.window:]), 0.90)
        if value is None:
            return 3
        print(f"{value:.1f}")
        return 0
    if args.check:
        if len(rounds) < args.window:
            print(f"INSUFFICIENT n={len(rounds)}/{args.window}")
            return 3
        value = percentile((row.seconds for row in rounds[-args.window:]), 0.90)
        assert value is not None
        status = "BREACH" if value >= args.threshold else "OK"
        print(f"{status} p90={value:.1f}s n={args.window} threshold={args.threshold:.0f}s")
        return 2 if status == "BREACH" else 0

    if args.date is not None:
        day = args.date
    elif rounds:
        day = rounds[-1].at.date()
    else:
        day = dt.datetime.now().date()
    result = report(rounds, load_events(args.events), day, args.window, args.threshold)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        print_text(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
