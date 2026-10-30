#!/bin/bash
# 게임 DB 일일 덤프 (크론: 매일 03:30). 7일치 보관 후 오래된 것 삭제.
PROJECT="/home/pyongjoo/Code/80_ogame_simulation"
OUT_DIR="$PROJECT/backups"
mkdir -p "$OUT_DIR"

cd /home/pyongjoo/Code/ogamex || exit 1
docker compose exec -T ogamex-db sh -c 'mariadb-dump -uroot -ptoor laravel | gzip' \
  > "$OUT_DIR/laravel_$(date +%Y%m%d_%H%M).sql.gz" 2>> "$PROJECT/scripts/watchdog.log"

# 7일 초과 백업 삭제
find "$OUT_DIR" -name "laravel_*.sql.gz" -mtime +7 -delete
