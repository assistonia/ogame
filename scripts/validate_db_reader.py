"""db_reader(DB 직독) vs client(HTTP 파싱) 대조 검증.

각 계정에 대해:
  1) overview GET 1회로 서버 정산을 강제(현재 행성 기준)한 직후,
  2) HTTP get_status/get_levels 와 DB get_status_db/get_levels_db 를 각각 읽어 비교.
정산 직후에 읽으므로 자원 추정 오차는 "정산~DB읽기" 사이 경과분(수백 ms)만 남는다.
레벨은 100% 일치해야 한다.
"""
from __future__ import annotations

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from bots.shared.client import OGameClient  # noqa: E402
from bots.shared import db_reader  # noqa: E402
from bots.shared.game_objects import name_of  # noqa: E402


import re

_RES_RE = {
    k: re.compile(rf'id="resources_{k}".*?data-raw="([\d.-]+)"', re.DOTALL)
    for k in ("metal", "crystal", "deuterium", "energy")
}


def compare(email: str, password: str = "botpass") -> dict:
    c = OGameClient(email, password)
    c.login()
    # overview GET 1회로 정산을 강제. 자원은 이 **같은 HTML** 에서 파싱하고 곧바로 DB 를
    # 읽어, 두 값이 동일 정산 시점을 보게 한다(2차 GET 을 끼우면 라이브 루프가 그 사이
    # 값을 바꿔 레이스 노이즈가 낀다).
    html = c.overview_html()
    http_status = {}
    for k, rx in _RES_RE.items():
        m = rx.search(html)
        http_status[k] = int(float(m.group(1))) if m else None
    db_status = db_reader.get_status_db(email)

    http_levels = c.get_levels()  # 기본 4페이지(resources+facilities+research+shipyard)
    db_levels = db_reader.get_levels_db(email)

    # 레벨 대조: HTTP 가 조회한 tid 만 비교(DB 는 방어/일부 함선까지 상위집합).
    level_mismatch = {}
    for tid, hv in http_levels.items():
        dv = db_levels.get(tid, 0)
        if hv != dv:
            level_mismatch[name_of(tid)] = {"http": hv, "db": dv}

    # 자원 대조.
    res_diff = {}
    for k in ("metal", "crystal", "deuterium", "energy"):
        hv = http_status.get(k)
        dv = db_status.get(k)
        if hv is None:
            continue
        denom = max(1, abs(hv))
        res_diff[k] = {"http": hv, "db": dv, "abs": dv - hv, "pct": (dv - hv) / denom * 100}

    return {
        "email": email,
        "level_total_http": len(http_levels),
        "level_mismatch": level_mismatch,
        "res_diff": res_diff,
    }


def main() -> None:
    emails = sys.argv[1:] or [f"bot{i:03d}@bots.local" for i in range(1, 11)]
    total_levels = 0
    total_mismatch = 0
    worst_pct = 0.0
    for email in emails:
        try:
            r = compare(email)
        except Exception as exc:  # noqa: BLE001
            print(f"{email}: ERROR {type(exc).__name__}: {exc}")
            continue
        total_levels += r["level_total_http"]
        total_mismatch += len(r["level_mismatch"])
        rd = r["res_diff"]
        mm = r["level_mismatch"]
        line = f"{email}: levels {r['level_total_http']}개, 불일치 {len(mm)}"
        if mm:
            line += f" {mm}"
        for k, d in rd.items():
            if k == "energy":
                continue  # energy 는 시간 무관, 정확 일치 기대
            worst_pct = max(worst_pct, abs(d["pct"]))
        # 자원 오차 요약(metal/crystal/deut 의 절대·%)
        rsum = ", ".join(
            f"{k}Δ{d['abs']:+d}({d['pct']:+.2f}%)" for k, d in rd.items()
        )
        line += f" | {rsum}"
        print(line)
    print("-" * 60)
    match_rate = (total_levels - total_mismatch) / max(1, total_levels) * 100
    print(f"레벨 일치율: {match_rate:.2f}% ({total_levels - total_mismatch}/{total_levels})")
    print(f"자원(metal/crystal/deut) 최대 절대오차: {worst_pct:.3f}%")


if __name__ == "__main__":
    main()
