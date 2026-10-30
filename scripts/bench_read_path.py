"""조회 경로 부하 측정: HTTP 파싱 vs DB 직독 (봇당 상태+레벨 조회 1회).

봇의 한 틱 조회 = get_status + get_levels(단계적 페이지). 이 스크립트는 그 조회만
떼어 두 경로의 벽시계 시간을 비교한다(액션 POST 는 제외 — 그건 어차피 HTTP 유지).
로그인은 미리 마치고 세션을 재사용해, 순수 조회 비용만 잰다.
"""
from __future__ import annotations

import sys
import time
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from bots.shared.client import OGameClient  # noqa: E402
from bots.shared import db_reader  # noqa: E402


def http_read(c: OGameClient) -> None:
    c.get_status()
    levels = c.get_levels(pages=("resources", "facilities"))
    if levels.get(31, 0) >= 1:
        c.get_levels(pages=("research",))
    if levels.get(21, 0) >= 1:
        c.get_levels(pages=("shipyard",))


def db_read(c: OGameClient) -> None:
    db_reader.get_status_db(c.email)
    db_reader.get_levels_db(c.email)


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    rounds = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    emails = [f"bot{i:03d}@bots.local" for i in range(1, n + 1)]

    clients = []
    for e in emails:
        c = OGameClient(e, "botpass")
        c.login()
        clients.append(c)

    # 워밍업(세션·DB 커넥션 확립).
    for c in clients:
        db_read(c)

    for label, fn in (("HTTP 파싱", http_read), ("DB 직독", db_read)):
        best = None
        for _ in range(rounds):
            t0 = time.perf_counter()
            for c in clients:
                fn(c)
            dt = time.perf_counter() - t0
            best = dt if best is None else min(best, dt)
        print(f"{label}: {n}봇 조회 {best*1000:8.1f} ms  (봇당 {best/n*1000:6.1f} ms)")


if __name__ == "__main__":
    main()
