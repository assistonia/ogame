# 전용 Admin 테스트 계정

## 신원 분리

- 규칙봇: `bot001@bots.local`, 표시명 `bot001`, Admin 역할 없음
- 테스트 계정: `simulation-admin@ogamex.local`, 표시명 `Admin`, Admin 역할 보유
- 테스트 계정은 `@bots.local` 밖이므로 `run_universe.py`, AI 루프, metrics의 성장
  snapshots 대상에 들어가지 않는다.
- 비밀번호는 OGameX의 `storage/app/private/simulation_admin.password`에 mode 0600으로만
  저장한다. 명령행·로그·프로젝트 코드에는 넣지 않는다.

계정 상태를 멱등 정규화하는 명령:

```bash
cd /home/pyongjoo/Code/ogamex
docker compose exec -T ogamex-app php artisan ogamex:dev:ensure-simulation-admin
```

## 테스트 공격

호스트 래퍼는 비밀번호를 읽지 않고 컨테이너 내부 Artisan 명령만 호출한다.

```bash
cd /home/pyongjoo/Code/80_ogame_simulation
scripts/send_admin_test_attack.py \
  --target-email 123123123@naver.com --target-planet 01 \
  --light-fighters 2000 --eta-sec 60 --speed 3 \
  --process-after-arrival
```

`--process-after-arrival`은 OGameX의 지연 정산 구조 때문에 필요하다. 전용 Admin은 자동
봇 루프 밖이므로 ETA가 지나도 요청이 없으면 미션이 처리되지 않는다. 이 옵션은 ETA 뒤
`PlayerService`/`FleetMissionService` 정상 경로로 도착을 정산한다.

## 통계 격리

`metrics.db.battles.is_admin_test=1`인 전투는 다음에서 제외한다.

- AI 경험 성적표의 약탈·공격 손실·방어 손실
- fleet sizing 예측/실전 매칭

Discord 전투 알림에서는 버리지 않고 `[관리자 테스트]`로 표시해 관전 가능하게 유지한다.
분류 기준은 전용 Admin 이메일 또는 원본 battle report의 `general.admin_test=true`다.

2026-07-12 보정 기록:

- 기존 `bot001` 수동 전투 #2283은 원본을 삭제하지 않고 `admin_test=true`로 표시했다.
- 해당 미션 #12232의 연료 5,392 듀테륨을 `bot001`에 한 번만 환급했다.
- 분리 후 전용 Admin 미션 #12447(경전투기 2,000대, ETA 60초)은 전투 #2299로
  정상 처리됐고 metrics에서 `is_admin_test=1`로 확인했다.
- 미션 #12537(4,000대)은 전투 #2306, 미션 #12594(10,000대)는 이동된 01번
  `1:39:5`에서 전투 #2315로 정상 처리됐다. 모두 `is_admin_test=1`이다.
