<!-- Data가 POST한 리뷰를 AI가 분석하고 SSE로 응답하는 공식 연동 계약. -->
# Data → AI POST 분석 스트림

`POST /api/v1/data/analyze/stream`은 기존 JSON API
`POST /api/v1/data/analyze`의 **추가 선택지**다. 기존 JSON API의 요청·응답·계산은 바뀌지 않는다.
AI가 외부 Data/크롤러 SSE를 구독하는 `/experimental/analysis/collect/stream`과는 반대 방향이며 별도 구현이다.
신규 경로는 실험 플래그와 무관하게 Swagger에 표시된다.

## 요청

| 헤더 | 의미 |
| --- | --- |
| `X-Internal-Token` | 기존 JSON API와 동일한 내부 인증. 운영에서 필수 설정 |
| `X-Request-ID` | 필수, 공백만으로 구성할 수 없음. 각 호출 추적 ID이며 이벤트와 응답 헤더에 그대로 반환 |
| `Idempotency-Key` | 필수, 공백만으로 구성할 수 없음. 영속 중복 방지 키. 재시도 시 동일 키 사용 |
| `Content-Type` | `application/json` |
| `Accept` | `text/event-stream` 권장 |

HTTP 제어 문자는 요청 ID/멱등 키에서 거절한다. 키는 대소문자를 구분하며 앞뒤 공백을 자동 제거하지 않는다.
키를 상품마다 재사용하지 말고 논리적 분석 작업마다 새 값을 생성한다. 토큰·비밀값을 요청 ID나 키에 넣지 않는다.

```json
{
  "platform": "kurly",
  "product_id": "1000146248",
  "reviews": [
    {
      "review_id": "135039521",
      "content": "리뷰 내용",
      "rating": 5,
      "written_at": "2026-10-07T10:00:00"
    }
  ]
}
```

기존 `DataAnalyzeRequestV05` 그대로다. body에 `request_id`를 넣지 않는다.
동일 상품 리뷰 **1~500건**, 중복 `review_id`·추가 필드·501건 이상은 `422`다.
500건을 초과한 요청을 서버가 자동 분할하지 않는다. 분할하면 network 점수의 비교 집합이 달라지기 때문이다.

## 식별자 및 DB idempotency

- Data의 `X-Request-ID`는 호출 추적용으로 매 재시도 때 달라도 된다.
- `Idempotency-Key`는 동일 논리 작업 재시도 동안 유지한다. 요청 body만 중복 판정 대상이며 요청 ID는 해시에 포함하지 않는다.
- AI는 별도 UUID `job_id`를 생성한다. 응답 `X-Analysis-Job-ID`와 이벤트 `ai_job_id`로 반환한다.
- `ai_analysis_idempotency` 테이블에 키 SHA-256, 검증된 body의 canonical JSON SHA-256, job ID, 시작 시점 버전 JSON을 보관한다.
  객체 키 순서는 무시하고 리뷰 배열 순서는 보존한다. Pydantic 검증 후 기본값/숫자 표현을 사용하므로 생략한 optional 필드와 명시적 null은 동일하다.
- 원래 `ai_analysis_jobs`의 `RUNNING/DONE/FAILED`와 `result_json`이 상태·결과의 기준이다. DB schema 변경 없이 기존 결과 저장을 유지하고 별도 테이블만 `CREATE TABLE IF NOT EXISTS`로 추가한다.
- MySQL은 고유 키+단일 InnoDB 트랜잭션, SQLite 테스트 저장소는 `BEGIN IMMEDIATE`+고유 키로 동시 등록을 막는다. 패배한 등록의 job insert도 롤백된다.
- 메모리 task set은 연결 종료 후 분석의 수명 관리 전용이다. 중복 판정은 메모리가 아닌 DB에서 이루어진다.

| 동일 키 재요청 | 응답 |
| --- | --- |
| 처음 보는 키 | 새 job 하나를 생성하고 분석 |
| 동일 payload, DONE | 저장된 최종 결과로 `meta → result들 → done` replay. 분석기/Groq 호출 없음 |
| 동일 payload, RUNNING | HTTP 409, `detail.code=IDEMPOTENCY_IN_PROGRESS` |
| 다른 payload | HTTP 409, `detail.code=IDEMPOTENCY_KEY_REUSED` |
| 동일 payload, FAILED | HTTP 409, `detail.code=IDEMPOTENCY_FAILED`; 자동 재분석 없음 |
| DB 접근 불가/저장 결과 손상 | 스트림 전 HTTP 503; 손상 데이터를 재계산하지 않음 |

RUNNING 충돌은 간격을 두고 **같은 키/같은 body**로 다시 요청한다. 스트림이 중간에 끊겼어도 새 키를 만들지 않는다.
DONE replay는 처음부터 모든 result를 전송하므로 Data는 같은 AI job/review ID를 중복 삽입하지 않도록 upsert해야 한다.
`Last-Event-ID` 기반 부분 replay는 제공하지 않는다.

FAILED 또는 강제 프로세스 종료로 남은 RUNNING은 자동 회수하지 않는다. 운영자가 job 상태/실행 여부를 확인한 뒤 복구 여부를 결정해야 한다.
실패한 키를 삭제하거나 만료시키는 자동 정책, TTL, 자동 재시작/작업 큐는 이번 범위에 없다.

## 이벤트

`Content-Type: text/event-stream`, UTF-8 JSON이며 각 이벤트는 빈 줄로 끝난다.
`Cache-Control: no-cache`, `X-Accel-Buffering: no`를 반환한다. 프록시에서도 buffering과 timeout 설정을 확인해야 한다.
아래 값은 **형식 예시**이며 위 예시 리뷰의 실제 분석 결과를 의미하지 않는다.

```text
event: meta
data: {"request_id":"data-job-123","ai_job_id":"00000000-0000-4000-8000-000000000001","platform":"kurly","product_id":"1000146248","review_count":1,"contract_version":"v0.5","model_version":"ptext-koelectra-v1-2epoch-20260929","policy_version":"rti-v0"}

event: progress
data: {"request_id":"data-job-123","stage":"analyzing","processed":0,"total":1}

event: heartbeat
data: {"request_id":"data-job-123"}

event: result
data: {"request_id":"data-job-123","review_id":"135039521","rti":88.3,"level":"safe","text_score":90.0,"behavior_score":-1.0,"network_score":84.0,"reasons":["리뷰 내용이 매우 짧습니다."]}

event: done
data: {"request_id":"data-job-123","ai_job_id":"00000000-0000-4000-8000-000000000001","result_count":1}

```

- `meta`: DB에 작업 접수 후, 계산 완료를 기다리지 않고 전달. replay는 기존 job의 저장된 버전을 사용한다.
- `progress/analyzing`: 전체 리뷰 집합의 분석을 시작/대기 중이라는 뜻. `processed=0`은 리뷰별 확정 결과를 아직 내보내지 않았다는 의미이며 추론 퍼센트가 아니다.
- `heartbeat`: threadpool 분석·Groq·저장/결과 읽기를 기다리는 동안 약 15초마다 전송. 짧은 분석/replay에는 없을 수 있다. 진행률이 아니다.
- `result`: **DB 저장 커밋 후** 최종 v0.5 리뷰 결과를 요청 순서대로 하나씩 전송. 추가 필드는 `request_id`뿐이다.
  중간/임시 점수는 없다. 결과를 모두 읽고 검증한 뒤 전송한다.
- `done`: 전송한 result 이벤트 개수와 `result_count`가 일치한다. Data는 **done까지 받아야** 성공 완료로 판단한다.
  서버의 이벤트 생성/전송은 클라이언트의 DB 저장 ACK를 의미하지 않는다.

스트림 시작 후 분석/저장/결과 읽기 실패는 다음 이벤트로 종료한다. 정상 result/done은 내보내지 않는다.
내부 예외 원문·DB 인증정보는 공개하지 않는다.

```text
event: error
data: {"request_id":"data-job-123","ai_job_id":"00000000-0000-4000-8000-000000000001","code":"ANALYSIS_FAILED","message":"분석 결과를 제공하지 못했습니다. 작업 상태 확인이 필요합니다.","retryable":false}

```

`retryable=false`는 무조건 새 작업을 자동 실행하지 말고 상태를 확인하라는 보수적 지침이다.
결과 저장은 끝났으나 재조회만 일시적으로 실패했다면 동일 키 재요청은 DONE replay가 가능하다.
인증 오류는 기존 HTTP 401, body/header 검증은 422이며 스트림을 시작하지 않는다.

## 계산/저장/연결 종료 정책

기존 `evaluate_data_and_store()` → `analyze_reviews()` → v0.5 serializer/Groq 후처리 → MySQL commit을 그대로 사용한다.
그 후 저장된 최종 JSON을 다시 읽어 전송한다. DB 저장 실패 시 성공 result/done을 보내지 않는다.
Groq 장애 시 기존 reason code fallback, `reasons: list[str]`, 빈 reasons 그대로 유지한다.

버전은 `app/core/analysis_versions.py`에서 중앙 관리한다. 버전 변경 시 해당 모델/정책과 일치하는지 릴리스 검토가 필요하다.
기존 정책은 text **0.50**, behavior **0.30**, network **0.20**, 가용 신호 재정규화,
safe **>=70**, warn **>=40**, danger **<40** 그대로다.
`-1`은 unavailable이지 0점이 아니다. wire에서는 -1을 유지하며 Data 저장 시 null로 변환하는 것은 Data 책임이다.
세 신호 모두 -1일 때만 `rti=-1, level=null`. 실제 0점은 0 그대로다.

클라이언트 연결이 끊겨도 이미 접수된 분석은 저장까지 계속하며 재연결 시 같은 키로 replay할 수 있다.
정상 프로세스 shutdown에서는 진행 중 task를 기다린다. 강제 종료/OOM/VM 종료까지 보장하는 durable queue는 아니다.
**DB commit 직후 연결이 끊겨도 DONE 결과를 재사용한다.**

## 호출 예시 (POSIX shell)

실제 토큰은 로컬 환경변수로만 주입한다. 로그나 문서에 값을 붙여 넣지 않는다.

```sh
curl --no-buffer --request POST "${AI_BASE_URL}/api/v1/data/analyze/stream" \
  --header "X-Internal-Token: ${INTERNAL_TOKEN}" \
  --header "X-Request-ID: data-job-123" \
  --header "Idempotency-Key: analysis-job-123" \
  --header "Accept: text/event-stream" \
  --header "Content-Type: application/json" \
  --data '{"platform":"kurly","product_id":"1000146248","reviews":[{"review_id":"135039521","content":"리뷰 내용","rating":5,"written_at":"2026-10-07T10:00:00"}]}'
```

POST와 커스텀 헤더/body가 필요하므로 브라우저 기본 `EventSource` 대신 streaming fetch 또는 서버 HTTP streaming client를 사용한다.
새 환경변수나 의존성, Docker/Azure workflow 변경은 없다. 기존 모델 마운트·worker 1·내부 인증·MySQL 설정을 유지한다.
운영 적용 전에 CREATE TABLE 권한, 프록시 buffering/read timeout, Data의 재시도/backoff·중복 result 처리·done 판정을 확인한다.
