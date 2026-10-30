#!/usr/bin/env python3
"""규칙봇 90개 + AI 10개 계정에 OGameX 캐릭터 클래스를 HTTP 로 배포.

성격(personality) → 클래스 매핑으로 bot001~bot090 에 클래스를 선택시킨다.
  miner / turtle   → COLLECTOR(1)  (광산 +25%, 에너지 +10%)
  raider / hybrid  → GENERAL(2)
  explorer         → DISCOVERER(3)

첫 선택은 무료(users.character_class_free_used=0). 이미 선택된 계정은 스킵한다
(재선택은 500k Dark Matter 가 들어 실패하므로 idempotent 하게 건너뛴다).
AI 계정은 policy_engine의 고유 fallback 성격을 사용한다. 사람 계정은 대상이 아니다.

사용:
  python3 scripts/assign_classes.py            # 실제 배포
  python3 scripts/assign_classes.py --dry-run  # 매핑만 출력, HTTP 호출 없음
"""
from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from bots.rule_based.personality import assign_personality  # noqa: E402
from bots.ai_agents.policy_engine import AI_PERSONALITY_FALLBACK  # noqa: E402
from bots.shared.client import OGameClient  # noqa: E402

PASSWORD = "botpass"
BOT_RANGE = range(1, 91)  # bot001 ~ bot090
AI_RANGE = range(1, 11)   # ai01 ~ ai10
EXPERIMENTAL_EMAILS = ["pirate01@bots.local", "explorer01@bots.local"]

# 성격 → CharacterClass 값 (app/Enums/CharacterClass.php)
PERSONALITY_TO_CLASS: dict[str, int] = {
    "miner": 1,     # COLLECTOR
    "turtle": 1,    # COLLECTOR
    "raider": 2,    # GENERAL
    "hybrid": 2,    # GENERAL
    "explorer": 3,  # DISCOVERER
}
CLASS_NAME = {1: "COLLECTOR", 2: "GENERAL", 3: "DISCOVERER"}


def emails() -> list[str]:
    return (
        [f"bot{i:03d}@bots.local" for i in BOT_RANGE]
        + [f"ai{i:02d}@bots.local" for i in AI_RANGE]
        + EXPERIMENTAL_EMAILS
    )


def personality_for_email(email: str) -> str:
    """규칙봇은 해시 배정, AI는 정책 만료에도 유지되는 고유 페르소나를 쓴다."""
    localpart = email.split("@", 1)[0]
    if localpart.startswith("ai"):
        return AI_PERSONALITY_FALLBACK.get(localpart, "miner")
    return assign_personality(email)


def class_plan() -> list[tuple[str, str, int]]:
    plan: list[tuple[str, str, int]] = []
    for email in emails():
        pkey = personality_for_email(email)
        plan.append((email, pkey, PERSONALITY_TO_CLASS[pkey]))
    return plan


def current_classes() -> dict[str, int | None]:
    """이미 선택된 클래스를 DB(botread, SELECT 전용)에서 조회해 idempotency 판단.

    DB 접근 실패 시 빈 dict 을 반환한다(모두 미선택으로 간주 → select 시도).
    """
    try:
        out = subprocess.run(
            [
                "mysql", "-h", "127.0.0.1", "-P", "3306",
                "-ubotread", "-pbotread", "laravel", "-N", "-B", "-e",
                "SELECT email, character_class FROM users "
                "WHERE email LIKE 'bot%@bots.local' OR email LIKE 'ai%@bots.local';",
            ],
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        print(f"[warn] DB 조회 실패, 모두 미선택으로 간주: {e}", file=sys.stderr)
        return {}
    result: dict[str, int | None] = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        email, cc = parts
        result[email] = None if cc in ("NULL", "") else int(cc)
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="매핑만 출력")
    args = ap.parse_args()

    existing = current_classes()

    plan = class_plan()

    from collections import Counter
    dist = Counter(cid for _, _, cid in plan)
    print("=== 배정 계획 (성격→클래스) ===")
    print("클래스 분포:", {CLASS_NAME[k]: v for k, v in sorted(dist.items())})

    if args.dry_run:
        for email, pkey, cid in plan:
            print(f"  {email}  {pkey:8s} -> {CLASS_NAME[cid]}")
        return 0

    ok: list[str] = []
    skipped: list[str] = []
    failed: list[tuple[str, str]] = []

    for email, pkey, cid in plan:
        if existing.get(email) is not None:
            skipped.append(email)
            continue
        try:
            client = OGameClient(email, PASSWORD)
            client.login()
            resp = client.select_class(cid)
            if resp.get("status") == "success":
                ok.append(email)
                print(f"[ok]   {email}  {pkey:8s} -> {CLASS_NAME[cid]}")
            else:
                msg = resp.get("message", str(resp))
                failed.append((email, msg))
                print(f"[fail] {email}  {pkey:8s} -> {CLASS_NAME[cid]}: {msg}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001 — 한 계정 실패가 배포를 멈추면 안 됨
            failed.append((email, repr(e)))
            print(f"[err]  {email}: {e!r}", file=sys.stderr)

    print("\n=== 결과 ===")
    print(f"성공: {len(ok)}  스킵(기선택): {len(skipped)}  실패: {len(failed)}")
    if failed:
        print("실패 계정:")
        for email, msg in failed:
            print(f"  {email}: {msg}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
