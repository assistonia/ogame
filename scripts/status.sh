#!/bin/bash
# 유니버스 운영 상태 한 줄 요약.
# 사용: ./scripts/status.sh   (자세히: ./scripts/status.sh -v)
PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT"

# 프로세스 생존
UNIV=$(pgrep -f "run_universe.py" | head -1)
AILOOP=$(pgrep -f "run_ai_loop.sh" | head -1)
COLL=$(pgrep -f "metrics_collector.py" | head -1)
ALIVE=$(( ${UNIV:+1}+0 + ${AILOOP:+1}+0 + ${COLL:+1}+0 ))

# 최근 바퀴
LAST_ROUND=$(grep "바퀴 완료" scripts/universe.log 2>/dev/null | tail -1)
ROUND_SEC=$(echo "$LAST_ROUND" | grep -oP '\(\K[0-9.]+(?=s\))')
ROUND_AGE=$(( $(date +%s) - $(date -d "$(echo "$LAST_ROUND" | cut -d' ' -f1,2)" +%s 2>/dev/null || echo 0) ))
# 순간 loadavg와 별개로 구조적 용량 상승을 보는 약 1시간 rolling p90.
# 150초는 운영 검토 경보선일 뿐 status가 interval을 바꾸지는 않는다.
ROUND_P90=$(python3 scripts/load_trend.py --metric rolling_p90 --window 15 2>/dev/null)

# CPU (1분 로드 / 코어수)
LOAD=$(cut -d' ' -f1 /proc/loadavg)
CORES=$(nproc)
CPU_PCT=$(awk -v l="$LOAD" -v c="$CORES" 'BEGIN {printf "%.0f", l/c*100}')

# 디스크
DISK=$(df -h / | awk 'NR==2 {print $4" 남음("$5" 사용)"}')

# 워치독 심박 (5분 초과 = 사망 의심)
HB="$PROJECT/scripts/.watchdog_heartbeat"
if [ -f "$HB" ]; then
  HB_AGE=$(( ( $(date +%s) - $(stat -c %Y "$HB") ) / 60 ))
  WD_STR="워치독 ${HB_AGE}분전 순찰"
  [ "$HB_AGE" -gt 5 ] && WD_STR="⚠워치독 ${HB_AGE}분 무응답"
else
  WD_STR="⚠워치독 심박 없음"
fi

# 메트릭 수집 최신성
LAST_TS=$(sqlite3 scripts/metrics.db "SELECT MAX(ts) FROM snapshots" 2>/dev/null)
if [ -n "$LAST_TS" ]; then
  COLL_AGE=$(( ( $(date +%s) - $(date -d "$LAST_TS" +%s) ) / 60 ))
  COLL_STR="수집 ${COLL_AGE}분 전"
else
  COLL_STR="수집 기록 없음"
fi

# 오게임 몫 (app+db+scheduler 컨테이너 CPU 합) vs 호스트 전체
OGAME_CPU=$(docker stats --no-stream --format "{{.Name}} {{.CPUPerc}}" 2>/dev/null \
  | awk '/ogamex/ {gsub("%","",$2); s+=$2} END {printf "%.0f", s}')

# 게임 시계 건강도: 최근 30분 함대 도착 예정시각 vs 다음 인증 요청에서 지연 정산된 시각.
# OGameX 함대는 별도 실시간 스케줄러가 아니라 GlobalGame 요청에서 처리되므로, 4분 봇
# 주기에서는 평균 약 120초가 정상이다. 이름도 장애처럼 보이는 "스케줄러" 대신 실제 의미로 표기.
SCHED_DELAY=$(cd ~/Code/ogamex && docker compose exec -T ogamex-db mariadb -uroot -ptoor laravel -N -e \
  "SELECT COALESCE(ROUND(AVG(UNIX_TIMESTAMP(updated_at)-time_arrival),1),'무샘플') FROM fleet_missions
   WHERE processed=1 AND time_arrival > UNIX_TIMESTAMP()-1800;" 2>/dev/null)

# 진행 지표: 최근 1h 건설 취소율 / 마지막 실질 완료(취소 아닌 완료) 경과분
PROGRESS=$(cd ~/Code/ogamex && docker compose exec -T ogamex-db mariadb -uroot -ptoor laravel -N -e "
SELECT CONCAT(
  COALESCE(ROUND(SUM(canceled)/NULLIF(COUNT(*),0)*100,1),'?'), '% · 실질완료 ',
  COALESCE((SELECT ROUND(TIMESTAMPDIFF(SECOND, MAX(updated_at), NOW())/60) FROM building_queues WHERE processed=1 AND canceled=0),'?'), '분전')
FROM building_queues WHERE created_at > NOW() - INTERVAL 1 HOUR;" 2>/dev/null)

# 사전스킵: 봇이 큐잉 전에 스스로 참은 건수(최근 1h, universe.log). 서버 취소율과 분리 측정 —
# 취소율은 "서버가 거절", 사전스킵은 "봇이 안 보냄". 둘을 합쳐야 건설 수요 전체가 보인다.
_CUT=$(date -d '-60 minutes' '+%Y-%m-%d %H:%M:%S')
PRESKIP=$(awk -v cut="$_CUT" '$0 >= cut' "$(dirname "$0")/universe.log" 2>/dev/null \
  | grep -ao "사전스킵" | wc -l)

# AI 폴백률(24h): AI 틱 중 정책 미적용(폴백=태그에 slash 포함, 예 [AI:채굴러/expired]) 비율.
# 정상 시 한 자릿수%(ttl=1 정책이 시간당 루프 직전 잠깐 만료되는 정도). 급등 = AI 루프 정지/정책 미기록 의심.
# codex 루프가 조용히 죽으면 여기서 오르기 시작한다 — 조회 전용(알림 없음).
_CUT24=$(date -d '-24 hours' '+%Y-%m-%d %H:%M:%S')
AIFB=$(awk -v cut="$_CUT24" '
  $0 >= cut && /ai[0-1][0-9]@bots\.local \[AI:/ { t++; if ($0 ~ /\[AI:[^]]*\//) f++ }
  END { if (t) printf "%.1f%%(%d/%d)", f/t*100, f, t; else print "무샘플" }
' "$(dirname "$0")/universe.log" 2>/dev/null)

# 평균점수 변화 (메트릭 스냅샷, 최근 vs 2시간 전)
SCORE_DELTA=$(sqlite3 scripts/metrics.db "
SELECT ROUND(
  (SELECT AVG(score) FROM snapshots WHERE ts = (SELECT MAX(ts) FROM snapshots)) -
  (SELECT AVG(score) FROM snapshots WHERE ts = (SELECT MIN(ts) FROM snapshots WHERE ts > datetime('now','-2 hours'))), 1);" 2>/dev/null)

# 다른 세입자 생존 (tailscale / RustDesk)
TS_OK=$(tailscale status --peers=false > /dev/null 2>&1 && echo O || echo X)
RD_OK=$(docker inspect hbbs --format '{{.State.Running}}' 2>/dev/null | grep -q true && echo O || echo X)

echo "루프 ${ALIVE}/3 생존(유니버스:${UNIV:-죽음} AI:${AILOOP:-죽음} 컬렉터:${COLL:-죽음}) · 바퀴 ${ROUND_SEC:-?}s(${ROUND_AGE}s 전) · 바퀴p90(15) ${ROUND_P90:-?}s/150 · 호스트로드 ${LOAD}/${CORES}코어(${CPU_PCT}%) · 오게임몫 CPU ${OGAME_CPU:-?}%(캡 app250+db100+sched150) · 미션 다음틱 정산지연 ${SCHED_DELAY}s · 취소율(1h) ${PROGRESS} · 사전스킵(1h) ${PRESKIP}건 · AI폴백률(24h) ${AIFB} · 점수Δ(2h) ${SCORE_DELTA:-?} · ${WD_STR} · 세입자 TS:${TS_OK} RustDesk:${RD_OK} · 디스크 ${DISK} · ${COLL_STR}"

if [ "$1" = "-v" ]; then
  echo "--- 오늘 부하 추세/배포 계단 ---"; python3 scripts/load_trend.py
  echo "--- 최근 3바퀴 ---"; grep "바퀴 완료" scripts/universe.log | tail -3
  echo "--- AI 최근 ---"; grep "AI 바퀴 완료" scripts/ai_loop.log | tail -2
  echo "--- 컨테이너 ---"; docker stats --no-stream --format "{{.Name}} {{.CPUPerc}}" | grep -E "app-1|db-1"
fi
