#!/bin/bash
# AI 에이전트 오케스트레이터
# ai01~ai10 + pirate01/explorer01의 정책을 갱신한다.
# 한 에이전트가 실패해도 스킵하고 다음으로 계속 진행한다.
#
# 사용법:
#   ./run_ai_agents.sh                        # ai01~ai10 전체 1틱씩
#   ./run_ai_agents.sh --agents "ai01 ai03"   # 일부만 실행
#   ./run_ai_agents.sh --delay 30             # 에이전트 사이 대기(초). 기본 10
#   ./run_ai_agents.sh --password test        # 계정 비번 지정. 기본 botpass
#   ./run_ai_agents.sh --domain ogamex.dev    # 이메일 도메인. 기본 bots.local
#
# 예) test 계정으로 검증:
#   ./run_ai_agents.sh --agents "test5" --domain ogamex.dev --password test

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TICK="$SCRIPT_DIR/../bots/ai_agents/tick.sh"

AGENTS=(ai01 ai02 ai03 ai04 ai05 ai06 ai07 ai08 ai09 ai10 pirate01 explorer01)
DELAY=10
PASSWORD=botpass
DOMAIN=bots.local
PARALLEL=1   # 동시 실행 codex 수. codex 1개당 ~340MB RAM

while [[ $# -gt 0 ]]; do
  case "$1" in
    --agents)   AGENTS=($2); shift 2 ;;   # $2: 공백 기준으로 단어 분할(bash)
    --delay)    DELAY="$2";     shift 2 ;;
    --password) PASSWORD="$2";  shift 2 ;;
    --domain)   DOMAIN="$2";    shift 2 ;;
    --parallel) PARALLEL="$2";  shift 2 ;;
    -h|--help)  grep '^#' "$0"; exit 0 ;;
    *) echo "알 수 없는 옵션: $1" >&2; exit 2 ;;
  esac
done

if [[ ! -x "$TICK" && ! -f "$TICK" ]]; then
  echo "[run] tick.sh 를 찾을 수 없다: $TICK" >&2
  exit 1
fi

total=${#AGENTS[@]}
echo "[run] 대상: ${AGENTS[*]}"
echo "[run] 도메인=$DOMAIN  딜레이=${DELAY}s  전체 $total명"

ok=0
fail=0

if [[ $PARALLEL -gt 1 ]]; then
  # 병렬 모드: 최대 $PARALLEL 개 codex 동시 실행 (개당 ~340MB RAM 고려해서 지정).
  # 각 PID를 직접 wait해 자식 실패를 집계한다. stderr는 상위 ai_loop.log로 흘려서
  # tick 로그를 만들기 전에 죽는 초기화 오류도 숨기지 않는다.
  echo "[run] 병렬 모드: 동시 $PARALLEL개"
  pids=()
  pid_names=()

  reap_oldest() {
    local pid="${pids[0]}"
    local name="${pid_names[0]}"
    local rc
    if wait "$pid"; then
      ((ok++))
    else
      rc=$?
      ((fail++))
      echo "[run] $name@$DOMAIN 실패 (exit $rc)" >&2
    fi
    pids=("${pids[@]:1}")
    pid_names=("${pid_names[@]:1}")
  }

  for name in "${AGENTS[@]}"; do
    email="$name@$DOMAIN"
    echo "[run] $email 시작 $(date '+%H:%M:%S')"
    bash "$TICK" "$email" "$PASSWORD" > /dev/null &
    pids+=("$!")
    pid_names+=("$name")
    if [[ ${#pids[@]} -ge $PARALLEL ]]; then
      reap_oldest
    fi
  done
  while [[ ${#pids[@]} -gt 0 ]]; do
    reap_oldest
  done

  echo "=================================================="
  echo "[run] 종료: 성공 $ok / 실패 $fail / 전체 $total"
  [[ $fail -eq 0 ]]
  exit $?
fi

idx=0
for name in "${AGENTS[@]}"; do
  ((idx++))
  email="$name@$DOMAIN"
  echo "=================================================="
  echo "[run] ($idx/$total) $email 시작 $(date '+%H:%M:%S')"
  if bash "$TICK" "$email" "$PASSWORD"; then
    echo "[run] ($idx/$total) $email 완료"
    ((ok++))
  else
    echo "[run] ($idx/$total) $email 실패 — 스킵하고 계속" >&2
    ((fail++))
  fi
  # 마지막 에이전트 뒤에는 대기하지 않는다
  if [[ $idx -lt $total ]]; then
    echo "[run] 다음 에이전트까지 ${DELAY}s 대기..."
    sleep "$DELAY"
  fi
done

echo "=================================================="
echo "[run] 종료: 성공 $ok / 실패 $fail / 전체 $total"
# 병렬 모드와 동일하게 자식 실패를 종료코드로 전파(수동 실행/워치독이 신뢰 가능).
[[ $fail -eq 0 ]]
exit $?
