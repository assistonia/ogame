"""Spark 한도 초과 시 재시도 금지 시각(epoch 초)을 stdout으로 출력한다.

방금 실패한 codex 호출이 남긴 세션 로그(~/.codex/sessions)의 rate_limits
스냅샷에서 리셋 시각을 읽는다:
- 주간(secondary) 한도가 찼으면 → 주간 리셋 시각까지 Spark 금지
- 5시간(primary) 한도만 찼으면 → 5시간 리셋 시각까지 금지
- 스냅샷을 못 읽으면 → 지금 + 5시간 (보수적 기본값)
"""
import glob
import json
import os
import time

DEFAULT = int(time.time()) + 5 * 3600
EXHAUSTED = 95.0  # used_percent 이 값 이상이면 소진으로 판정


def find_rate_limits(obj):
    if isinstance(obj, dict):
        if "rate_limits" in obj:
            return obj["rate_limits"]
        for v in obj.values():
            r = find_rate_limits(v)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_rate_limits(v)
            if r is not None:
                return r
    return None


def latest_snapshot():
    files = sorted(
        glob.glob(os.path.expanduser("~/.codex/sessions/*/*/*/*.jsonl")),
        key=os.path.getmtime,
        reverse=True,
    )
    # 방금 실패한 호출의 세션이 최신 파일이다. 혹시 비어 있으면 몇 개 더 본다.
    for path in files[:5]:
        last = None
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    if '"rate_limits"' not in line:
                        continue
                    try:
                        rl = find_rate_limits(json.loads(line))
                    except ValueError:
                        continue
                    if rl:
                        last = rl
        except OSError:
            continue
        if last:
            return last
    return None


def main():
    rl = latest_snapshot()
    if not rl:
        print(DEFAULT)
        return
    primary = rl.get("primary") or {}
    secondary = rl.get("secondary") or {}
    if secondary.get("used_percent", 0) >= EXHAUSTED and secondary.get("resets_at"):
        print(int(secondary["resets_at"]))
    elif primary.get("used_percent", 0) >= EXHAUSTED and primary.get("resets_at"):
        print(int(primary["resets_at"]))
    else:
        print(DEFAULT)


if __name__ == "__main__":
    main()
