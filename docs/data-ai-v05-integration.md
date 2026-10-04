<!-- v0.5 결과 계약과 Data Swagger를 기준으로 구현한 API·인증·분석 정책을 설명한다. -->
# Data AI v0.5 연동

## 적용 범위

2026-10-02 인수인계 문서와 `FireViewLab/review-ai-new` main(`ff3c149`)의 검증된 런타임을
기존 운영 구조에 통합하는 로컬 검증 단계다. 공식 분석 API는 POST /api/v1/data/analyze다.
요청 필드·인증·MySQL 저장·실험 SSE 경로는 유지하고 공통 `analyze_reviews()`와 결과 serializer를 사용한다.
2차 실측·테스트 결과는 [런타임 통합 보고서](validation/review-ai-runtime-integration-20261002.md)에 기록한다.

| 결과 정책 | 이전 review-ai-db 운영 결과 | 통합 runtime 기준 |
| --- | --- | --- |
| 계산 불가 점수 | null | -1 (0/null 대체 금지) |
| 모든 신호 계산 불가 | rti/level=null | rti=-1, level=null |
| RTI 등급 경계 | 80/50 | 70/40 |
| reasons | 접두사 없는 코드 | TEXT_ / BEHAVIOR_ / NETWORK_ 출처 접두사 |
| RTI 표기 | 별도 반올림 없음 | 소수점 한 자리 반올림 |

위 차이는 인수인계 런타임에 맞춘 계약 전환이다. 실제 릴리스 전에 Data 소비자가 -1·등급·사유 코드를
처리할 수 있는지 확인해야 한다. 이번 단계에서는 commit/push나 Azure 배포를 수행하지 않는다.

| 경로 | 입력·응답 | 계산 |
| --- | --- | --- |
| POST /api/v1/data/analyze | v0.5 정규화 리뷰 → 평면 결과 | 팀원 분석기, 기본 가중치 .5/.3/.2, 가용 신호로 재정규화 |
| POST /experimental/analysis/collect/stream | 수집 진행 SSE → 최종 v0.5 result | 새 Data 분석과 같은 분석기, 기본 OFF |

공식 분석 및 실험 SSE 경로 모두 입력·결과를 AI 자체 MySQL에 저장한다. DB 커밋 후에만 성공 결과를 반환한다.
이 계약은 통합 이후 새로 생성하는 분석 결과에 적용한다. 기본 OFF인
GET /experimental/analysis/jobs/{job_id}는 저장된 과거 JSON을 그대로 반환하므로
이전 null·80/50 결과를 새 -1·70/40 계약으로 자동 변환하지 않는다. 과거 행의 자동 이관·재계산은 없다.
새 경로는 계산 완료 응답 방식이며 202 접수·백그라운드 큐·콜백 방식이 아니다.
Data의 결과 DB에 직접 쓰지 않고 결과 JSON을 응답한다. 별도 결과 수신 URL은 추측해 만들지 않았다.

## 요청과 결과

```json
{
  "platform": "mall",
  "product_id": "0007",
  "reviews": [{
    "review_id": "00:01",
    "content": "배송 빠르고 제품도 좋아요",
    "rating": 5,
    "written_at": "2026-09-27T12:00:00"
  }]
}
```

아래는 text_score=87을 고정한 모의 결과다. 위 본문의 실제 KoELECTRA 추론값을 보장하지 않는다.

```json
{
  "platform": "mall",
  "product_id": "0007",
  "review_count": 1,
  "results": [{
    "review_id": "00:01",
    "rti": 87.0,
    "level": "safe",
    "text_score": 87.0,
    "behavior_score": -1,
    "network_score": -1,
    "reasons": ["TEXT_SHORT_REVIEW"]
  }]
}
```

- platform/product_id/review_id는 문자열 그대로 보존한다. 숫자 변환·trim·접두사 추가를 하지 않는다.
- 한 요청은 한 상품, 리뷰 1~500개다. 500개는 O(n²) 비교 비용을 제한하는 우리 서버의 상한이며 운영 합의가 필요하다.
- 같은 review_id를 두 번 보내면 422다. 결과는 요청 순서대로 정확히 매칭한다.
- rating/written_at은 선택이며 원본 저장에 보존한다. rating은 현재 분석 입력에 사용하지 않는다.
- 최소 입력에는 구매 인증·안정적인 사용자 ID·작성 이력이 없으므로 behavior_score는 -1이다.
  written_at만으로 행동 점수를 만들지 않는다. SSE의 표시용 author로 사용자 동일성을 추측하지 않는다.
- P_text는 로컬 KoELECTRA의 suspicious probability를 신뢰 점수로 변환한다. 모델 임계값은 inference_config.json을 따른다.
- P_network는 동일 상품 배치에서 Hybrid A(문자 n-gram TF-IDF cosine 40% + canonical bigram Dice 60%)를 사용한다.
  너무 짧은 본문이나 비교 근거가 없으면 -1이다. 원본 ff3c149의 정책·threshold·감점식을 유지한다.
- RTI는 text/behavior/network=50/30/20의 가용 신호만 합계 1로 재정규화하고 소수점 한 자리로 반올림한다.
- level은 반올림한 RTI 70 이상 safe, 40 이상 warn, 그 미만 danger다.
- reasons는 원본 source/code로 `TEXT_*`, `BEHAVIOR_*`, `NETWORK_*`를 만들며 중복 코드를 제거하고 순서를 보존한다.
  AI 런타임 통합 단계에서는 Groq/LLM 자연어 변환을 연결하지 않았다.
  이후 선택적 Groq 후처리 활성화 시 문장으로 바뀔 수 있다. 필드 타입은 list[str]이며 오류 시 코드로 fallback한다.
- 계산 불가 text_score/behavior_score/network_score는 -1이며 정상 분석 결과다. 요청 오류는 422, 분석·저장 실패는 503이다.
- 세 신호 모두 -1이면 rti=-1, level=null이다. 점수 필드에는 null을 허용하지 않는다.
- 응답 review_count는 필수이며 results 길이와 같아야 한다. platform/product_id/review_id와 요청 순서를 보존한다.

## 모델 경로와 lazy loading

`PTEXT_MODEL_PATH`는 config.json, inference_config.json, model.safetensors와 tokenizer 파일이 있는 디렉터리다.
Python 직접 실행은 실제 경로를 지정하고, Compose는 `PTEXT_MODEL_HOST_PATH`를 컨테이너의
`/models/ptext-koelectra-v1-2epoch-20260929`(기본 PTEXT_MODEL_PATH)에 읽기 전용으로 마운트한다.
모델 폴더는 Git/이미지에 넣지 않는다. 없는 호스트 경로는 Compose 기동 오류로 처리한다.

P_text는 첫 분석 시 로드하고 프로세스 내 캐시 및 RLock으로 로딩·추론을 직렬화한다.
worker는 1개이며 OMP_NUM_THREADS/MKL_NUM_THREADS는 기본 2다. 여러 worker를 켜면 모델 메모리가 프로세스마다 복제된다.
코드는 CUDA 가능 시 GPU를 선택하고 그 외에는 CPU를 선택한다. 기본 이미지는 requirements-ml.txt의 CPU 전용 torch를 사용한다.
health는 lazy loading을 유발하지 않으며 모델이 없거나 읽기 불가여도 프로세스 health는 OK일 수 있다.
따라서 별도의 실제 모델 분석 smoke test가 필요하다. 모델 로딩 실패는 text_score=-1로 전달되며 임의 규칙 점수로 대체하지 않는다.

## 인증과 TLS

분석 API와 실험 API는 `X-Internal-Token` 헤더를 검사한다. `/health`는 Docker 상태 확인용으로 인증 없이 유지한다.

```dotenv
# 실제 값은 .env 또는 배포 비밀 저장소에서 설정하며 Git에 넣지 않는다.
REQUIRE_INTERNAL_TOKEN=1
INTERNAL_TOKEN=
DATA_SERVER_BASE_URL=
DATA_INTERNAL_TOKEN=
ENABLE_EXPERIMENTAL_COLLECTION=0
```

- 운영에서는 REQUIRE_INTERNAL_TOKEN=1과 유효한 INTERNAL_TOKEN을 함께 설정해야 한다. 없으면 시작 실패한다.
- REQUIRE_INTERNAL_TOKEN=0이고 토큰도 없을 때만 기존 로컬 무인증 동작을 유지한다.
- DATA_INTERNAL_TOKEN은 AI → Data 요청용이다. 비어 있으면 INTERNAL_TOKEN을 재사용한다.
- DATA_SERVER_BASE_URL은 확정된 HTTPS 주소로 설정한다. 비어 있을 때만 과거 CRAWLER_BASE_URL을 읽는다.
- 토큰을 사용하는 Data SSE 수신기는 HTTP를 거부한다. 리다이렉트를 따라가지 않고 타 호스트 경로도 거부한다.
- 실제 공유 토큰은 이번 코드·테스트·문서에 저장하지 않았다. 테스트는 가짜 토큰만 사용한다.
- AI의 외부 공개 주소에도 TLS termination이 필요하다. 이 앱이 인증서나 프록시를 자동 설치하지는 않는다.
- `docker compose config`는 환경변수의 비밀 값을 펼칠 수 있으므로 검사는 `--quiet`를 사용한다.

## SSE와 Swagger 확인 범위

확인한 Data OpenAPI: `http://34.50.27.128:8000/openapi.json`, title=review-data, version=0.1.0.
공개 명세만 인증 없이 읽었고 실제 리뷰 수집이나 토큰 전송은 하지 않았다.

- GET /{platform}/products/{product_id}/reviews/stream, limit, Last-Event-ID, X-Internal-Token은 확인했다.
- 응답 상세 필드는 명세에 비어 있으므로 기존 review/progress/done/error/heartbeat payload의 실샘플 확인이 필요하다.
- SSE는 수집 중 진행 상황을 전달하고, done 이후 전체 배치를 한 번 분석한다. 새 모델의 최종 결과는 v0.5 평면 형식이다.
- 수집·분석 중 heartbeat와 DB 저장 선행, 중복 검증, 연결 종료 후 이미 시작한 분석의 저장 동작을 유지한다.
- ALLOW_LEGACY_CRAWLER_DEFAULTS는 새 SSE 경로에서 더 이상 사용하지 않는다. 빈 근거의 기본 점수를 생성하지 않는다.
- product_key로 여러 상품을 묶는 요청은 422다. 원본 platform/product_id 단위만 허용한다.
- Data의 GET /api/v1/jobs/{job_id}는 정수 ID, AI의 X-Analysis-Job-ID는 UUID다. 동일 ID라고 간주하지 않는다.
- CRAWLER_MAX_RETRIES는 기본 0이다. Last-Event-ID가 영구 재개·멱등 처리를 보장하지 않는다.

## 분석기 출처와 검증 한계

현재 출처: FireViewLab/review-ai-new `ff3c149`의 app/analyzers, app/scoring,
app/services/analysis, app/schemas/analysis와 필요한 integrations 구현이다.
운영 API/SSE는 공통 services.analysis.analyze_reviews()를 호출한다.
services/team_analysis는 기존 import 호환용 재노출 모듈로 남기고 과거 normalize 도구는 별도로 보존한다.
원본 `/analysis/...` API는 이 서버에 새로 등록하지 않는다. AI 런타임 통합 단계에는 KoELECTRA 학습·진단·Groq 연동을 포함하지 않았다.
Google 감성 서비스는 공식 P_text의 점수 대체 경로가 아니다.

### 현재 로컬 통합 검증 (2026-10-02)

- Windows 전체 pytest 327개 통과(경고 2개), Docker Linux 동일 CPU 이미지 전체 pytest 327개 통과(경고 1개).
  양쪽 모두 실제 MySQL 저장 검증을 포함한다.
- Docker config/build/up/healthy 통과. health 200, 미인증 분석 401, 인증 분석 200, retired legacy endpoint 404.
- 실제 외부 모델을 사용한 일반 API와 SSE 및 MySQL 저장 결과가 일치했다.
  SSE의 Data HTTPS 전송은 모의 transport이며 실제 Data 서버 요청은 하지 않았다.
- 2 CPU/4 GiB 제한, CPU 스레드 2개에서 모델 로딩 1.5481초, 최초 리뷰 1건 추론 0.7861초,
  합성 리뷰 100건 분석 4.4714초, peak process RSS 690.67 MiB, cache miss 1회였다.
- 모델 경로·읽기 전용 마운트·worker 1을 확인했다. 실제 Azure 배포와 Groq 연동은 하지 않았다.

자세한 조건·측정 해석·남은 TODO는 [2차 통합 보고서](validation/review-ai-runtime-integration-20261002.md)를 따른다.

### 이전 단계 기록 (현재 런타임 검증과 구분)

2026-09-27의 최초 통합은 DMU-FireView/review-ai-new `c48b7e566bf8e5d4c832c2fcad64da4406af707e` 기준이었다.

테스트는 격리 SQLite 저장소와 모의 Data SSE로 실행한다. 실제 MySQL·GCP HTTPS·Data 왕복 연결 검증을 대신하지 않는다.
이전 통합 단계 검증은 전체 pytest 237개 통과·실제 MySQL 테스트 1개 보류, 실제 로컬 TCP HTTP와 Compose 설정 검사 통과다.
당시 호환 API 점수 88과 Data API 점수 75(동일 짧은 본문·비교 근거 없음)를 각각 회귀 검증했다.
위 기록은 최초 통합 당시 기준이다. 실제 클라우드 계정·확정 HTTPS 주소·입력/SSE 샘플을 이용한 운영 검증과 구분한다.
v0.4 검토용 모델·문서·샘플은 과거 비교용으로 보존하며 새 API 응답으로 사용하지 않는다.

## Legacy API 폐기

POST /api/v1/analyze는 **retired legacy endpoint**다. 라우트·Legacy AI Analysis 태그·전용 저장 서비스를 제거했다.
2026-09-28 제거 당시에는 분석기·50/30/20 가중치·null 처리·당시 등급·MySQLJobStore를 변경하지 않았다.
이 정책은 당시 기록이며, 현재 통합 결과의 -1·70/40 기준은 위 적용 범위를 따른다.
실험 경로는 삭제하지 않았으며 ENABLE_EXPERIMENTAL_COLLECTION=0이면 라우트와 Swagger 모두에서 빠진다.
플래그를 켜면 Swagger에 표시된다. include_in_schema=False로 문서에서만 숨길 수도 있으나
보안·접근 차단이 아니므로 이번에는 임의로 적용하지 않았다.

### 제거 후 검증 2026-09-28

- pytest 220개 통과: 격리된 임시 MySQL을 사용하여 선택적 실제 DB 테스트도 실행했다.
- Docker Compose 이미지 빌드 성공, 임시 AI 컨테이너 health=healthy 및 GET /health 200.
- 토큰 인증 POST /api/v1/data/analyze 200, RTI 75, MySQL에 저장된 결과와 HTTP 응답 일치.
- POST /api/v1/analyze는 retired legacy endpoint로 404, Legacy AI Analysis 태그 없음.
- OpenAPI paths는 GET /health와 POST /api/v1/data/analyze 두 개뿐이다 (실험 기능 OFF).
- 기존 서버·DB 컨테이너는 교체하지 않고 임시 자원으로 검증했다. 이 기록은 Azure 자동 배포 성공의 증거는 아니다.

## 현재 후처리 단계: 선택적 Groq reasons 자연어화

AI의 확정 점수와 사유 코드를 먼저 만든 뒤 운영 분석·저장 계층에서 reasons만 한국어로 풀어 쓴다.
기본 `ENABLE_GROQ_REASON_NATURALIZATION=0`에서는 외부 요청 없이 기존 코드를 반환한다.
활성화하더라도 RTI·등급·세 점수·review_count·식별자와 Result Contract v0.5 필드 구조는 그대로다.
일반 API와 실험 SSE는 같은 후처리를 거쳐 MySQL 저장 결과와 최종 응답을 일치시킨다.

reasons는 활성화/오류/시간 예산에 따라 한국어 문장 또는 코드가 될 수 있고, 한 배치에서 혼합될 수도 있다.
Data 소비자는 list[str]을 보존하고 모든 문자열을 코드 enum으로만 해석하지 않도록 확인해야 한다.
과거 저장 JSON은 재번역하지 않는다. 외부 실패 시 점수는 유지하고 해당 리뷰의 원래 코드를 저장·반환한다.

전체 리뷰 본문과 코드만 합성 배치 참조와 함께 Groq에 전송하며 실제 식별자·점수·내부 인증 토큰은 보내지 않는다.
최대 20건/32 KiB chunk, 순차 처리, 전체 2 * GROQ_TIMEOUT_SECONDS 예산,
기본 키 대상 오류 시 보조 키 1회 전환으로 요청을 제한한다. 자세한 설정·검증·개인정보 범위는
[Groq reasons 후처리 문서](groq-reason-naturalization.md)를 따른다.

앞의 327개 통과·모델/DB 실측은 Groq 도입 이전 AI 런타임 검증 기록이다.
후처리의 실제 외부 모델 문장 품질·키 quota·지원 모델은 별도 실제 키 검증이 필요하다.
