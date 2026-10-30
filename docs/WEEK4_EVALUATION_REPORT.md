# Week 4 평가 리포트 생성기

`scripts/week4_evaluation_report.py`는 `metrics.db`와 선택적 게임 MariaDB를 읽기
전용으로 조회해 AI와 동일 캐릭터 클래스 규칙봇을 비교하는 한국어 Markdown/JSON
리포트를 만든다. 기존 collector와 metrics writer는 사용하거나 변경하지 않는다.

## 기본 실행

cutoff는 timezone이 있는 ISO-8601로 반드시 지정한다. 기간은 UTC 반개구간
`[start, end)`이다.

```bash
python3 scripts/week4_evaluation_report.py \
  --metrics-db scripts/metrics.db \
  --start 2026-07-12T00:00:00Z \
  --end 2026-07-19T00:00:00Z \
  --ai-cohort 'ai*@bots.local' \
  --rule-cohort 'bot*@bots.local' \
  --output-prefix reports/week4/week4_20260712_20260719
```

생성 파일:

- `reports/week4/week4_20260712_20260719.md`
- `reports/week4/week4_20260712_20260719.json`

동일 입력과 옵션은 같은 정렬·반올림 규칙을 사용한다. JSON에는 선택 데이터와 정책
내용으로 계산한 `analysis_fingerprint`가 들어간다. 수집 DB의 과거 행이 backfill되거나
게임 DB 현재 class가 바뀌면 같은 cutoff라도 fingerprint가 달라질 수 있다.

## 게임 DB를 포함한 실행

게임 DB를 생략하면 다음 값은 `missing` 또는 personality 기반 `estimated`가 된다.

- `users.character_class` 현재값
- `attack_funnel`도 없을 때 성공 espionage report 기반 scout 입력
- expedition message 기반 직접 자원 보상

비밀번호는 CLI에 노출하지 않고 환경변수로만 전달한다. `botread`처럼 DB 권한 자체도
`SELECT`로 제한된 계정을 사용해야 한다. 스크립트는 추가로 `START TRANSACTION READ
ONLY`를 실행하며 모든 애플리케이션 쿼리는 `SELECT`다.

```bash
export OGAME_REPORT_DB_PASSWORD='read-only-password'
python3 scripts/week4_evaluation_report.py \
  --metrics-db scripts/metrics.db \
  --start 2026-07-12T00:00:00Z \
  --end 2026-07-19T00:00:00Z \
  --ai-cohort 'ai*@bots.local' \
  --rule-cohort 'bot*@bots.local' \
  --game-db-host 127.0.0.1 \
  --game-db-port 3306 \
  --game-db-name laravel \
  --game-db-user botread \
  --game-db-password-env OGAME_REPORT_DB_PASSWORD \
  --output-prefix reports/week4/week4_20260712_20260719
```

게임 DB 연결을 지정했는데 연결 또는 `pymysql` import가 실패하면 리포트를 결측으로
조용히 낮추지 않고 실행을 실패시킨다. 연결 후 개별 optional query의 스키마가 맞지
않으면 해당 capability만 `missing`으로 남기고 나머지 리포트를 만든다.

## Cohort와 class 선택

`--ai-cohort`와 `--rule-cohort`는 쉼표 구분 또는 반복 지정할 수 있고 shell glob을
지원한다. glob은 shell이 아닌 스크립트가 해석하도록 따옴표로 감싼다.

```bash
python3 scripts/week4_evaluation_report.py \
  --start 2026-07-12T00:00:00Z \
  --end 2026-07-19T00:00:00Z \
  --ai-cohort 'ai01@bots.local,ai02@bots.local' \
  --rule-cohort 'bot0[0-3][0-9]@bots.local' \
  --class GENERAL,DISCOVERER \
  --scout-window-hours 12 \
  --output-prefix reports/week4/selected
```

기본값은 다음과 같다.

- AI: `ai*@bots.local`
- 규칙봇: `bot*@bots.local`
- class: `ALL`
- scout conversion window: 24시간
- AI personality 정책: `bots/ai_agents/policies/<ai-localpart>.json`

`--class`는 `COLLECTOR`, `GENERAL`, `DISCOVERER`, 숫자 `1,2,3`, 또는 `ALL`을
받는다. AI와 규칙봇 selector가 같은 이메일을 선택하면 비교 오염을 막기 위해 실패한다.

## 지표 정의

### 성장

cutoff 안의 첫 snapshot과 마지막 snapshot 차이를 사용하며 경계 보간은 하지 않는다.

- 총 성장: `score` 차이
- 식민 성장: `planets` 차이
- 경제 성장: `building_sum`, `facility_sum`, 자원 재고 합 차이
- 군사 성장: `ships_sum`, `defense_sum` 차이

함선수와 방어수는 구성과 비용을 모르는 개수 proxy다. 서로 더하거나 실제 군사 가치로
부르지 않는다. 현재 `highscores.economy/military`는 기간 이력이 없으므로 성장치에
사용하지 않는다.

### 실전 전투

`battles`에서 다음 조건을 모두 만족하는 행만 실전 전투로 센다.

- `is_admin_test = 0`
- `is_probe_only = 0`
- cutoff 안의 `battle_ts`, 없으면 수집 `ts`

NULL 분류 flag 행은 실제 전투라고 추정하지 않고 제외한다. 약탈, 공격측 손실,
방어측 손실은 원본 실제값을 합한다. 구성값이 NULL인 행은 0으로 바꾸지 않고 합계를
`partial`로 표시한다. 데브리는 생성량이 아닌 `debris_harvests`의 실제 회수 메시지만
수입으로 센다.

`known_combat_net`은 다음 값이 모두 `measured`일 때만 계산한다.

```text
실제 약탈 + 실제 데브리 회수 - 공격측 손실 - 피격 방어측 손실
```

### Scout-to-preflight-to-launched funnel

`metrics.db`에 다음 `attack_funnel` 스키마가 있으면 이 테이블을 최우선으로 사용한다.

```text
attack_funnel(id, ts, email, target, stage, reason, expected_loot,
              fleet_value, fleet_value_limit, metadata_json)
```

cutoff와 cohort에 속한 stage 행을 대소문자 무시 토큰으로 분류한다.

- scout: stage에 `scout` 또는 `spy`
- preflight: stage에 `preflight`
- launched: stage에 `launch`
- hold: stage에 `hold/held/skip/block/reject/defer`, 또는 launched가 아니며 reason 존재

`scout -> preflight`, `preflight -> launched`, `scout -> launched` 비율은 raw stage
행 수의 분자/분모를 JSON에 함께 둔다. cutoff 경계나 writer stage 의미 때문에 분자가
분모보다 크면 값을 숨기지 않고 `quality_warning`을 남긴다. 하나의 stage 문자열에 여러
토큰이 있으면 여러 단계에 포함될 수 있다. `raw_stage_counts`, `unknown_stage_rows`,
`hold_reason_counts`로 writer 원문을 항상 감사할 수 있다.

`attack_funnel` 테이블이 존재하지만 기간 내 행이 0건이면 0건을 실측으로 유지하며 다른
소스로 대체하지 않는다. 테이블이 없거나 필수 컬럼이 없을 때만 fallback한다.

첫 fallback은 게임 DB 성공 espionage report를 scout로, 분류된 metrics battle을
launched로 시간창 매칭한다. 이 경우 preflight와 hold reason은 `missing`이다.

게임 DB scout도 없으면 `is_probe_only=1` battle을 scout proxy로 사용한다. probe-only
battle은 성공 scout 전체가 아니라 전투가 발생한 probe에 편향되므로 scout 수와
scout-to-launched 비율을 `estimated`로 표시한다. preflight와 hold reason은 여전히
`missing`이다.

### Fallback scout-to-attack 매칭

게임 DB의 `messages.espionage_report_id`와 `espionage_reports`를 조인한 성공 report가
분모다. 실패한 정찰 파견 전체를 나타내지 않는다. target user가 삭제되어 email을
복원할 수 없는 report는 분모에서 별도 제외한다.

동일 공격자와 target email의 실전 공격이 설정된 시간창 안에 있으면, 각 공격을 가장
최근의 아직 사용하지 않은 scout report에 1:1로 매칭한다. 따라서 반복 scout 하나를
여러 전환으로 부풀리지 않는다. 좌표가 metrics battle에 없으므로 target 계정 단위
매칭이며 인과관계를 증명하지 않는다.

### Expedition과 counterfactual

직접 원정 이득은 `messages.key`에 `expedition`이 포함되고 `params`에서 명시적인
`metal/crystal/deuterium` triplet을 안전하게 해석할 수 있는 이벤트만 합한다.

- 직접 자원 보상: `measured` 또는 미해석 이벤트가 있으면 `partial`
- 함선/다크매터 보상, 함대 손실, 연료: 현재 직접 gain에서 제외
- 원정 message 입력 자체가 없으면 `missing`; 0으로 대체하지 않음

리포트는 총점과 직접 원정 자원을 별도 필드로 보존한다. 단위가 다르므로 직접 빼지
않고, 다음 두 추정을 별도로 제공한다.

```text
direct_resource_adjusted_score_delta_estimate
  = total_score_delta - round(direct_expedition_resource_gain / 1000)

same_class_residual_attributed_gain_estimate
  = AI total_score_delta - 동일 class 규칙봇 total_score_delta 중앙값
```

첫 식은 모든 직접 자원이 점수 자산에 투자됐다는 resource-only proxy다. 두 번째 식은
원정 외 정책, 시작 자산, 위치와 운까지 모두 포함한 교란된 잔여치다. 둘 다
`estimated`이며 원정의 인과효과나 정확한 반사실로 인용하면 안 된다. 직접 계측이
없으면 직접조정 값은 `missing`으로 유지된다.

## 상태값

- `measured`: 입력 DB 행을 직접 집계
- `derived`: 입력값의 중앙값, 차이, 비율 등 명시적 산식
- `estimated`: 검증할 수 없는 가정을 포함
- `partial`: 원본 일부가 NULL 또는 미해석이며 값은 known-only 합
- `missing`: 필요한 입력이 없음. 숫자 0과 다름

JSON은 각 metric에 `status`, `value`, `unit`, `method`, 필요 시 `reason`, 분자·분모,
완전/결측 행 수를 함께 둔다. Markdown 숫자에도 상태를 붙여 실측과 추정을 섞지 않는다.

## 재현성과 한계

- cutoff와 cohort를 명시하고 생성된 JSON, 정책 fingerprint, analysis fingerprint를 함께 보관한다.
- class는 게임 DB의 실행 시점 현재값이며 기간별 변경 이력이 아니다.
- 동일 class 비교는 무작위 실험이 아니며 초기 자산·좌표·표본 수를 통제하지 않는다.
- snapshot retention보다 오래된 cutoff는 계정 coverage가 낮거나 계정 자체가 선택되지 않을 수 있다.
- 전투 시각이 없는 legacy 행은 수집 시각 fallback이며 품질 카운터에 기록된다.
- 원정 reward message key/params가 서버 버전에서 달라지면 해당 이벤트는 `partial`로 드러난다.
- JSON을 주 분석 산출물로 사용하고 Markdown은 판독용 요약으로 사용한다.
