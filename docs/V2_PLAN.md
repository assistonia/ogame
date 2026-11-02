# v2 시뮬레이션 설계

v1(2026-07) 평가(`docs/v1_evaluation/README.md`)에서 "LLM이 규칙봇보다 잘하나?"에 답하지
못한 이유를 하나씩 막는 것이 v2의 목표다.

## v1에서 바꾼 것

| v1 문제 | v2 대응 |
|---|---|
| 실시간 서버(×100)가 과부하라 한 바퀴 150초 목표가 1,000~2,700초, 최대 92명이 차례를 건너뜀 | **가상 시계 턴제**. 모든 봇이 행동한 뒤에야 시계가 넘어간다. 서버가 느려도 결과는 같고, 건너뛰는 차례가 없다 |
| LLM 계정만 탐험가 클래스로 교체 → 클래스 효과가 LLM 효과로 보임 | 모든 계정 같은 클래스(`universe.class`) |
| LLM 계정과 규칙봇이 다른 실행기(policy_engine vs miner_bot) | **대조군(static)과 LLM군이 같은 policy_engine**을 쓰고 초기 정책도 같다. 다른 점은 정책을 LLM이 고쳐 쓰느냐뿐 |
| 사람 계정 하나가 판을 흔듦 | 사람 계정 없음 |
| 레이더가 털 비활성 계정이 없음 | 비활성 farm 계정(광산·창고 레벨 지정) |
| 실험 중 코드·설정 변경 | 설정 파일 하나(`sim/configs/*.json`)로 고정, 실행 중 변경 없음 |
| LLM 프롬프트가 원정을 강하게 유도 → 12명 전부 원정 수렴 | 중립 프롬프트(`sim/strategist.py`), 페르소나는 선호일 뿐이라고 명시 |
| LLM이 실행 실패를 못 봄(열흘간 건물 못 지었는데 계속 "광산 우선") | 최근 엔진 실행 결과와 거절 사유를 프롬프트에 넣음 |
| 게임 서버 수정분이 레포 밖에 있다가 유실 | ogamex를 서브모듈로 고정하고 수정분은 `server/sim-overlay/`에 보관 |

## 구조

```
sim/run_sim.py ── 턴 루프
  1. 봇 쪽 시계를 서버 가상 시계에 맞춤 (bots/shared/simclock.py)
  2. N턴마다 LLM군 정책 갱신 (sim/strategist.py → claude -p)
  3. 전 계정 1턴 행동 (실제 OGameX 서버에 HTTP)
  4. ogamex:sim:advance — 시계를 turn_seconds만큼 넘기며 그 사이 함대 도착을 시간순 처리,
     모든 플레이어의 연구·건설·생산 정산
  5. 점수 기록(runs/<name>/metrics.db), 주기적 DB 체크포인트

server/ (OGameX + 오버레이)
  sim.sh up|down|reset|artisan|dump|restore
  ogamex:sim:init / clock / seed-bots / advance / scores
```

가상 시계는 Laravel의 `Carbon::setTestNow`(ogamex 테스트도 쓰는 방식)와, `time()`을 직접 부르는
네임스페이스에 같은 이름의 함수를 정의하는 방식으로 구현했다. 원본 ogamex 파일은 고치지 않는다
(서비스 프로바이더 등록 한 줄만 추가).

## 실험군

| 그룹 | 실행기 | 정책 | 역할 |
|---|---|---|---|
| classic | v1 규칙봇(miner_bot) | 성격 고정 | 배경 인구, v1 기준선 |
| static | policy_engine | 초기 정책 고정 | **대조군** |
| llm | policy_engine | Claude가 N턴마다 갱신 | **실험군** |
| farm | 없음(비활성) | – | 약탈 대상 |

static과 llm은 같은 성격 구성(miner 2, raider 2, turtle 2, explorer 1, hybrid 1)으로 시작한다.

비교 지표: 최종 총점과 점수 궤적(경제·연구·군사), 그룹 내 성격별 차이, LLM 정책 변경 횟수와 이유.

## LLM 비용

- `claude -p`(헤드리스 Claude Code)로 호출하므로 클라우드 세션 크레딧으로 처리된다(API 키 불필요).
- 호출마다 비용을 `runs/<name>/llm_ledger.json`에 누적하고, `llm.budget_usd`를 넘으면 더 이상
  호출하지 않는다(정책은 마지막 것을 유지).
- 크레딧 한도: US$240(2026-11-05 만료). 개발 작업도 같은 크레딧을 쓰므로 시뮬레이션 LLM 예산은
  본 실험 기준 $60 이하로 잡는다.

## 실행

```bash
./server/sim.sh up                                   # 서버 (처음엔 composer install 포함)
python3 -m sim.run_sim --config sim/configs/smoke.json --fresh   # 1분짜리 동작 확인
python3 -m sim.run_sim --config sim/configs/pilot.json --fresh   # 파일럿
python3 -m sim.run_sim --config sim/configs/pilot.json           # 중단된 곳부터 이어서
```

클라우드 세션 컨테이너는 오래 쉬면 회수된다. `checkpoint_every` 턴마다 DB를
`runs/<name>/checkpoint.sql.gz`로 저장한다.

## 알려진 한계

- 전투 엔진은 Rust 대신 PHP 구현을 쓴다(샌드박스에서 Rust 빌드 불가). 두 구현은 같은 결과를 내도록
  유지되고 있다(ogamex AGENTS.md).
- v1 테스트 중 4개(`tests/test_post_actions.py` 3개, `tests/test_ai_autonomy.py` 1개)는 v1 원본
  코드에서도 실패한다. v2 변경과 무관.
- LLM 직접 명령(지휘관) 모드는 아직 없음. 현재 LLM은 정책(수치·우선순위)만 바꾼다.
