#!/bin/zsh
# 시뮬레이션 관전용 스냅샷: N초마다 봇 유니버스 집계를 sim_stats.log에 기록
# 사용법: ./sim_snapshot.sh [간격초=120] [횟수=15]
INTERVAL="${1:-120}"
COUNT="${2:-15}"
LOG="$(cd "$(dirname "$0")" && pwd)/sim_stats.log"

for i in $(seq 1 $COUNT); do
  TS=$(date '+%H:%M:%S')
  docker exec ogamex-ogamex-db-1 mariadb -uroot -ptoor laravel -N -e "
    SELECT CONCAT('[$TS] bots=', COUNT(DISTINCT u.id),
      ' 광산레벨합=', SUM(p.metal_mine + p.crystal_mine + p.deuterium_synthesizer + p.solar_plant),
      ' 최고메탈광산=', MAX(p.metal_mine),
      ' 평균메탈=', ROUND(AVG(p.metal)),
      ' 총자원=', ROUND(SUM(p.metal + p.crystal + p.deuterium)))
    FROM users u JOIN planets p ON p.user_id = u.id
    WHERE u.email LIKE '%@bots.local';" 2>/dev/null >> "$LOG"
  sleep $INTERVAL
done
echo "[done] snapshots written to $LOG"
