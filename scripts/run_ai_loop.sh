#!/bin/bash
# AI 에이전트 12명 상시 루프: 한 바퀴 돌고 INTERVAL_SEC 쉬고 반복.
# ai01~ai10은 Luna 우선 + 한도 시 Spark 폴백, pirate01/explorer01은 Spark 전용이다.
# 사용: nohup ./scripts/run_ai_loop.sh > /dev/null 2>&1 &   (로그: scripts/ai_loop.log)
set -u

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
INTERVAL_SEC="${1:-3600}"  # 바퀴 사이 휴식 (기본 1시간; 하이브리드 = AI 는 정책만 드물게 수립)
PARALLEL="${2:-2}"         # codex 동시 실행 수
LOG="$PROJECT_DIR/scripts/ai_loop.log"

cd "$PROJECT_DIR"
echo "$(date '+%F %T') === AI 루프 시작: interval=${INTERVAL_SEC}s parallel=${PARALLEL} ===" >> "$LOG"

while true; do
  START=$(date +%s)
  ./scripts/run_ai_agents.sh --parallel "$PARALLEL" >> "$LOG" 2>&1
  ELAPSED=$(( $(date +%s) - START ))
  echo "$(date '+%F %T') --- AI 바퀴 완료 (${ELAPSED}s) — ${INTERVAL_SEC}s 대기 ---" >> "$LOG"
  sleep "$INTERVAL_SEC"
done
