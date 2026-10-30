#!/bin/bash
# 루프 사망 감지 → 자동 재시작 워치독. crontab에서 2분마다 실행.
# flock으로 중복 실행 방지. 재시작 이력은 scripts/watchdog.log 에 남긴다.
#   crontab: */2 * * * * /home/pyongjoo/Code/80_ogame_simulation/scripts/watchdog.sh
PROJECT="/home/pyongjoo/Code/80_ogame_simulation"
PYTHON="/usr/bin/python3"
# v2: 기존 lock 은 자식 프로세스가 FD 를 상속해 영구 점유하는 버그가 있었음 (9>&- 로 수정).
# 경로를 바꿔 죽은 잠금에서 즉시 벗어난다.
LOCK="/tmp/ogame_watchdog_v2.lock"
LOG="$PROJECT/scripts/watchdog.log"
UNIVERSE_RUNNER_ERR="$PROJECT/scripts/universe_runner.err"
UNIVERSE_PROCESS_PATTERN='^([^[:space:]]*/)?python3([.][0-9]+)?[[:space:]]+([^[:space:]]*/)?scripts/run_universe[.]py([[:space:]]|$)'

exec 9>"$LOCK"
flock -n 9 || exit 0   # 이미 다른 워치독이 도는 중이면 조용히 종료

cd "$PROJECT" || exit 1

# 심박: 매 순찰마다 갱신 — status.sh 가 경과시간을 표시한다 (5분 초과 = 워치독 사망 신호)
touch "$PROJECT/scripts/.watchdog_heartbeat"

# 선행조건: 게임 서버가 응답해야 루프를 살린다.
# (서버가 죽어있으면 루프 재시작이 실패 루프만 만든다 — 서버 복구는 docker restart 정책 몫)
if ! curl -s -m 5 -o /dev/null http://localhost/login; then
  echo "$(date '+%F %T') 서버 무응답 — 루프 재시작 보류" >> "$PROJECT/scripts/watchdog.log"
  exit 0
fi

# Discord 알림 (OGame 전용 웹훅). 실패해도 워치독은 계속.
# 다른 프로젝트의 .env를 재사용하면 알림이 그 프로젝트 채널로 섞이므로 전용 파일만 읽는다.
WEBHOOK_FILE="$PROJECT/.discord_webhook_url"
WEBHOOK=$(tr -d '\r\n' 2>/dev/null < "$WEBHOOK_FILE")
alert() {
  [ -n "$WEBHOOK" ] && curl -s -m 10 -H "Content-Type: application/json" \
    -d "{\"content\":\"🛰️ [ogame] $1\"}" "$WEBHOOK" > /dev/null 2>&1
}
note() { echo "$(date '+%F %T') $1" >> "$LOG"; alert "$1"; }

# 1) 유니버스 루프 (규칙봇 90 + AI 하이브리드 10 + 실험 2 = 102계정)
if ! pgrep -f "$UNIVERSE_PROCESS_PATTERN" > /dev/null; then
  EMAILS=$("$PYTHON" -c "print(','.join([f'bot{i:03d}@bots.local' for i in range(1,91)] + [f'ai{i:02d}@bots.local' for i in range(1,11)] + ['pirate01@bots.local','explorer01@bots.local']))")
  # 계정 별 배치 rollout 파일: ai01 -> AI 10 -> 규칙 표본 -> * 순으로 확대.
  # 파일이 없거나 비어 있으면 클라이언트 배치는 fail-closed(OFF).
  BATCH_ACCOUNTS_FILE="$PROJECT/.economy_batch_accounts"
  BATCH_ACCOUNTS=$(tr -d '\r\n ' 2>/dev/null < "$BATCH_ACCOUNTS_FILE")
  if [ -n "$BATCH_ACCOUNTS" ]; then
    BATCH_ENABLED=1
  else
    BATCH_ENABLED=0
  fi
  # 정상 활동은 universe.log에 남고, 여기에는 시작/치명적 오류의 stderr만 보존한다.
  # 루프가 정지한 시작 시점에만 1MiB 초과 로그를 단일 백업으로 회전한다.
  RUNNER_ERR_SIZE=$(stat -c %s -- "$UNIVERSE_RUNNER_ERR" 2>/dev/null || echo 0)
  if [ "$RUNNER_ERR_SIZE" -gt 1048576 ]; then
    mv -f -- "$UNIVERSE_RUNNER_ERR" "$UNIVERSE_RUNNER_ERR.1"
  fi
  # 102계정을 600초에 균등 분산(약 5.88초 간격). parallel=6은 건강한 상태에서
  # 상한일 뿐이고, due-time 스케줄러가 시작 burst를 막는다.
  OGAME_ECONOMY_BATCH_ENABLED="$BATCH_ENABLED" \
  OGAME_ECONOMY_BATCH_ACCOUNTS="$BATCH_ACCOUNTS" \
  nohup nice -n 10 "$PYTHON" scripts/run_universe.py --emails "$EMAILS" \
    --loop --interval-min 10 --parallel 6 --spread-sec 600 \
    > /dev/null 2>> "$UNIVERSE_RUNNER_ERR" 9>&- &
  note "run_universe 재시작 (pid $!, interval=10m spread=600s parallel=6 batch=${BATCH_ACCOUNTS:-off})"
fi

# 2) AI 정책 수립 루프 (ai 10 + Spark 전용 pirate/explorer, 1시간마다 갱신)
if ! pgrep -f "run_ai_loop.sh" > /dev/null; then
  nohup nice -n 10 ./scripts/run_ai_loop.sh 3600 2 > /dev/null 2>&1 9>&- &
  note "run_ai_loop 재시작 (pid $!)"
fi

# 3) 메트릭 컬렉터
if ! pgrep -f "metrics_collector.py" > /dev/null; then
  nohup nice -n 10 "$PYTHON" scripts/metrics_collector.py > /dev/null 2>&1 9>&- &
  note "metrics_collector 재시작 (pid $!)"
fi

# 4) preflight 이후 sizing 정확도 자동 재채점. 게임/라이브 플래그는 읽기만 하고,
# N>=57 최초 도달 또는 이후 PASS/FAIL 변경 때만 dedupe된 Discord 알림을 보낸다.
"$PYTHON" "$PROJECT/scripts/sizing_regrade.py" --notify > /dev/null 2>&1 || true

# 5) 이상 감지 (재시작/interval 변경은 안 함).
# 구조적 용량 경보: 최근 15개 완주 바퀴(10분 주기 기준 약 2.5h) p90 >=150s.
# 전이 때만 알리고, 단일 바퀴 >300s는 기존 severe 경보로 별도 유지한다.
P90_STATE="/tmp/ogame_round_p90_state"
P90_LINE=$("$PYTHON" scripts/load_trend.py --check --window 15 --threshold 150 2>/dev/null)
P90_RC=$?
P90_PREV=$(cat "$P90_STATE" 2>/dev/null)
if [ "$P90_RC" -eq 2 ]; then
  if [ "$P90_PREV" != "BREACH" ]; then
    note "용량 경고: ${P90_LINE} — 10분 interval 유지/용량 검토 필요 (자동변경 없음)"
  fi
  printf '%s\n' "BREACH" > "$P90_STATE"
  chmod 600 "$P90_STATE"
elif [ "$P90_RC" -eq 0 ]; then
  if [ "$P90_PREV" = "BREACH" ]; then
    note "용량 정상화: ${P90_LINE} — 자동변경 없음"
  fi
  printf '%s\n' "OK" > "$P90_STATE"
  chmod 600 "$P90_STATE"
fi

# severe/디스크 경보. 30분 dedupe로 매 2분 스팸을 막는다.
ROUND=$(grep "바퀴 완료" scripts/universe.log 2>/dev/null | tail -1 | grep -oP '\(\K[0-9.]+(?=s\))')
MARK="/tmp/ogame_round_warned"
if [ -n "$ROUND" ] && "$PYTHON" -c "exit(0 if float('$ROUND') > 300 else 1)"; then
  if [ ! -f "$MARK" ] || [ $(( $(date +%s) - $(stat -c %Y "$MARK") )) -gt 1800 ]; then
    touch "$MARK"
    note "경고: 바퀴 시간 ${ROUND}s (>300s)"
  fi
fi
DISK_PCT=$(df / | awk 'NR==2 {gsub("%",""); print $5}')
if [ "$DISK_PCT" -gt 90 ]; then
  note "경고: 디스크 사용률 ${DISK_PCT}%"
fi
