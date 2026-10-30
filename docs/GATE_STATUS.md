# Part 2 게이트 상태 (2026-07-12 배포)

채점 스냅샷과 그 결과로 배포한 플래그를 함께 기록한다. 편대 사이징 live는 아래 정확도 조건을
충족할 때까지 계속 OFF다.

## 1. 편대 사이징 shadow — 표본 충족, 정확도 게이트 FAIL

- shadow 시작: **2026-07-12 14:46:18 KST** (`scripts/universe.log` 첫
  `[sizing/shadow]`).
- shadow 기회: **326건 / 133.1분 = 146.9건/h**.
- 실제 ATTACK: **106건 = 47.8건/h**.
- `battle_guard` 예측↔실전 exact-match: **82건 resolved + 2건 pending**. 매칭 생산속도는
  첫 resolved부터 약 **38.2건/h**.
- 실행 게이트의 최소 표본은 **N=57** (`scripts/backtest_battle_sim.py --min-reports` 기본값,
  파일 docstring에 사용자 지정값으로 고정). N=57은 **16:14:53 KST에 이미 도달**했다.
- offline 재구성 게이트: 유효 **253/2077건**, 예측손실 7,398,560 / 실제손실 7,449,000,
  합계오차 **0.677% ≤ 15%**, PASS.
- 그러나 shadow 시작 뒤 online exact-match 82건은 예측손실 **0** / 실제손실 **248,000**,
  합계오차 **100% > 15%**, FAIL. 82건 중 손실 발생은 2건(report 2013=232,000,
  report 2062=16,000).

두 실패는 시뮬 엔진 오차가 아니라 시간차 위험이다. 파견 시 fresh spy에는 안전/무방비였지만 실제
도착 시 report 2013의 방어측에 LF584+HF5+SC206 등이, report 2062에는 LF4+SC8 등이 귀환해
있었다. offline backtest는 전투 시점 함대를 복원하므로 통과하지만, online 의사결정은 정찰 시점과
도착 시점 사이의 귀환 함대를 알 수 없다.

티어 로그 분포도 정상적으로 다양화됐다: `pick=tier80` 218건, `tier90` 8건, `tier95` 6건,
순EV custom pick 94건. 82건은 `tier80=none`으로 상한 65% 편대도 승률 80%를 못 넘겼다. 다만
실제로 보낸 편대는 여전히 기존 4대 경로이므로 **방어 비례 sized fleet의 실전 표본은 0건**이다.

판정:

- 공격/정찰 쿨다운 완화는 하지 않는다. 표본은 이미 N을 넘었고, 완화는 stale-fleet 노출과 농장
  압력만 늘린다(`ATTACK_COOLDOWN_SEC=1500`, `SPY_GLOBAL_COOLDOWN_SEC=600` 유지).
- sizing live는 보류한다. 추가로 무방비 전투만 쌓아도 현재 실패 원인은 사라지지 않는다.
- 공격 전 preflight는 LIVE다. 새 정찰 보고서, 공격자 자신의 최근 24시간 합법 정찰에서 본 함종별
  최고치, 계정 전체 함대가치의 15%(절대 하한 50,000)인 동적 위험 한도, ETA 75초를 모두
  통과해야 공격한다. 고정 100k 표본은 `preflight_v1`, 동적 한도 표본은
  `preflight_v2_dynamic_cap`으로 분리한다.
- 다음 sizing live 검토 조건은 이 변경 뒤 신규 exact-match N≥57, online 손실합계 오차≤15%다.
  인증된 즉시 settlement 트리거가 없어 24시간 동안 한 번도 관측되지 않은 신규 복귀 함대 위험은
  남으므로, sizing live는 계속 OFF다.

자동 재채점은 `scripts/sizing_regrade.py`가 담당하며 watchdog 순찰마다 읽기 전용으로 실행된다.
N57 최초 도달과 이후 PASS↔FAIL 변경만 Discord에 dedupe 알림하고 live 플래그는 절대 바꾸지 않는다.
2026-07-12 23:07 KST 동적 한도 v2 cohort는 배포 전 **N=0/57**, 판정
**COLLECTING**이다. 이전 v1 N=3은 더 큰 편대의 새 위험분포와 섞지 않는다.

재현:

```bash
python3 scripts/backtest_battle_sim.py --samples 100 --json
jq -s '[.[].resolved[]]' bots/rule_based/.battle_guard_state/*.json
rg '\[sizing/shadow\]' scripts/universe.log
```

## 2. base-defense — ACTIVE

`scripts/base_defense_gate.py`가 `metrics.db`만 읽어 miner의
`실질자산 = score + (metal+crystal+deuterium)/1000` 코호트 비율을 판정한다.

- idle-fallback 활성: **2026-07-12 16:17:58 KST**.
- baseline→17:00 직전: miner AI/RB **0.7870 → 0.8361 (+4.91%p)**.
- 회귀 기울기 **+0.0703/h**, post **9점 / 41.1분**, AI n=3 / 규칙봇 n=27.
- 현재 판정: **ACTIVE** (`scripts/base_defense_gate.py` exit 0).
- 추세가 확인되지 않았더라도 절대 마감은 **2026-07-13 16:17:58 KST**
  (`READY_DEADLINE`, exit 0).

```bash
python3 scripts/base_defense_gate.py
python3 scripts/base_defense_gate.py --json
```

스크립트는 절대 플립하지 않는다. 채점 후 `MINER_BASE_DEFENSE_ENABLED=True`로 배포했다.

1. `bots/rule_based/miner_defense.py`의 현재 플래그가 `True`인지 확인.
2. 검증:

   ```bash
   python3 -m py_compile bots/rule_based/miner_defense.py
   pytest -q tests/test_miner_defense.py tests/test_base_defense_gate.py
   ```

3. 루프만 재시작하고 확인:

   ```bash
   pkill -f '[r]un_universe.py'
   ./scripts/watchdog.sh
   ./scripts/status.sh
   rg -m 5 'basedef (BASE-DEF|ADAPT-DEF)' scripts/universe.log
   ```

위험 범위: 유니버스 루프가 잠깐 비고, miner/explorer/hybrid가 경제 행동 뒤 남은 잉여로 방어를
건조하기 시작한다. 하드캡은 589,625 metal-equivalent(터틀 중앙값의 50%)이며 DB 마이그레이션은 없다.

## 3. 터틀 반사 — ACTIVE, sizing live에는 필수

`TURTLE_REFLEX_ENABLED=True`로 배포했다. 첫 관측은 현재 방어를 baseline peak로만 저장해 기존
방어를 손실로 오인하지 않는다. 이후 방어가 peak 아래로 떨어질 때만
post-actions의 경제 행동 뒤 남은 실제 자원까지 재건 예산을 올리고, 회복 또는 6시간 상한에서
자동 해제한다. 상태/DB 실패는 반사 없음으로 닫힌다.

따라서 의존성은 단방향이다.

- 터틀 반사 ON은 sizing live 없이도 안전하고, 기존 공격에 대한 baseline/보호를 미리 만든다.
- sizing live는 기존 4대 안전가정을 깨므로 터틀 반사 OFF 상태에서 열면 안 된다.
- 두 플래그를 자동 결합하는 코드 가드는 아직 없다. sizing live 배포 체크리스트가 이를 강제해야 한다.

현재 상태 확인:

```bash
pytest -q tests/test_turtle_reflex.py
pkill -f '[r]un_universe.py'
./scripts/watchdog.sh
rg -m 5 'turtle REFLEX-DEF' scripts/universe.log
```

전체 회귀 suite는 현재 **312 passed**다:

```bash
pytest -q tests/test_turtle_reflex.py tests/test_miner_defense.py \
  tests/test_base_defense_gate.py tests/test_battle_guard.py \
  tests/test_battle_sim.py tests/test_raider_intel.py
```

## 4. AI 성적표·정본 성격 — ACTIVE

`bots/ai_agents/.experience_scorecard_enabled`와 `.tech_tree_deployed`가 모두 켜져 있다.
매 정책 갱신 전에 직전 정책 구간을 동일 성격·비슷한 시작 자산의 규칙봇과 비교하고, 군사 수입·손실은
실제 battle/debris 행만 사용한다. 등록된 aiNN의 성격은 canonical persona로 고정한다. 첫 라이브
사이클에서 ai10의 모델 제안 `hybrid`를 `raider`로 교정했고 다음 유니버스 바퀴에서
`[AI:레이더]`와 리사이클러 연구·건조 경로를 확인했다.

## 5. Discord 전투 알림 — ACTIVE

메트릭 컬렉터가 5분마다 신규 battle ID를 최대 8건씩 묶어 OGame 전용 webhook으로 보낸다. 공격측
`units`가 정찰기 한 종류뿐인 보고서는 Discord에 보내지 않고 cursor만 소비한다. 첫 활성은 현재 ID를
baseline으로만 저장하고, 실제 메시지는 POST 성공 뒤에만 0600 cursor를 전진한다. 라이브 backfill에서
`#2119~#2131` 중 실제 약탈전 `#2123·#2127`과 probe-only를 정확히 분리했고, 신규 probe-only
`#2138·#2139`는 무전송 상태로 cursor가 전진하는 것을 확인했다.

## 6. 계정 전체 함선 상한 — 배포 검증 완료

공용 `ship_targets`와 AI `ship_priority.max_total`은 현재 행성만 보지 않고 모든 소유 행성·비행 함대·
미완료 조선소 큐를 단일 DB 스냅샷으로 합산한다. 큐 완료분은 이미 행성에 지급되므로 잔여분만 더한다.
조회 실패 시 건설·연구는 유지하고 상한 관련 함선 생산만 fail-closed한다. ai10 실측은 SC121/LF209/
HF5/probe6/위성87이며, 과잉 LF·SC·위성 후보가 제거되는 것을 확인했다.

## 7. 배포 후 부하 — STABLE

- sizing shadow(14:46) 뒤 23바퀴: p50 77.7s / p90 88.4s / max 95.9s.
- Part 2 직전 기준선(16:18) 18바퀴: p50 83.4s / p90 93.1s / max 100.2s.
- Part 2 묶음 직후(17:28) 5바퀴: p50 96.3s / p90 111.3s / max 114.2s.
- 계정 전체 상한 안정화(17:48) 뒤 최신 구간: 이벤트별 p90 93.6~97.6s.
- preflight cohort/regrade 재시작(18:32) 뒤 6바퀴: p50 85.8s / p90 89.5s / max 91.6s.
- 최신 rolling 15바퀴: p50 87.0s / p90 93.9s / max 105.5s.

4분은 `sleep=max(0, 240-elapsed)`인 start-to-start 목표이므로 최신 p90에도 약 146초 여유가 있다.
배포 후 HTTP POST는 연구·방어·시설 큐 때문에 35.1% 늘었지만 GET은 거의 동일하다. status의
148→198→230%는 라운드와 10분 highscore 작업이 겹친 순간의 1분 loadavg다. 단일 시점 설명과
별개로 추세를 검증하기 위해 배포 계단 절편을 제거했으며, 현재 구간 내 공통기울기는 **-4.1s/h**,
17:48 이후 기울기는 **-2.7s/h**다. 따라서 바퀴시간의 지속 우상향 증거는 현재 없다.

운영 임계는 **최근 15개 완주 바퀴 p90 ≥150초**로 고정했다. `status.sh`가 항상
`바퀴p90(15) .../150`을 표시하고 watchdog은 임계 진입·회복 전이만 Discord로 알린다. 단일 바퀴
`>300초` severe 경보도 유지한다. 두 경보 모두 재시작이나 interval 변경을 자동 집행하지 않는다.
현재 루프는 이미 4분이므로 경보 의미는 "4분 복귀"가 아니라 **4분 유지/용량 검토**다.

sizing LIVE의 tier 스캔은 shadow에서 이미 바퀴당 평균 약 4~5회 수행 중이다. 마이크로벤치는
무방비 0.001s/call, RL100 0.299s, 혼합방어 0.509s, 과거 귀환함대 규모 1.287s/call이었다.
선택 결과를 재사용하면 tier CPU 증분은 약 0이고 공격/수거 I/O까지 총 **+3~10s/round**로 본다.
LIVE에서 다시 계산하면 보통 tier CPU +1.3~5.7s(heavy burst 약 10s), 총 **+5~20s/round**다.
현재 `_sizing_shadow_log`는 문자열만 반환하므로 N57 PASS 뒤 플립 전에 구조화된 선택 편대·판정을
반환해 같은 결과를 집행하는 리팩터가 필수다. 이 조건 없이는 LIVE 플립하지 않는다.

재현:

```bash
python3 scripts/load_trend.py
python3 scripts/load_trend.py --json
python3 scripts/load_trend.py --check --window 15 --threshold 150
```
