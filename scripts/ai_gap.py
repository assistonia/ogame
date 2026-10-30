#!/usr/bin/env python3
"""AI vs 규칙봇 격차 진단 — 원점수 착시 방지용 "실질자산 점수" 병기.

## 왜 (지표-vs-믿음 착시 백신)
"AI가 25% 느리다"는 원점수(highscores.general = **쓴 자원 누적**) 기준인데, 여기엔
"소비 성향"이 섞여 있다. AI가 자원을 덜 쓰고 창고에 들고 있으면 원점수만 낮게 나온다.
→ **실질자산 점수 = 원점수 + 미소비자원/1000** 을 함께 봐야 진짜 격차가 보인다.
(2026-07-12 실측: 이 지표로도 격차가 안 닫힘 = 실격차 ~25% 확인. 착시가 아니라 실재였음.
 하지만 앞으로 "AI 느림/착시"를 단정하기 전에 이 렌즈부터 보라 — [[metric-vs-belief-illusions]].)

metrics.db(snapshots)만 읽으므로 라이브 DB 부하 없음. AI 성격은 정책 JSON에서 읽는다
(metrics.db 의 personality 컬럼은 AI 계정에서 'ai_agent' 라 무효).

사용: python3 scripts/ai_gap.py
"""
from __future__ import annotations

import json
import pathlib
import sqlite3

ROOT = pathlib.Path(__file__).resolve().parents[1]
DB = ROOT / "scripts" / "metrics.db"
POLICY_DIR = ROOT / "bots" / "ai_agents" / "policies"


def _ai_personas() -> dict[str, str]:
    """aiNN → personality (정책 JSON). 없으면 miner 폴백."""
    out: dict[str, str] = {}
    for i in range(1, 11):
        name = f"ai{i:02d}"
        p = POLICY_DIR / f"{name}.json"
        pers = "miner"
        try:
            pers = json.loads(p.read_text(encoding="utf-8")).get("personality", "miner")
        except (OSError, ValueError):
            pass
        out[f"{name}@bots.local"] = pers
    return out


def _latest(con: sqlite3.Connection, email: str) -> tuple[int, float] | None:
    """가장 최근 스냅샷의 (원점수, 실질자산점수). 실질자산 = 점수 + (M+C+D)/1000."""
    row = con.execute(
        "SELECT score, metal, crystal, deuterium FROM snapshots "
        "WHERE email=? ORDER BY ts DESC LIMIT 1",
        (email,),
    ).fetchone()
    if not row:
        return None
    score = row[0] or 0
    held = ((row[1] or 0) + (row[2] or 0) + (row[3] or 0)) / 1000.0
    return score, score + held


def main() -> None:
    con = sqlite3.connect(DB)
    ai = _ai_personas()
    rule_p: dict[str, list[str]] = {}
    for (email, pers) in con.execute(
        "SELECT DISTINCT email, personality FROM snapshots "
        "WHERE email LIKE 'bot%@bots.local' AND personality IS NOT NULL"
    ).fetchall():
        rule_p.setdefault(pers, []).append(email)

    def cohort(emails: list[str], idx: int) -> float:
        vals = [v[idx] for v in (_latest(con, e) for e in emails) if v]
        return sum(vals) / len(vals) if vals else 0.0

    print("성격       | 원점수 AI/RB | 실질자산 AI/RB | (원점수는 소비성향 섞임 — 실질자산이 진짜)")
    print("-" * 78)
    ai_by_p: dict[str, list[str]] = {}
    for email, pers in ai.items():
        ai_by_p.setdefault(pers, []).append(email)
    for pers in ["miner", "raider", "hybrid", "turtle", "explorer"]:
        ae, re = ai_by_p.get(pers, []), rule_p.get(pers, [])
        if not ae or not re:
            continue
        raw = cohort(ae, 0) / cohort(re, 0) if cohort(re, 0) else 0
        real = cohort(ae, 1) / cohort(re, 1) if cohort(re, 1) else 0
        print(f"{pers:<10} | {raw:>11.2f} | {real:>13.2f} | (AI n={len(ae)}, RB n={len(re)})")

    all_ai = list(ai)
    all_rb = [e for lst in rule_p.values() for e in lst]
    for label, idx in [("원점수", 0), ("실질자산", 1)]:
        a, r = cohort(all_ai, idx), cohort(all_rb, idx)
        print(f"전체 {label}: AI={a:.0f} RB={r:.0f} 비율={a / r:.2f}" if r else f"전체 {label}: n/a")
    con.close()


if __name__ == "__main__":
    main()
