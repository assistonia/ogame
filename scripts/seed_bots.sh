#!/usr/bin/env bash
#
# 봇 계정 100개 생성 (규칙봇 bot001~bot090, AI봇 ai01~ai10 / 비밀번호 botpass).
#
# 실제 생성 로직은 OGameX 소스에 추가한 artisan 커맨드가 담당한다:
#   ogamex/app/Console/Commands/Dev/SeedBotUsers.php  (signature: ogamex:dev:seed-bots)
#   - PlayerServiceFactory / PlanetServiceFactory 로 유저 + 초기 행성(Homeworld) 자동 생성
#   - 이미 존재하는 이메일은 스킵하므로 재실행 안전 (idempotent)
#   - first_login=false 로 설정해 봇이 클래스 선택 화면 대신 바로 /overview 로 진입
#
# 이 스크립트는 컨테이너 안에서 그 커맨드를 실행하는 얇은 래퍼일 뿐이다.
# 옵션은 그대로 전달된다. 예:
#   ./seed_bots.sh                       # 기본값(규칙봇 90 + AI봇 10)
#   ./seed_bots.sh --rule-count=5 --ai-count=2
#   ./seed_bots.sh --password=other --domain=example.local
#
set -euo pipefail

CONTAINER="ogamex-ogamex-app-1"

docker exec "$CONTAINER" php artisan ogamex:dev:seed-bots "$@"
