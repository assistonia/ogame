# OGame AI 에이전트 프로젝트

로컬 OGameX 서버에서 102명 규모의 봇 유니버스를 돌리는 프로젝트.
- **90명**: 규칙 기반 봇 (성격 파라미터만 다름)
- **10명**: AI 에이전트 (Luna 주모델, Spark 한도 폴백)
- **2명**: Spark 전용 AI 에이전트 (`pirate01`, `explorer01`)

## 구조

```
02_ogame_project/
├── ogamex/            # OGameX 서버 소스 (심볼릭 링크 → ~/ogamex)
│                      #   ※ colima가 ~/Documents를 마운트 못해서 실체는 홈에 있음
├── bots/
│   ├── shared/        # 공용 봇 클라이언트 (로그인, 게임 API 함수)
│   ├── rule_based/    # 규칙 기반 봇 90명
│   └── ai_agents/     # AI 에이전트 12명 (codex exec 정책 수립)
├── scripts/           # 운영 스크립트 (봇 계정 생성, 틱 스케줄러 등)
└── docs/              # 설계 문서
```

## 서버 실행

```bash
cd ~/ogamex && docker compose up -d
# 첫 실행은 컴포저 + Rust 컴파일로 최대 10분 소요
# 웹: http://localhost  /  phpMyAdmin: http://localhost:8080 (root/toor)
```

도커는 colima 사용: `colima start` 먼저 실행.

## 실행 방법

```bash
# 기본 봇 계정 100개 시드 (규칙 90 + AI 10, 최초 1회, 재실행 안전)
./scripts/seed_bots.sh

# 전체 102명 상시 루프 — 4분 주기, 240초 균등 분산, 동시 실행 상한 6
# 워치독이 pirate01/explorer01까지 포함한 정확한 계정 목록과 배치 플래그를 적용한다
./scripts/watchdog.sh

# AI 에이전트 12명 — 정책 1바퀴 (ai 10 Luna 우선 + pirate/explorer Spark 전용)
./scripts/run_ai_agents.sh
./scripts/run_ai_agents.sh --agents "ai01 ai03"   # 부분 실행
```

- 계정: `bot001~090@bots.local`(규칙), `ai01~10@bots.local`(AI),
  `pirate01@bots.local`·`explorer01@bots.local`(Spark 전용 AI), 비번 전부 `botpass`
- 경제 배치 롤아웃은 `.economy_batch_accounts`를 `ai01` → AI 10개 → 규칙봇 표본 → `*`
  순으로 확대한다. 자세한 운영 명령은 `STARTUP.md`가 기준이다
- 규칙봇 성격: `bots/rule_based/personality.py` (채굴러/크리스탈러/밸런스 등, 이메일 기반 결정적 배정)
- AI 페르소나: `bots/ai_agents/personas/` (ai01~10 + pirate01 + explorer01)
- AI 판단 기록: `bots/ai_agents/state/<이름>/memory.md` + 틱 로그

## AI 에이전트 호출 형태

```bash
codex exec -m gpt-5.6-luna \
  -c model_reasoning_effort="low" \
  --skip-git-repo-check \
  "empire_status를 읽고 다음 1~3시간의 정책 JSON을 작성해"

# pirate01/explorer01은 아래 모델을 고정 사용
codex exec -m gpt-5.3-codex-spark ...
```

## 비용 메모 (2026-07 기준)

- GPT-5.6 Luna: $1/$6 per MTok (캐시 입력 90% 할인)
- Luna AI 10명 + Spark AI 2명 × 1시간 정책 틱. ChatGPT 구독 한도를 공유하므로
  `account/rateLimits/read`의 일반/Spark 버킷을 운영 중 확인한다.
