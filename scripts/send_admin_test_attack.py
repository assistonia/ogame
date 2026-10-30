#!/usr/bin/env python3
"""전용 Admin의 컨테이너 내부 명령으로 테스트 공격을 보낸다.

비밀번호는 OGameX 컨테이너의 mode-0600 파일 안에만 남고 이 래퍼나 명령행으로
나오지 않는다. ``--process-after-arrival``을 주면 ETA 뒤 정상 게임 서비스로 도착
정산까지 수행한다.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import time


PROJECT = pathlib.Path(__file__).resolve().parents[1]
OGAMEX_DIR = PROJECT.parent / "ogamex"


def _artisan(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "docker", "compose", "exec", "-T", "ogamex-app", "php", "artisan",
            *arguments,
        ],
        cwd=OGAMEX_DIR,
        capture_output=True,
        text=True,
        check=False,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-email", required=True)
    parser.add_argument("--target-planet", required=True)
    parser.add_argument("--light-fighters", type=int, required=True)
    parser.add_argument("--eta-sec", type=int, required=True)
    parser.add_argument("--speed", type=float, default=3.0)
    parser.add_argument(
        "--process-after-arrival",
        action="store_true",
        help="ETA 경과 뒤 정상 게임 서비스로 미션 도착까지 정산",
    )
    args = parser.parse_args()
    if args.light_fighters <= 0 or args.eta_sec < 30 or not 1 <= args.speed <= 10:
        raise SystemExit("invalid ship count, eta, or speed")

    sent = _artisan(
        "ogamex:dev:send-simulation-admin-attack",
        args.target_email,
        args.target_planet,
        f"--light-fighters={args.light_fighters}",
        f"--eta-sec={args.eta_sec}",
        f"--speed={args.speed:g}",
    )
    if sent.stdout:
        print(sent.stdout.rstrip())
    if sent.returncode != 0:
        # Artisan의 안전한 일반 오류만 전달하고, PHP 로그/credential은 출력하지 않는다.
        if sent.stderr:
            print("simulation attack command failed")
        return sent.returncode
    if not args.process_after_arrival:
        return 0

    try:
        payload = json.loads(sent.stdout)
        mission_id = int(payload["mission_id"])
        arrival = int(payload["arrival"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        print("sent mission output could not be verified")
        return 3

    wait_seconds = max(0, arrival - int(time.time())) + 1
    time.sleep(wait_seconds)
    processed = _artisan(
        "ogamex:dev:process-simulation-admin-mission", str(mission_id),
    )
    if processed.stdout:
        print(processed.stdout.rstrip())
    return processed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
