# 재부팅 후 시작 가이드 (리눅스, 2026-07-13 기준)

컴퓨터를 껐다 켠 뒤 시뮬레이션을 다시 돌리는 순서.

> **2026-07-11 업데이트**: crontab 워치독(`scripts/watchdog.sh`, 2분마다)이 등록되어
> 루프 3종(유니버스/AI/컬렉터)이 죽으면 자동 재시작한다. 부팅 후에도 cron이 살리므로
> 사실상 아래 2·3절은 자동화됨 (실험 가동 기간 동안 의도된 무인 운영).
> 상태 확인: `./scripts/status.sh` / 재시작·경고 이력: `scripts/watchdog.log` + Discord 알림.

## ⚠ 실험 종료 절차 (72시간 스냅샷 후 — GROWTH_PLAN.md 참고)

실험이 끝나면 **워치독부터 내려야** 봇들이 영구 기동하지 않는다. 순서:

```bash
crontab -l | grep -v watchdog.sh | crontab -   # 1) 워치독 제거 (안 하면 계속 부활함)
pkill -f "run_universe.py"                     # 2) 유니버스 루프 중지 (102계정)
pkill -f run_ai_loop.sh                        # 3) AI 루프 중지
pkill -f metrics_collector.py                  # 4) 컬렉터 중지
cp scripts/metrics.db scripts/metrics_final_$(date +%Y%m%d).db   # 5) 데이터 스냅샷
cd ~/Code/ogamex && docker compose exec ogamex-db sh -c \
  'mariadb-dump -uroot -ptoor laravel | gzip' > ~/Code/80_ogame_simulation/db_final_$(date +%Y%m%d).sql.gz  # 6) DB 덤프
# (선택) docker compose stop  # 서버까지 내리기
```

## 1. 서버 확인 (대부분 자동으로 올라옴)

도커 컨테이너들은 `restart: unless-stopped` 정책이라 부팅하면 보통 알아서 올라온다.

```bash
docker ps   # ogamex-* 컨테이너 7개가 보이면 OK
```

안 올라와 있으면:

```bash
cd ~/Code/ogamex && docker compose up -d
```

- 게임 웹: http://localhost
- DB 관리(phpMyAdmin): http://localhost:8080 (root / toor)

## 2. 봇 루프 켜기 (규칙봇 90 + AI 하이브리드 10 + 실험봇 2 = 102계정)

**가장 쉬운 방법 — 워치독 한 번 실행하면 죽어있는 것 전부 올라온다:**

```bash
cd ~/Code/80_ogame_simulation && ./scripts/watchdog.sh
```

수동으로 하려면 (워치독이 하는 것과 동일):

```bash
cd ~/Code/80_ogame_simulation
EMAILS=$(python3 -c "print(','.join([f'bot{i:03d}@bots.local' for i in range(1,91)] + [f'ai{i:02d}@bots.local' for i in range(1,11)] + ['pirate01@bots.local','explorer01@bots.local']))")
BATCH_ACCOUNTS=$(tr -d '\r\n ' 2>/dev/null < .economy_batch_accounts)
[ -n "$BATCH_ACCOUNTS" ] && BATCH_ENABLED=1 || BATCH_ENABLED=0
OGAME_ECONOMY_BATCH_ENABLED="$BATCH_ENABLED" OGAME_ECONOMY_BATCH_ACCOUNTS="$BATCH_ACCOUNTS" \
  nohup nice -n 10 python3 scripts/run_universe.py --emails "$EMAILS" \
  --loop --interval-min 4 --parallel 6 --spread-sec 240 > /dev/null 2>&1 &
```

- **하이브리드 구조**: AI 계정(aiNN)도 이 루프에서 4분마다 집행되는데, 행동 우선순위는
  `bots/ai_agents/policies/<계정>.json` 정책을 따른다 (정책 없거나 만료 시 miner 폴백)
- **분산 실행**: 102계정을 240초에 균등 분산하고 동시 실행 상한은 6으로 둔다. 한 번에
  몰아서 실행하는 예전 일괄 burst 방식은 사용하지 않는다
- **경제 배치 단계 확대**: `.economy_batch_accounts`를 `ai01@bots.local` → AI 10개 →
  규칙봇 표본 → `*` 순으로 갱신한다. 파일이 없거나 비어 있으면 워치독은 배치를 끈다
- 로그는 스크립트가 자체적으로 `scripts/universe.log`에 쓰므로 stdout은 버린다

## 3. AI 정책 수립 루프 (codex — 1시간마다 정책 JSON 갱신)

```bash
cd ~/Code/80_ogame_simulation
nohup ./scripts/run_ai_loop.sh 3600 2 > /dev/null 2>&1 &   # (워치독도 이 값으로 살림)

# 단발 정책 수립
./scripts/run_ai_agents.sh --agents "ai01 ai03"
```

- AI(codex)는 게임 API를 직접 호출하지 않는다 — **정책 JSON만 작성** (집행은 2절 루프가)
- codex(ChatGPT 구독) 로그인 상태여야 함: `codex login status`로 확인
- 모델: `gpt-5.6-luna` 우선, 한도 시 `gpt-5.3-codex-spark` 자동 폴백 (tick.sh)
  한도 확인: `codex /status` 또는 https://chatgpt.com/codex/settings/usage

## 3.5 메트릭 컬렉터 (실험 데이터 수집)

```bash
cd ~/Code/80_ogame_simulation
nohup python3 scripts/metrics_collector.py > /dev/null 2>&1 &
```

- 5분마다 봇별 지표를 `scripts/metrics.db`(SQLite)에 시계열 적재 + 로그 50MB 로테이션
- 분석: `sqlite3 scripts/metrics.db "SELECT personality, AVG(building_sum) FROM snapshots GROUP BY 1;"`
- 중지: `pkill -f metrics_collector`

## 상태 확인 / 중지

```bash
tail -f scripts/universe.log        # 봇 활동 실시간 로그
pgrep -af run_universe              # 루프 프로세스 확인
pkill -f "scripts/run_universe.py"  # 규칙봇 루프 중지
docker stats --no-stream            # 서버 부하 확인
```

## 참고: 현재 서버 설정

- 배속 **×100** (economy/research/fleet). 변경은 phpMyAdmin 또는:
  ```bash
  cd ~/Code/ogamex && docker compose exec ogamex-db mariadb -uroot -ptoor laravel \
    -e "UPDATE settings SET value='50' WHERE \`key\` IN ('economy_speed','research_speed','fleet_speed','fleet_speed_war','fleet_speed_holding','fleet_speed_peaceful');"
  ```
- 성능 튜닝: OPcache 켬 + PHP 워커 16 (`~/Code/ogamex/docker-compose.override.yml`, `docker/php-fpm-bots.conf`)
  - PHP 소스 수정 시 반영이 최대 60초 지연됨 (opcache revalidate)
- 봇 세션 쿠키: `bots/shared/.sessions/` — 재로그인 비용 절약용. 이상하면 폴더 삭제해도 됨 (자동 재로그인)
- 봇 계정: bot001~090@bots.local (규칙), ai01~10@bots.local (AI),
  pirate01@bots.local·explorer01@bots.local (실험), 비번 전부 botpass
- 서버 소스: `~/Code/ogamex` (이 폴더의 `ogamex` 심볼릭 링크), 봇 시드 커맨드: `php artisan ogamex:dev:seed-bots`
