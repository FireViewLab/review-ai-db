<!-- 확정된 AI 점수를 유지하며 reasons 문자열만 선택적으로 한국어로 풀어 쓰는 후처리 안내. -->
# Groq reasons 한국어 후처리

## 적용 범위

KoELECTRA·P_behavior·P_network·RTI 계산이 끝난 뒤 `reasons`만 한국어 문장으로 풀어 쓴다.
일반 `POST /api/v1/data/analyze`와 실험 SSE는 같은 후처리 경로를 거쳐 MySQL에 결과를 저장한다.
후처리 결과를 저장한 다음 HTTP 응답 또는 SSE result를 보내므로 저장 JSON과 신규 응답이 일치한다.

기본값은 `ENABLE_GROQ_REASON_NATURALIZATION=0`이다. 비활성화하거나 필요한 설정이 없으면
외부 요청 없이 기존 출처 코드(`TEXT_*`, `BEHAVIOR_*`, `NETWORK_*`)를 그대로 반환한다.
Python의 `app.services.analysis.analyze_reviews()`는 결정적 분석·코드 생성 진입점이며,
Groq 후처리는 운영 분석·저장 계층에서 적용한다.
구현은 `app/integrations/groq_reason_naturalizer.py`의 동기 batch helper를 사용하며
API/SSE 분석 worker에서 실행한다. 이벤트 루프 안에서 직접 호출하면 코드 fallback한다.

`rti`, `level`, `text_score`, `behavior_score`, `network_score`, `review_count`, 식별자와 순서는
AI 런타임의 값을 그대로 유지한다. unavailable=-1, 전부 계산 불가일 때 level=null,
50/30/20 재가중·70/40 등급·모델 threshold도 후처리 대상이 아니다.
외부 Result Contract v0.5의 필드 구조와 `reasons: list[str]` 타입은 그대로다.

`reasons`의 문자열 내용은 설정과 성공 여부에 따라 코드 또는 한국어 문장이 될 수 있다.
일부 chunk만 성공하거나 시간 예산을 소진하면 한 결과 배치 안에도 두 형태가 섞일 수 있다.
Data 소비자는 모든 문자열을 반드시 코드 enum이라고 가정하지 않아야 한다.
과거 작업 조회는 저장 JSON을 그대로 반환하며 기존 결과를 다시 번역하거나 이관하지 않는다.

## 설정

실제 값은 배포 환경변수 또는 Git 추적에서 제외된 로컬 `.env`에서 설정한다.
키를 README, 커밋, 모델 폴더, Dockerfile, DB 결과 JSON에 넣지 않는다.

```dotenv
ENABLE_GROQ_REASON_NATURALIZATION=0
GROQ_API_KEY_PRIMARY=
GROQ_API_KEY_SECONDARY=
GROQ_MODEL=
GROQ_TIMEOUT_SECONDS=5
```

| 환경변수 | 의미 |
| --- | --- |
| ENABLE_GROQ_REASON_NATURALIZATION | 1일 때만 후처리 활성화. 기본 0 |
| GROQ_API_KEY_PRIMARY | 첫 Groq 요청에 쓰는 기본 키 |
| GROQ_API_KEY_SECONDARY | 기본 키의 대상 오류 발생 시 한 번 대체하는 보조 키 |
| GROQ_MODEL | Groq strict structured output을 지원하는 모델명. 실제 모델명을 운영자가 명시 |
| GROQ_TIMEOUT_SECONDS | 요청당 timeout(초). 기본 5, 유효 범위 0 초과~30 이하, 전체 예산은 이 값의 두 배 |

환경변수 예제는 비밀값을 제공하지 않는다. `docker compose config`의 전체 출력에는
키가 펼쳐질 수 있으므로 설정 검사에는 `docker compose config --quiet`를 사용한다.
추가 Groq SDK 의존성 없이 기존 HTTPX로 HTTPS API를 호출한다.
필수 기본 키·모델이 비어 있거나 timeout이 유효 범위를 벗어나면 외부 요청 없이 코드를 유지한다.
보조 키는 선택이다. 호출 대상은 `https://api.groq.com/openai/v1/chat/completions`로 고정하며
redirect와 환경 proxy를 따르지 않는다.
외부 모델 지원·가용성은 운영자가 [Groq Structured Outputs](https://console.groq.com/docs/structured-outputs)에서 확인해야 한다.

Groq 요청에는 `reasoning_effort: "low"`를 고정해 전달한다. 계획된 운영 모델
`openai/gpt-oss-20b`는 운영자가 `GROQ_MODEL` 환경변수로 명시해야 하며 자동 기본 모델로 선택하지 않는다.
`GROQ_MODEL`이 없거나 비어 있으면 기존과 같이 외부 요청 없이 원래 코드를 반환한다.
모델별 reasoning effort 지원은 [Groq Reasoning](https://console.groq.com/docs/reasoning)을 참고한다.

## 입력과 출력 검증

Groq에는 리뷰 본문 전체와 이미 계산된 사유 코드만 전달한다. 점수·등급·실제
platform/product_id/review_id 필드·서버 간 인증 토큰은 보내지 않는다.
응답을 원래 리뷰에 연결하는 데 필요한 식별은 해당 후처리 배치 안의 합성 참조값을 사용한다.
리뷰 본문은 명령이 아닌 데이터로 취급하도록 요청하며, 새로운 판단·점수·사유를 만들지 않도록 지시한다.

Groq에 보내는 JSON schema는 strict structured output을 요구한다. 응답에서도 합성 참조값,
사유 코드와 순서, 코드별 한국어 문장 대응을 직접 검증한다. 누락·중복·알 수 없는 코드,
변경된 코드, 잘못된 타입이나 형식은 성공으로 처리하지 않고 원래 코드를 유지한다.
내부 응답의 code/message 객체는 외부 응답에 추가하지 않는다. 검증한 문장만 기존 reasons 배열에 넣는다.
Groq 내부 참조 필드는 `item_id`이며 실제 리뷰 ID 대신 요청 내 배열 위치의 문자열을 사용한다.
응답은 최대 128 KiB, 각 message는 500자 이하의 비어 있지 않은 한 줄이어야 한다.
내용을 충실하게 풀어 쓸 수 없는 경우에는 message 자체를 원래 코드로 유지하도록 요청한다.

이 검증은 구조·대응 관계를 검사한다. 한국어 문장이 원래 분석 근거의 의미를 정확히
설명하는지는 자동으로 완전히 보장하지 못한다. 실제 키·확정 모델로 정상/부족 근거/오류
샘플을 검토한 뒤 활성화해야 한다.

## 배치와 시간 제한

- 요청은 최대 20개 리뷰씩 순차 처리한다. 무제한 병렬 요청을 만들지 않는다.
- 전체 HTTP 요청 JSON은 system prompt와 schema를 포함해 chunk당 최대 32 KiB다.
  한 리뷰가 한도를 넘으면 그 리뷰를 보내지 않고 코드를 유지한다.
  본문을 잘라 의미를 바꾸거나 가짜 근거를 추가하지 않는다.
- 전체 후처리 예산은 `2 * GROQ_TIMEOUT_SECONDS`이며 기본 10초다. 기본 요청당 timeout은 5초다.
- 시간 예산을 소진하면 남은 리뷰의 코드를 유지한다. 최대 500건을 전부 번역하려고 API 응답을 무기한 기다리지 않는다.
- 이미 성공한 chunk의 문장은 유지하므로 timeout·오류·입력 한도에 따라 코드/문장이 혼합될 수 있다.

## 오류와 키 전환

기본 키에서 401/403/408/429/5xx 또는 연결·timeout 오류가 발생하면 같은 chunk를 보조 키로 한 번 시도한다.
보조 키로 전환한 뒤에는 해당 분석 배치의 남은 chunk도 보조 키를 사용한다.
두 키 모두 실패하면 외부 후처리를 중단하고 아직 처리하지 않은 리뷰는 코드를 유지한다.
400 등 요청·모델 설정 오류나 그 밖의 재시도 대상이 아닌 HTTP 응답은 후처리를 중단한다.
HTTP 200의 JSON/schema 결과가 잘못된 경우에는 해당 chunk를 코드로 유지하고 다음 chunk를 처리한다.
두 경우 모두 보조 키 전환이나 동일 chunk 재시도를 하지 않는다.
오류 유형은 [Groq 오류 안내](https://console.groq.com/docs/errors)를 참고한다.

Groq 장애는 점수 분석 성공을 실패로 바꾸거나 503을 만드는 이유가 아니다. 후처리만 건너뛰고
AI가 만든 코드와 점수를 저장·반환한다. MySQL 저장 실패 등 기존 분석·저장 오류 처리는 유지한다.
서비스 로그에는 API 키·Authorization 헤더·리뷰 본문·Groq 응답 원문을 기록하지 않는다.

## 개인정보와 활성화 전 확인

후처리를 켜면 리뷰 본문과 사유 코드가 외부 Groq 서비스로 전송된다.
본문에는 개인정보나 식별자가 들어갈 수 있으며 본문 자체는 마스킹하지 않는다.
팀의 접근·보존·외부 전송 정책과 사용 가능한 키의 권한을 확인해야 한다.
실제 키를 사용하지 않은 모의 테스트는 외부 모델의 문장 품질이나 quota·모델 지원을 보장하지 않는다.

기본 OFF 상태에서 기존 API/SSE 점수·DB·health·인증을 회귀 검증하고, 활성화 모의 테스트로
reasons만 달라지는지, HTTP/SSE/저장 결과가 같은지, 키 전환·timeout·잘못된 출력을 검사한다.
실제 키 검증을 별도로 마친 다음 운영 설정을 켠다. `/health`는 모델·Groq를 호출하지 않는다.

이 기능은 기존 Azure 배포 workflow를 이용하되, PR 검증은 테스트 단계로 제한하고
main push에만 기존 자동 배포가 실행되도록 구분한다. PR merge 후에도 운영 환경변수를
별도로 설정하지 않으면 Groq는 OFF다. 실제 운영 .env 수정과 키 입력은 별도 활성화 단계다.

## 로컬 검증 기록 (2026-10-02)

- 기존 327개에 Groq 모의 테스트 74개를 추가했다. Windows 및 Docker Linux에서 각각 **401 passed**.
  전용 임시 MySQL에서 기존 lifecycle/API 저장과 자연어 reasons 저장 테스트를 모두 실행했다.
- 실제 외부 Groq 호출은 없었다. HTTPX MockTransport로 성공·키 전환·timeout·잘못된 출력·부분 성공을 검증했다.
- 기존 점수·ID·응답 필드 보존, 후처리 예외 시 API 200/원본 코드, API/SSE/저장 JSON 일치를 확인했다.
- Docker build와 기본/검증 Compose `config --quiet` 통과. 격리 컨테이너 health/API 200, startup error 없음.
- Groq OFF, worker 1, 2 CPU/4 GiB 제한에서 외부 KoELECTRA 실제 CPU 모델·API/SSE/MySQL smoke 통과.
  모델 캐시는 misses=1/currsize=1이며 health와 인증 실패가 모델을 로드하지 않았다.
- 전용 MySQL을 설정하지 않는 일반 pytest/CI에서는 실제 DB 검증 2개가 명시적으로 skip된다.
  TestClient 의존성 deprecation 경고는 기존과 같이 남아 있다.

이 기록은 실제 Groq 모델 문장 품질·quota나 운영 활성화의 검증을 대신하지 않는다.
