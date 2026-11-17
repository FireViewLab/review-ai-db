# Data ↔ AI v2 SSE 연동 계약 (로컬 검증, 미배포)

신규 경로는 `POST /api/v2/data/analyze/stream` 하나입니다. `ENABLE_DATA_AI_V2=1`일 때만 등록합니다.
기존 `POST /api/v1/data/analyze`와 `POST /api/v1/data/analyze/stream`, v0.5 응답/RTI, 인증 및 replay는 유지합니다.
Data는 기존 트래픽을 v1에 둔 상태에서 테스트 환경에서만 v2를 연결하세요.

## 요청

| 항목 | 타입 | 조건 |
| --- | --- | --- |
| `X-Internal-Token` | HTTP 헤더 | 서버 간 내부 인증. 운영에서는 `REQUIRE_INTERNAL_TOKEN=1` 필수 |
| `X-Request-ID` | HTTP 헤더 | 요청별 추적용, 비어 있거나 제어문자 불가 |
| `Idempotency-Key` | HTTP 헤더 | 논리 분석 1건에 하나. 동일 작업 재연결 시 유지 |
| `platform`, `product_id` | 문자열 | 필수. 원본 그대로 보존 |
| `reviews` | 배열 | 필수, 같은 상품의 1~500건. ID 중복 불가, 입력 순서 유지 |
| `review_id`, `content` | 문자열 | 필수, 공백만 입력 불가. 숫자로 변환하거나 trim하지 않음 |
| `rating` | 숫자 또는 `null` | 선택, 0~5. 검증된 5점 척도 값만 보내고 결측·추정값은 `null`/누락 |
| `image_count` | 정수 또는 `null` | 선택, 0 이상. 0은 확인된 0장, 1 이상은 확인된 개수, `null`/누락은 미확인 |
| `written_at` | 문자열 또는 `null` | 선택. 원문 저장은 하지만 날짜 단독으로 Behavior 근거를 만들지 않음 |

알 수 없는 필드, 숫자 문자열/불리언 등 잘못된 타입, 범위 초과 및 빈 배치는 422입니다.
별점이나 사진의 검증 여부를 나타내는 별도 wire 필드는 없습니다. **Data가 확인한 값만 non-null로 보낸다**는 전제입니다.
사진 URL 미수집, 크롤링 실패 또는 원천 필드가 없는 경우 `0`으로 대체하지 마세요. 원천에서 이를 구분할 수 있는지와 별점 검증 기준은 Data 팀 확인이 필요합니다.

```json
{"platform":"kurly","product_id":"1000146248","reviews":[{"review_id":"135039521","content":"좋아요!","rating":5,"image_count":0,"written_at":"2026-11-17T10:00:00"}]}
```

## SSE 이벤트

`Content-Type: text/event-stream`. 응답 헤더에 `X-Request-ID`, `X-Analysis-Job-ID`가 포함됩니다.
아래는 합성 리뷰를 기준으로 한 형식 예시입니다. 수치·작업 ID는 운영 결과를 의미하지 않습니다.

```text
event: meta
data: {"request_id":"data-1","ai_job_id":"job-1","platform":"kurly","product_id":"1000146248","review_count":1,"contract_version":"v2","model_version":"ptext-koelectra-v3.1-b-seed20261115","policy_version":"evidence-v3-balanced-interior-v1"}

event: progress
data: {"request_id":"data-1","stage":"analyzing","processed":0,"total":1}

event: heartbeat
data: {"request_id":"data-1"}

event: result
data: {"request_id":"data-1","review_id":"135039521","policy_index_v3":15.8,"evidence_level_v3":"DANGER","text_score":54.0,"behavior_score":-1.0,"network_score":-1.0,"analysis_summary_ko":"첨부 사진이 0장이고 별점이 5점으로 검증됐으며 의미 있는 본문이 10자 미만이라는 세 조건이 함께 나타나 정책상 주의 근거로 반영됐습니다. 짧은 본문에는 길이 감점도 크게 반영됐지만, 이 조합만으로 리뷰가 허위라고 판단할 수는 없습니다.","reason_source":"python","rules_reasons_ko":["의미 있는 본문이 10자 미만이라 길이 감점이 크게 반영됐습니다.","전달된 데이터에 첨부 사진이 없습니다.","검증된 별점은 5점입니다.","구체적인 사용 경험을 서술한 표현을 찾지 못했습니다.","정책이 찾는 홍보성 표현 조합은 감지되지 않았습니다.","확인된 사진은 없고, 별점 5점에 본문이 10자 미만입니다."],"evidence_reasons_v3":["MEANINGFUL_TEXT_0_9_STRONG_PENALTY","IMAGE_ABSENT_VERIFIED","RATING_FIVE_VERIFIED","EXPERIENCE_EVIDENCE_NO","PROMOTIONAL_STYLE_NOT_DETECTED","VERIFIED_NO_PHOTO_ULTRASHORT_FIVE_STAR"]}

event: done
data: {"request_id":"data-1","ai_job_id":"job-1","result_count":1}
```

`progress`는 분석 시작만 뜻합니다. 대기 중 heartbeat는 약 15초 간격이고, 빠른 작업에는 나오지 않을 수 있습니다.
DB 커밋 후 요청 순서대로 `result`가 나옵니다. `done`과 결과 건수를 확인해야 성공입니다.
연결이 끊겨도 AI 작업은 진행됩니다. DONE replay는 저장한 결과로 `meta → result* → done`을 반환하며 분석·Groq 재호출은 없습니다.

## 결과와 오류

- `policy_index_v3`: 정책 지수. SAFE ≥70, WARN ≥40 및 <70, DANGER <40. 허위 리뷰 확률이나 기존 RTI가 아닙니다.
- `text_score`: v3.1 B. Behavior/Network는 기존 분석기 의미 유지; v2 입력에는 안정적인 작성자 ID와 이력이 없어 날짜만으로 Behavior를 산출하지 않습니다.
- 세부 점수 `-1`은 unavailable입니다. 모든 분석 신호가 `-1`이면 `policy_index_v3=-1`, `evidence_level_v3=null`, `evidence_reasons_v3=[]`입니다. 모델 초기화·실행 예외는 숫자 대신 아래 SSE 오류로 구분합니다.
- `rules_reasons_ko`와 `evidence_reasons_v3`는 계산된 목록 전체입니다. 화면은 `analysis_summary_ko`가 있으면 한 문단, 없으면 `rules_reasons_ko`를 표시합니다. `reason_source`는 `groq`, `python`, `rules`입니다. Groq 실패는 작업 실패가 아닙니다.
- 토큰 불일치/누락은 v1과 같은 HTTP 401입니다. `REQUIRE_INTERNAL_TOKEN=1`인데 서버 토큰이 없으면 앱 시작이 실패합니다. 요청 검증 422, 스토리지 불가 503입니다.
- HTTP 409 `detail.code`: `IDEMPOTENCY_IN_PROGRESS`는 같은 키로 기다렸다 재요청, `IDEMPOTENCY_KEY_REUSED`는 페이로드가 다름, `IDEMPOTENCY_FAILED`는 저장된 실패 상태입니다. 실패 원인 조치 후 **새 논리 작업**만 새 키를 사용합니다.
- 스트림이 열린 뒤에는 아래 오류로 끝나며 `result`/`done`을 보내지 않습니다. 코드: `V2_MODEL_UNAVAILABLE`, `V2_INFERENCE_FAILED`, `ANALYSIS_FAILED`. 내부 예외나 키는 보내지 않습니다.

```text
event: error
data: {"request_id":"data-1","ai_job_id":"job-1","code":"V2_MODEL_UNAVAILABLE","message":"분석 결과를 제공하지 못했습니다. 작업 상태 확인이 필요합니다.","retryable":false}
```

v1/v2는 같은 멱등 키라도 독립 작업입니다. Data DB에서도 `(계약 버전, 작업/리뷰 식별자)`를 분리하고 `contract_version`, `model_version`, `policy_version`을 함께 보존하세요. v2 결과로 기존 RTI를 덮지 않습니다. 운영 전환은 양측 스테이징의 인증·처리량·SSE smoke test와 결과 보존 승인 이후로 남겨 둡니다.
