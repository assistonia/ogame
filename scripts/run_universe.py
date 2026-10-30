#!/usr/bin/env python3
"""규칙봇·AI 하이브리드 유니버스 스케줄러.

인자를 생략한 단발 실행은 규칙봇 90명이 기본이다. 현재 상시 운영은 watchdog이
규칙봇 90 + AI 10 + pirate/explorer 2의 이메일 102개를 명시하고, 4분 주기·동시 실행
상한 6·``--spread-sec 240``으로 고정 epoch 슬롯에 균등 분산한다. spread가 0이면 기존
일괄 pass이며, 이때만 ``--parallel 1``에서 봇 사이의 짧은 딜레이를 적용한다.

각 봇의 결과는 stdout에 한 줄씩 출력하고 scripts/universe.log 에도 남긴다.
로그인 실패(존재하지 않는 계정, 비번 오류 등)나 HTTP 500 같은 예외는
봇 단위로 잡아서 스킵하고 계속 진행한다.

사용 예:
  # 현재 102계정 상시 운영(정확한 이메일·분산 옵션은 watchdog이 적용)
  ./scripts/watchdog.sh
  # 한 바퀴만 (인자 생략 기본: bot001~bot090)
  python3 scripts/run_universe.py --once
  # 명시한 계정을 4분 주기의 240초 슬롯에 분산(현재 운영 스케줄 형태)
  python3 scripts/run_universe.py --emails "$EMAILS" --loop --interval-min 4 --parallel 6 --spread-sec 240
  # 앞 5명만, 테스트
  python3 scripts/run_universe.py --once --bots 5
  # 특정 계정만 (스케줄러 경로 검증용)
  python3 scripts/run_universe.py --once --emails bot001@bots.local
"""
from __future__ import annotations

import re
import sys
import time
import argparse
import collections
import datetime
import pathlib
import concurrent.futures
import fcntl
import threading
from dataclasses import dataclass

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from bots.rule_based import miner_bot  # noqa: E402
from bots.rule_based.personality import assign_personality, summary  # noqa: E402
from bots.ai_agents import policy_engine  # noqa: E402
from bots.shared import db_reader  # noqa: E402
from bots.shared.client_pool import OGameClientPool  # noqa: E402
from scripts.tick_metrics import TickMetricsWriter  # noqa: E402
from scripts.universe_schedule import DueTick, SpreadSchedule  # noqa: E402

# AI 하이브리드 계정 판별: aiNN과 Spark 전용 실험 계정은 규칙봇 대신
# 정책 엔진으로 집행한다. bot001~bot090은 기존 규칙봇 동작을 유지한다.
_AI_ACCOUNT_RE = re.compile(r"^ai\d+$", re.IGNORECASE)
_NAMED_AI_EMAILS = {"pirate01@bots.local", "explorer01@bots.local"}


def is_ai_account(email: str) -> bool:
    """이 계정이 AI 하이브리드(정책 엔진) 계정인지."""
    normalized = email.lower()
    account = normalized.split("@", 1)[0]
    return normalized in _NAMED_AI_EMAILS or bool(_AI_ACCOUNT_RE.match(account))

LOG_PATH = PROJECT_ROOT / "scripts" / "universe.log"
LOCK_PATH = PROJECT_ROOT / "scripts" / ".run_universe.lock"
CLIENT_POOL = OGameClientPool()
_TICK_METRICS_ERROR_ACTIVE = False


@dataclass(frozen=True)
class TickOutcome:
    kind: str
    line: str
    duration_sec: float
    engine: str
    cycle: int
    skip_reason: str | None = None


class SingletonLock:
    """프로세스 생존 동안 유지되는 non-blocking advisory lock."""
    def __init__(self, path: pathlib.Path):
        self.path = path
        self.handle = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return False
        self.handle = handle
        return True

    def release(self) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None


def build_emails(args: argparse.Namespace) -> list[str]:
    """실행할 봇 이메일 목록 생성."""
    if args.emails:
        return [e.strip() for e in args.emails.split(",") if e.strip()]
    return [
        f"{args.accounts_prefix}{i:03d}@{args.accounts_domain}"
        for i in range(args.start, args.start + args.bots)
    ]


def log_line(line: str) -> None:
    """stdout + universe.log 동시 기록 (타임스탬프 포함)."""
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    stamped = f"{ts} {line}"
    print(stamped, flush=True)
    try:
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(stamped + "\n")
    except OSError as exc:
        print(f"[warn] 로그 파일 기록 실패: {exc}", file=sys.stderr, flush=True)


def _settle_due_fleets(client, email: str) -> bool:
    """Settle lazy fleet arrivals only when the read-only DB says one is due.

    Detection and the best-effort GET are deliberately isolated from the planner.
    A read-replica/DB outage therefore preserves the previous tick behavior, and a
    failed settlement attempt does not reorder or suppress any economy/fleet
    candidate the engine would otherwise evaluate.
    """
    try:
        due = db_reader.has_due_fleet_missions_db(email)
    except Exception:  # noqa: BLE001 - optional read optimization must not break ticks
        return False
    if not due:
        return False
    try:
        client.settle_overview()
    except Exception:  # noqa: BLE001 - planner actions remain the compatibility path
        return False
    return True


def _tick_engine(email: str) -> str:
    return "policy_engine" if is_ai_account(email) else "miner_bot"


def _tick_outcome(
    email: str,
    args: argparse.Namespace,
    tick_index: int,
    cycle: int | None = None,
) -> TickOutcome:
    """Execute and time one account tick, absorbing failures per account."""
    started = time.perf_counter()
    engine = _tick_engine(email)
    metric_cycle = tick_index if cycle is None else cycle
    try:
        # The lease owns this account for the complete tick.  Both engines receive
        # the same process-reused client, so their candidate/action order remains
        # unchanged while Session connections and CSRF caches survive across ticks.
        with CLIENT_POOL.lease(email, args.password) as client:
            # Persistent sessions intentionally avoid a GlobalGame GET on healthy
            # leases.  Restore exactly the settlement request that is semantically
            # needed, and only when FleetMissionService has due work for this user.
            _settle_due_fleets(client, email)
            if is_ai_account(email):
                result = policy_engine.policy_tick(
                    email, password=args.password, tick_index=tick_index, client=client
                )
            else:
                result = miner_bot.tick(
                    email, password=args.password, tick_index=tick_index, client=client
                )
        kind = "idle" if "nothing buildable" in result else "built"
        return TickOutcome(
            kind=kind,
            line=result,
            duration_sec=time.perf_counter() - started,
            engine=engine,
            cycle=metric_cycle,
        )
    except Exception as exc:  # noqa: BLE001 - 봇 단위로 잡고 계속 진행
        pkey = assign_personality(email)
        reason = f"{type(exc).__name__}: {exc}"
        return TickOutcome(
            kind="skipped",
            line=f"SKIP {email} [{pkey}] ({reason})",
            duration_sec=time.perf_counter() - started,
            engine=engine,
            cycle=metric_cycle,
            skip_reason=reason,
        )


def _tick_one(email: str, args: argparse.Namespace, tick_index: int) -> tuple[str, str]:
    """Compatibility wrapper returning the scheduler's original two-tuple."""
    outcome = _tick_outcome(email, args, tick_index)
    return outcome.kind, outcome.line


def _record_tick_metric(
    metrics: TickMetricsWriter | None, email: str, outcome: TickOutcome,
) -> None:
    """Persist a tick without allowing observability failure to stop scheduling."""
    global _TICK_METRICS_ERROR_ACTIVE
    if metrics is None:
        return
    try:
        metrics.record(
            email=email,
            duration_sec=outcome.duration_sec,
            engine=outcome.engine,
            cycle=outcome.cycle,
            result=outcome.kind,
            skip_reason=outcome.skip_reason,
        )
    except Exception as exc:  # noqa: BLE001 - metrics must not break account ticks
        if not _TICK_METRICS_ERROR_ACTIVE:
            log_line(f"[warn] tick metrics 기록 실패: {type(exc).__name__}: {exc}")
        _TICK_METRICS_ERROR_ACTIVE = True
    else:
        _TICK_METRICS_ERROR_ACTIVE = False


def run_pass(
    emails: list[str],
    args: argparse.Namespace,
    tick_index: int,
    metrics: TickMetricsWriter | None = None,
) -> dict:
    """봇 목록을 실행하고 요약 통계를 반환. --parallel N이면 N개 동시 실행."""
    stats = {"built": 0, "idle": 0, "skipped": 0}
    total = len(emails)

    if args.parallel > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.parallel) as pool:
            futures = {
                pool.submit(_tick_outcome, email, args, tick_index): email
                for email in emails
            }
            for n, fut in enumerate(concurrent.futures.as_completed(futures), start=1):
                outcome = fut.result()
                email = futures[fut]
                _record_tick_metric(metrics, email, outcome)
                log_line(f"[{n}/{total}] {outcome.line}")
                stats[outcome.kind] += 1
        return stats

    for n, email in enumerate(emails, start=1):
        outcome = _tick_outcome(email, args, tick_index)
        _record_tick_metric(metrics, email, outcome)
        log_line(f"[{n}/{total}] {outcome.line}")
        stats[outcome.kind] += 1
        # 마지막 봇 뒤에는 딜레이 불필요
        if n < total and args.delay_sec > 0:
            time.sleep(args.delay_sec)
    return stats


def _cycle_stats() -> dict[str, float | int]:
    return {
        "due": 0,
        "finished": 0,
        "built": 0,
        "idle": 0,
        "skipped": 0,
        "max_lag": 0.0,
    }


def run_spread_loop(
    emails: list[str],
    args: argparse.Namespace,
    tick_index: int,
    metrics: TickMetricsWriter | None = None,
) -> None:
    """Run fixed-epoch account ticks, submitting each future only when due.

    Hash rank assigns accounts to exactly even slots across ``spread_sec``.  The
    scheduler advances due times independently of completion, and coalesces an
    occurrence when the same account is still active instead of creating a catch-up
    queue.  ``parallel`` remains the executor ceiling, not a startup burst size.
    """
    period_sec = float(args.interval_min) * 60.0
    schedule = SpreadSchedule(
        emails,
        period_sec=period_sec,
        spread_sec=float(args.spread_sec),
        now=time.time(),
        base_tick_index=tick_index,
    )
    total = len(emails)
    condition = threading.Condition()
    completed: collections.deque[concurrent.futures.Future] = collections.deque()
    pending: dict[concurrent.futures.Future, DueTick] = {}
    cycles: dict[int, dict[str, float | int]] = {}

    def tracked_stats(item: DueTick) -> dict[str, float | int] | None:
        if item.cycle < schedule.first_full_cycle:
            return None
        return cycles.setdefault(item.cycle, _cycle_stats())

    def note_due(item: DueTick) -> dict[str, float | int] | None:
        stats = tracked_stats(item)
        if stats is not None:
            stats["due"] += 1
        return stats

    def finish(
        item: DueTick, kind: str, *, finished_at: float, minimum_lag: float = 0.0,
    ) -> None:
        stats = tracked_stats(item)
        if stats is None:
            return
        stats["finished"] += 1
        stats[kind] += 1
        lag = max(minimum_lag, finished_at - item.due_at)
        stats["max_lag"] = max(float(stats["max_lag"]), lag)

    def finalize_cycles() -> None:
        for cycle in sorted(list(cycles)):
            stats = cycles[cycle]
            if int(stats["due"]) < total or int(stats["finished"]) < total:
                continue
            # In spread mode the comparable capacity signal is worst due-to-finish
            # lag, not the intentionally 240-second first-to-last submission span.
            log_line(
                f"--- 바퀴 완료: built={stats['built']} idle={stats['idle']} "
                f"skipped={stats['skipped']} ({float(stats['max_lag']):.1f}s) ---"
            )
            del cycles[cycle]

    def on_done(future: concurrent.futures.Future) -> None:
        with condition:
            completed.append(future)
            condition.notify()

    def drain_completed() -> None:
        while True:
            with condition:
                if not completed:
                    break
                future = completed.popleft()
            item = pending.pop(future)
            schedule.complete(item.email)
            try:
                outcome = future.result()
            except Exception as exc:  # noqa: BLE001 - defensive; _tick_one normally absorbs
                reason = f"scheduler {type(exc).__name__}: {exc}"
                outcome = TickOutcome(
                    kind="skipped",
                    line=f"SKIP {item.email} ({reason})",
                    duration_sec=0.0,
                    engine=_tick_engine(item.email),
                    cycle=item.cycle,
                    skip_reason=reason,
                )
            finished_at = time.time()
            finish(item, outcome.kind, finished_at=finished_at)
            _record_tick_metric(metrics, item.email, outcome)
            log_line(
                f"[spread {item.stable_order + 1}/{total} cycle={item.cycle}] "
                f"{outcome.line}"
            )

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.parallel) as pool:
        while True:
            # Release account-active flags before deciding whether this epoch's due
            # occurrence is an overrun.
            drain_completed()
            now = time.time()
            batch = schedule.take_due(now)

            for item in batch.overrun:
                note_due(item)
                # A still-running previous occurrence is an explicit capacity miss.
                # Count it as skipped and expose at least one full period of lag to
                # the existing load-trend parser.
                finish(
                    item,
                    "skipped",
                    finished_at=now,
                    minimum_lag=period_sec,
                )
                outcome = TickOutcome(
                    kind="skipped",
                    line=f"SKIP {item.email} (previous tick still running)",
                    duration_sec=0.0,
                    engine=_tick_engine(item.email),
                    cycle=item.cycle,
                    skip_reason="previous tick still running",
                )
                _record_tick_metric(metrics, item.email, outcome)
                log_line(
                    f"[spread {item.stable_order + 1}/{total} cycle={item.cycle}] "
                    f"{outcome.line}"
                )

            for item in batch.ready:
                note_due(item)
                try:
                    future = pool.submit(
                        _tick_outcome,
                        item.email,
                        args,
                        item.tick_index,
                        item.cycle,
                    )
                except Exception as exc:  # noqa: BLE001 - submit failure releases single-flight
                    schedule.complete(item.email)
                    finish(item, "skipped", finished_at=time.time())
                    reason = f"submit {type(exc).__name__}: {exc}"
                    outcome = TickOutcome(
                        kind="skipped",
                        line=f"SKIP {item.email} ({reason})",
                        duration_sec=0.0,
                        engine=_tick_engine(item.email),
                        cycle=item.cycle,
                        skip_reason=reason,
                    )
                    _record_tick_metric(metrics, item.email, outcome)
                    log_line(
                        f"[spread {item.stable_order + 1}/{total} cycle={item.cycle}] "
                        f"{outcome.line}"
                    )
                    continue
                pending[future] = item
                future.add_done_callback(on_done)

            # Very short ticks may have completed during submission.
            drain_completed()
            finalize_cycles()

            next_due = schedule.next_due_at
            now = time.time()
            timeout = 30.0 if next_due is None else max(0.0, min(30.0, next_due - now))
            if timeout <= 0:
                continue
            with condition:
                if not completed:
                    condition.wait(timeout=timeout)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="규칙봇 유니버스 스케줄러")
    ap.add_argument("--bots", type=int, default=90, help="실행할 봇 수 (기본 90)")
    ap.add_argument("--start", type=int, default=1, help="시작 번호 (기본 1)")
    ap.add_argument("--accounts-prefix", default="bot", help="계정 접두사 (기본 bot)")
    ap.add_argument(
        "--accounts-domain", default="bots.local", help="계정 도메인 (기본 bots.local)"
    )
    ap.add_argument("--password", default="botpass", help="봇 계정 비밀번호")
    ap.add_argument(
        "--emails",
        default=None,
        help="쉼표로 구분한 계정 목록 직접 지정 (지정 시 --bots/--prefix 무시)",
    )
    ap.add_argument("--delay-sec", type=float, default=1.0, help="봇 사이 딜레이(초, 순차 모드만)")
    ap.add_argument(
        "--parallel", type=int, default=1,
        help="동시 실행 봇 수 (기본 1=순차). 예: --parallel 20",
    )
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="한 바퀴만 실행 (기본)")
    mode.add_argument("--loop", action="store_true", help="interval-min 간격으로 무한 반복")
    ap.add_argument(
        "--interval-min", type=float, default=60.0, help="--loop 시 바퀴 간격(분)"
    )
    ap.add_argument(
        "--spread-sec",
        type=float,
        default=0.0,
        help=(
            "--loop 계정을 고정 epoch 슬롯에 분산 제출할 구간(초). "
            "0은 기존 일괄 pass, 4분 균등 분산은 240"
        ),
    )
    return ap.parse_args(argv)


def use_spread_scheduler(args: argparse.Namespace) -> bool:
    """Spread is opt-in for loop mode; --once keeps the original pass contract."""
    return bool(args.loop and args.spread_sec > 0)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.parallel < 1:
        print("--parallel must be at least 1", file=sys.stderr)
        return 2
    if args.interval_min <= 0:
        print("--interval-min must be positive", file=sys.stderr)
        return 2
    if args.spread_sec < 0:
        print("--spread-sec cannot be negative", file=sys.stderr)
        return 2
    if use_spread_scheduler(args) and args.spread_sec > args.interval_min * 60:
        print("--spread-sec cannot exceed the loop interval", file=sys.stderr)
        return 2
    singleton = SingletonLock(LOCK_PATH)
    if not singleton.acquire():
        print("run_universe already running", file=sys.stderr)
        return 2
    emails = build_emails(args)

    log_line(
        f"=== universe 시작: {len(emails)}봇, loop={args.loop}, "
        f"spread={args.spread_sec:.1f}s ==="
    )
    # 성격 배정 요약은 bot001~botNN 규칙 계정일 때만 의미가 있다.
    if not args.emails:
        print(summary(args.bots), flush=True)

    # tick_index: 바퀴마다 1씩 증가시켜 성격 사이클이 돌게 한다.
    # --once 라도 실행 시각 기준으로 시작점을 잡아 매번 같은 건물만 짓지 않도록.
    tick_index = int(time.time() // 60)
    metrics = TickMetricsWriter()

    try:
        if use_spread_scheduler(args):
            if not emails:
                log_line("=== 실행할 계정 없음. 종료 ===")
                return 0
            log_line(
                f"--- 분산 스케줄: {len(emails)}계정/{args.spread_sec:.1f}s, "
                f"parallel={args.parallel}, period={args.interval_min * 60:.1f}s ---"
            )
            run_spread_loop(emails, args, tick_index, metrics)
            return 0  # pragma: no cover - loop exits only via interruption

        while True:
            t0 = time.time()
            stats = run_pass(emails, args, tick_index, metrics)
            elapsed = time.time() - t0
            log_line(
                f"--- 바퀴 완료: built={stats['built']} idle={stats['idle']} "
                f"skipped={stats['skipped']} ({elapsed:.1f}s) ---"
            )
            tick_index += 1

            if not args.loop:
                break
            sleep_sec = max(0.0, args.interval_min * 60 - elapsed)
            log_line(f"--- 다음 바퀴까지 {sleep_sec/60:.1f}분 대기 ---")
            time.sleep(sleep_sec)
    except KeyboardInterrupt:
        log_line("=== 사용자 중단(Ctrl-C). 종료 ===")
        return 130
    finally:
        metrics.close()

    log_line("=== universe 종료 ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
