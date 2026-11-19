# 정빈님 인수인계 — Data 서버 v2 연동 (PR #36)

연동 대상은 `POST /api/v2/data/analyze/stream`입니다. v2 요청·응답 및 정책은 PR #36 계약 그대로이며, 변경점은 **AI MySQL 대신 로컬 파일에 재전송용 결과를 보관하고, Data 서버가 최종 적재를 담당**한다는 점입니다. 스테이징 주소·토큰은 별도로 합의하세요. v1/v0.5는 그대로 유지합니다.

## 요청 입력

필수 HTTP 헤더: `Content-Type: application/json`, `X-Internal-Token`(서버 간 인증), `X-Request-ID`(연결별 추적 ID), `Idempotency-Key`(논리 작업별 고유 키). 재연결 시 추적 ID는 바꿔도 되지만, **멱등 키와 검증된 페이로드는 그대로** 보냅니다.

| 필드 | 타입 및 규칙 |
| --- | --- |
| `platform`, `product_id` | 비어 있지 않은 문자열. 숫자 변환이나 trim 없이 원본 식별자 사용 |
| `reviews` | 동일 상품 1~500건. 같은 요청에서 리뷰 ID 중복 금지, 전송 순서 유지 |
| `reviews[].review_id`, `content` | 비어 있지 않은 문자열. 숫자 문자열 ID의 선행 0과 원문의 공백을 그대로 보존 |
| `reviews[].rating` | 선택, 확인된 5점 척도의 JSON 숫자 0~5. 미확인·추정값·다른 척도는 `null`/누락 |
| `reviews[].image_count` | 선택, 확인된 정수 0 이상. `0`은 확인된 0장, `null`/누락은 미수집·실패·미확인. 둘을 구분하지 못하면 `null` |
| `reviews[].written_at` | 선택, 출처의 문자열 또는 `null`. 날짜 단독으로 행동 점수 근거를 만들지 않음 |

확인 여부를 나타내는 별도 필드는 없습니다. 미정의 필드와 잘못된 타입은 422입니다. 플랫폼별 별점·사진의 출처와 검증 기준은 Data에서 확인합니다.

```json
{"platform":"kurly","product_id":"1000146248","reviews":[{"review_id":"135039521","content":"좋아요!","rating":5,"image_count":0,"written_at":"2026-11-17T10:00:00"}]}
```

## SSE 수신

응답은 `text/event-stream`이며 `X-Request-ID`, `X-Analysis-Job-ID`를 돌려줍니다. `event: <name>`, `data: <JSON>`, 빈 줄 단위로 파싱합니다. 모든 이벤트의 `request_id`는 현재 연결의 추적 ID입니다.

| 이벤트 | JSON 키 (공통 `request_id` 외) | Data 처리 |
| --- | --- | --- |
| `meta` | `ai_job_id`, `platform`, `product_id`, `review_count`, `contract_version`, `model_version`, `policy_version` | 버전과 상품·예상 건수 확인. 모델 `ptext-koelectra-v3.1-b-seed20261115`, 정책 `evidence-v3-balanced-interior-v1`, 계약 `v2` |
| `progress` | `stage: "analyzing"`, `processed: 0`, `total` | 작업 시작 신호. 퍼센트·완료를 의미하지 않음 |
| `heartbeat` | 추가 키 없음 | 분석 중 약 15초마다 올 수 있음. 빠른 요청에는 없을 수 있음 |
| `result` | `review_id`, `policy_index_v3`, `evidence_level_v3`, `text_score`, `behavior_score`, `network_score`, `analysis_summary_ko`, `rules_reasons_ko`, `evidence_reasons_v3`, `reason_source` | 요청 순서의 확정 결과만 수집. AI 로컬 파일에 완료 기록 후 전송 |
| `done` | `ai_job_id`, `result_count` | 건수·ID·순서·작업 ID까지 대조한 후 적재/성공 처리 |
| `error` | `ai_job_id`, `code`, `message`, `retryable: false` | 실패. `done` 없음. 미완료 결과를 최종 저장하지 않음 |

점수의 `-1`은 계산 불가(0점 아님)입니다. 모든 신호가 미가용이면 `policy_index_v3=-1`, `evidence_level_v3=null`입니다. 그 외 등급은 대문자 `SAFE`(≥70), `WARN`(≥40, <70), `DANGER`(<40)입니다. 정책 지수는 기존 RTI나 허위 리뷰 확률이 아닙니다. 화면에는 `analysis_summary_ko`가 있으면 한 문단을, 없으면 `rules_reasons_ko` 배열을 표시합니다. `reason_source`는 `groq`, `python`, `rules` 중 하나입니다. 필드 및 패킷 예시는 [v2 SSE 상세 명세](data-ai-v2-integration.md)를 참고하세요.

## 오류·재시도·적재

- **401/422:** 토큰 또는 요청을 바로 수정하세요. 새 요청 내용으로 바꿔야 한다면 기존 멱등 키를 재사용하지 않습니다.
- **409 `detail.code=IDEMPOTENCY_IN_PROGRESS`:** 지수 백오프와 횟수 제한을 두고 같은 키·같은 입력으로 다시 요청합니다. 중복 분석이 시작되지 않습니다.
- **409 `IDEMPOTENCY_KEY_REUSED`:** 같은 키가 다른 입력에 묶여 있습니다. 같은 키로 반복하지 않습니다. **409 `IDEMPOTENCY_FAILED`:** 실패 상태 또는 버전이 달라져 재개할 수 없는 중단 작업입니다. 원인을 조치하고 실제로 새 작업으로 승인된 경우에만 새 키를 발급하세요.
- **503/연결 단절/`done` 없이 종료:** 적재하지 않고, 제한된 백오프로 같은 키·같은 입력을 재시도합니다. DONE은 같은 `ai_job_id`와 동일 결과를 `meta → result* → done`으로 재전송합니다. 프로세스 강제 종료로 RUNNING이 남은 경우, 같은 키·입력·버전의 재요청이 같은 작업 ID로 다시 계산할 수 있습니다. 자동 큐 복구는 없습니다.
- **SSE `error`:** `V2_MODEL_UNAVAILABLE`, `V2_INFERENCE_FAILED`, `ANALYSIS_FAILED`입니다. 최종 실패로 표시하고 원인을 확인합니다. 제공되는 `retryable: false`를 무시해 자동 재실행하지 않습니다.
- **Data DB:** `done` 검증 후 트랜잭션으로 적재하고, 예를 들어 `(contract_version, platform, product_id, ai_job_id, review_id)`의 유일 키/업서트로 재전송 중복을 막으세요. `model_version`과 `policy_version`도 함께 저장합니다. v2 값으로 v1 `rti`/`level`을 덮어쓰지 않습니다. AI 파일 캐시는 Data DB의 적재 확인이나 영구 보관을 대신하지 않습니다.

스테이징에서 사진 0/null/있음, 별점 null, 500건 경계, 연결 단절 후 재전송, v1/v2 결과 분리, Data의 트랜잭션/중복 방지를 확인한 뒤 운영 전환을 승인합니다. 이번 작업에서 Data 서버 저장소는 변경하지 않습니다.
