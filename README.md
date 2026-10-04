# Re:view AI 분석·저장 서비스 (수집/SSE 통합 준비)

Data 서버가 HTTP로 전달한 리뷰를 분석해 JSON을 반환합니다.
분석 결과는 AI 자체 MySQL DB에 먼저 저장합니다. Redis는 사용하지 않습니다.
`FireViewLab/review-ai-new` main(`ff3c149`)에서 검증한 KoELECTRA·행동·네트워크 런타임을
기존 운영 구조에 통합하는 로컬 검증 단계입니다. 공식 분석 경로는 `POST /api/v1/data/analyze`입니다.
새 연동은 [v0.5 API·인증·계산 정책](docs/data-ai-v05-integration.md)을 먼저 확인하세요.
수집/SSE 실험 경로는 기본 비활성화입니다. [통합 상태·미확정 사항](docs/integration-preparation.md)을 먼저 확인하세요.
검증 결과와 배포 전 TODO는 [2차 런타임 통합 보고서](docs/validation/review-ai-runtime-integration-20261002.md)를 참고하세요.

## 현재 상태와 역할

| 구분 | 현재 범위 |
| --- | --- |
| 기본 기능 | 리뷰 배치 분석, AI 자체 DB에 입력·결과 저장, 저장 성공 후 JSON 응답 |
| 실험 기능 (기본 OFF) | 외부 크롤러 SSE 수신, 수집 진행 알림, 분석 중 heartbeat, 저장 결과 조회 |
| 미구현 | 프로세스 재시작 후 작업 자동 재개, 멱등 요청, 결과 자동 재전송 |
| 팀 합의 필요 | HTTPS·실제 이벤트 샘플, 운영 계정·DB 백업·보존 정책 |

기본 API 처리 순서는 **요청 → 입력 저장 → 점수 계산 → 결과 저장 → HTTP 응답**입니다.
현재는 `202`로 접수만 알리는 작업 큐가 아니라, 계산·저장을 마친 최종 결과를 반환합니다.
크롤링 자체는 외부 크롤러가 담당하며, 이 저장소의 실험 기능은 그 스트림을 받아 분석에 연결합니다.
Data API와 SSE는 `app.services.analysis.analyze_reviews()`와 같은 결과 serializer를 사용합니다.
기본 가중치는 .5/.3/.2이며, 계산 불가 점수는 `-1`, RTI 등급 경계는 70/40입니다.
이전 운영 응답의 null·80/50·접두사 없는 사유 코드와 차이가 있으므로 Data 소비자 확인 후 배포해야 합니다.
과거 호환 분석 라우트는 운영 API에서 제거했습니다.

> 운영에서는 `REQUIRE_INTERNAL_TOKEN=1`과 `INTERNAL_TOKEN`을 설정하고 HTTPS로 공개하세요.
> 토큰 미설정·REQUIRE=0은 로컬 호환 모드입니다. DB 저장은 클라이언트의 결과 수신까지 보장하지 않습니다.

## 빠른 시작

Python 3.12와 Git이 필요합니다. 이미 저장소가 있다면 복제 단계는 건너뛰고
프로젝트 루트에서 실행하세요. 기존 `.env`와 `.venv`는 덮어쓰지 않습니다.

### 1. 설치와 서버 실행 (Windows PowerShell)

```powershell
git clone --branch main https://github.com/FireViewLab/review-ai-db.git
cd review-ai-db
if (-not (Test-Path .venv)) { py -3.12 -m venv .venv }
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt -r requirements-ml.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }

# .env의 DB_PASSWORD를 기존 MySQL 비밀번호로 설정한 뒤 DB를 실행합니다.
# DB_HOST=127.0.0.1, DB_PORT=3307 (Compose 내부에서는 ai-db:3306)
docker compose up -d ai-db
$env:ENABLE_EXPERIMENTAL_COLLECTION = "0"
$env:GOOGLE_APPLICATION_CREDENTIALS = ""
# 외부 모델 폴더의 실제 경로로 바꿉니다. 모델 파일을 저장소에 복사하지 않습니다.
$env:PTEXT_MODEL_PATH = "C:/models/ptext-koelectra-v1-2epoch-20260929"
$env:OMP_NUM_THREADS = "2"
$env:MKL_NUM_THREADS = "2"
.\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000 --workers 1
```

macOS/Linux에서는 가상환경 생성·설치·실행 명령을 다음처럼 바꾸면 됩니다.
`.env`가 없다면 `.env.example`을 복사하고, 기존 설정이 있다면 먼저 확인하세요.

```sh
if [ ! -d .venv ]; then python3.12 -m venv .venv; fi
.venv/bin/python -m pip install -r requirements-dev.txt -r requirements-ml.txt
docker compose up -d ai-db
ENABLE_EXPERIMENTAL_COLLECTION=0 GOOGLE_APPLICATION_CREDENTIALS="" \
  PTEXT_MODEL_PATH=/absolute/models/ptext-koelectra-v1-2epoch-20260929 \
  OMP_NUM_THREADS=2 MKL_NUM_THREADS=2 \
  .venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8000 --workers 1
```

서버가 켜지면 [Swagger UI](http://localhost:8000/docs)에서 API를 실행할 수 있습니다.
종료는 서버 터미널에서 Ctrl+C를 누릅니다.
위 예시는 `.env`에서 DB 접속 정보를 읽습니다. 기존 터미널에
`PYTHON_DOTENV_DISABLED=1`이 설정되어 있다면 해제하거나 새 터미널에서 실행하세요.

### 2. 공식 Data API 분석 요청

서버는 켜둔 채 **새 PowerShell 터미널**에서 실행합니다. 아래 예시는 로컬 토큰 미설정 기준이며,
인증 사용 시 `-Headers @{"X-Internal-Token"=$env:INTERNAL_TOKEN}`을 추가하세요.

```powershell
Invoke-RestMethod -Uri "http://localhost:8000/health"

$analysisBody = @{
    platform = "mall"
    product_id = "A001"
    reviews = @(
        @{
            review_id = "1001"
            content = "배송 빠르고 제품도 좋아요"
            written_at = "2026-09-08T00:00:00"
        }
    )
} | ConvertTo-Json -Depth 5

$analysisResponse = Invoke-WebRequest -Method Post `
    -Uri "http://localhost:8000/api/v1/data/analyze" `
    -ContentType "application/json; charset=utf-8" `
    -Body ([System.Text.Encoding]::UTF8.GetBytes($analysisBody))

$analysisResponse.StatusCode
$analysisResponse.Headers["X-Analysis-Job-ID"]
$analysisResponse.Content | ConvertFrom-Json | ConvertTo-Json -Depth 10
```

기대 결과는 health의 `status: ok`, 분석 HTTP `200`입니다. 점수는 실제 모델 추론 결과에 따라 달라집니다.
이 예시는 비교 리뷰와 행동 근거가 없어 해당 점수가 `-1`이며 텍스트 점수만 RTI에 반영합니다.
모델을 읽지 못하면 text_score도 `-1`입니다. 세 신호 모두 계산 불가이면 rti=`-1`, level=null입니다.
macOS/Linux에서는 Swagger UI의 `POST /api/v1/data/analyze` → **Try it out**에
아래 API 절의 JSON을 넣어 동일하게 확인할 수 있습니다.

기본 저장소는 MySQL의 `review_system.ai_analysis_jobs`입니다.
`DB_HOST/DB_PORT/DB_USER/DB_PASSWORD/DB_NAME`으로 접속하며, 시작 시 테이블이 없으면 생성합니다.
접속 실패 시 시작을 중단합니다. SQLite 자동 대체는 없고 `AI_RESULT_DB_PATH`는 사용하지 않습니다.
기존 팀 테이블은 변경하지 않으며, 과거 SQLite 데이터도 삭제하거나 자동 이관하지 않습니다.
정상 분석 응답은 결과의 DB 커밋이 완료된 뒤 반환됩니다.
작업 ID 헤더는 저장 기록 식별용이며, 기본 API에는 공개 결과 조회 경로가 없습니다.

## API

운영 Swagger에는 다음 두 경로만 표시됩니다 (`ENABLE_EXPERIMENTAL_COLLECTION=0`).

- `GET /health`: `{"status":"ok"}` (프로세스 liveness, 모델 lazy load/readiness 검사는 아님)
- `POST /api/v1/data/analyze`: Data/AI v0.5 공식 리뷰 배치 분석

`/docs`, `/redoc`, `/openapi.json`은 API 문서이며 `/`는 문서 화면으로 이동합니다.
토큰을 설정한 경우 분석 요청에 `X-Internal-Token` 헤더가 필요합니다.

요청 예시:

```json
{
  "platform": "mall",
  "product_id": "A001",
  "reviews": [{
    "review_id": "1001",
    "content": "배송 빠르고 제품도 좋아요",
    "rating": 5,
    "written_at": "2026-09-08T00:00:00"
  }]
}
```

응답 형태 예시 (text_score를 87로 고정한 모의 결과이며 위 문장의 실제 추론값이 아님):

```json
{
  "platform": "mall",
  "product_id": "A001",
  "review_count": 1,
  "results": [{
    "review_id": "1001",
    "rti": 87.0,
    "level": "safe",
    "text_score": 87.0,
    "behavior_score": -1,
    "network_score": -1,
    "reasons": ["TEXT_SHORT_REVIEW"]
  }]
}
```

필수 필드는 platform/product_id/reviews와 각 리뷰의 review_id/content입니다.
rating/written_at은 선택입니다. 리뷰 1~500개, 단일 상품 배치이며 ID와 요청 순서를 보존합니다.
누락·중복 리뷰 ID·잘못된 타입·알 수 없는 입력 필드는 422입니다.
기본 가중치는 text/behavior/network = 50/30/20이며 사용 가능한 신호만 재정규화합니다.
계산 불가 점수는 `-1`이며 null이나 0으로 대체하지 않습니다. 세 점수가 모두 -1일 때만 rti=-1/level=null입니다.
RTI는 소수점 한 자리로 반올림하며 70 이상 safe, 40 이상 warn, 40 미만 danger입니다.
reasons는 기본 `TEXT_*`/`BEHAVIOR_*`/`NETWORK_*` 코드 배열입니다. 선택적 Groq 후처리를 켜면
한국어 문장으로 바뀔 수 있으며 fallback 시 코드/문장이 섞일 수 있습니다. `review_count`는 응답 필수 필드입니다.
작업·입력·결과를 MySQL에 저장하고, 커밋 성공 후 X-Analysis-Job-ID와 최종 결과를 반환합니다.
분석·저장 실패는 503입니다. 자세한 내용은 [v0.5 연동 문서](docs/data-ai-v05-integration.md)를 참고하세요.

`POST /api/v1/analyze`는 **retired legacy endpoint**이며 라우트와 Swagger에서 제거되어 404를 반환합니다.
원본 review-ai-new의 `/analysis/...` 라우트는 추가하지 않았습니다. 기존 운영·실험 경로를 유지합니다.

## 선택적 Groq 사유 문장 후처리 (기본 OFF)

`ENABLE_GROQ_REASON_NATURALIZATION=1`과 유효한 키·strict JSON 지원 `GROQ_MODEL`을 설정하면
확정된 점수 계산 뒤 reasons만 한국어로 풀어 씁니다. 일반 API/SSE는 같은 후처리 결과를 MySQL에 저장한 뒤 반환합니다.
점수·등급·-1 규칙·식별자·응답 필드는 그대로이며, 외부 오류·설정 누락·시간 초과는 기존 코드로 fallback합니다.
키는 환경변수로만 관리합니다. 활성화 시 리뷰 본문이 Groq로 전송되므로 실제 키·문장 품질·외부 전송 정책을 확인해야 합니다.
배치·timeout·보조 키 전환·혼합 문자열 처리와 설정은 [Groq 후처리 안내](docs/groq-reason-naturalization.md)를 참고하세요.

## 테스트와 Docker 실행

프로젝트 루트에서 테스트를 실행합니다.

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

macOS/Linux에서는 `.venv/bin/python -m pytest -q`를 사용합니다.
2026-09-09의 과거 검증 기록은 42개 통과입니다. 가짜 크롤러 SSE와 로컬 DB를 사용하는 테스트이며,
실제 크롤러·Spring·Azure 연동 검증을 대신하지 않습니다.
현재 통합 코드의 결과는 [2차 런타임 통합 보고서](docs/validation/review-ai-runtime-integration-20261002.md)에 별도 기록합니다.
2026-10-02 로컬 통합 검증은 Windows와 Docker Linux에서 각각 전체 pytest **327개 통과**입니다.
실제 KoELECTRA CPU 추론·일반 API·MySQL 저장과 Docker config/build/up/healthy를 확인했습니다.
SSE는 실제 모델·MySQL과 모의 HTTPS Data 전송을 사용해 API/저장 결과 일치를 검증했으며 실제 Data 서버 왕복 검증은 남아 있습니다.

Docker Desktop의 **Linux 엔진** 또는 Linux Docker Engine이 실행 중이어야 합니다.
로컬 Uvicorn이 8000 포트를 쓰고 있다면 먼저 종료하세요.
`.env`에 `PTEXT_MODEL_HOST_PATH`를 외부 모델 폴더로 지정합니다. 컨테이너의 `PTEXT_MODEL_PATH`는
기본 `/models/ptext-koelectra-v1-2epoch-20260929`이며 해당 폴더를 읽기 전용으로 마운트합니다.
호스트 경로가 없으면 Compose 기동이 실패하며 빈 모델 폴더를 자동 생성하지 않습니다.

```sh
docker compose config --quiet
docker compose build
docker compose up -d
docker compose ps
docker compose logs --tail 50 ai
```

이후 위의 health·분석 요청으로 확인합니다. 아래 내용은 과거 기록입니다. 2026-09-09 통합 변경 당시에는
엔진 미실행으로 새 이미지 빌드·실행 검증을 완료하지 못했습니다.

Docker에는 AI 서비스와 MySQL 8.0이 있으며 AI 프로세스는 비-root 사용자로 실행합니다.
소스·실행 의존성만 복사하고 .env, 데이터, 팀원 DB, artifacts, 크롤링 도구는 제외합니다.
production에서는 reload를 사용하지 않고 worker는 1개입니다. `requirements-ml.txt`의 CPU 전용 torch와
transformers를 설치하며 OMP/MKL 스레드는 기본 2개입니다. 모델 lazy loading·프로세스 내 캐시·RLock을 유지합니다.
모델 파일은 이미지/Git에 포함하지 않습니다. CPU 이미지와 별개로 코드의 CUDA 자동 선택/CPU fallback 경로는 보존합니다.
MySQL은 기존 `ai-db-data` 볼륨(`/var/lib/mysql`)을 재사용합니다.
Compose 프로젝트 이름을 바꾸면 다른 볼륨이 생성되므로 기존 프로젝트 이름을 유지하세요.
MySQL 호스트 포트는 기본 `127.0.0.1:3307`이며 외부 네트워크에 공개하지 않습니다.
기존 볼륨의 비밀번호는 `.env` 수정만으로 바뀌지 않습니다. 인증 실패 시 기존 계정을 확인하세요.
`DB_USER=root`는 이전 로컬 구성 호환용이며 운영에서는 별도 최소 권한 계정을 사용하세요.
이 구성은 별도 계정을 자동 생성하지 않습니다. 기존 `ai-results` SQLite 볼륨은 삭제하지 않습니다.
일반 `docker compose down`은 볼륨을 유지하지만 **`docker compose down -v`는 결과 볼륨을
삭제하므로 사용하지 마세요.** 볼륨 보존과 별도로 운영 백업이 필요합니다.

## 실험적 수집/SSE

최종 공개 계약이 아닌 개발용 기능입니다. 기본 실행에서는 다음 경로가 등록되지 않아 404입니다.

- `POST /experimental/analysis/collect/stream`
- `GET /experimental/analysis/jobs/{job_id}`

조회 API는 저장된 JSON을 그대로 반환합니다. 신규 작업 결과는 통합 v0.5 계약이지만,
과거 null·80/50 정책으로 저장된 작업은 자동 변환·재계산하지 않습니다. 과거 조회까지 새 계약이라고 간주하지 마세요.

실험 경로 등록에는 `ENABLE_EXPERIMENTAL_COLLECTION=1`과 명시적 `DATA_SERVER_BASE_URL`이 필요합니다.
SSE는 일반 API와 같은 KoELECTRA·행동·네트워크 분석 및 v0.5 결과를 반환합니다. 누락 근거 점수는 `-1`로 처리하며
`ALLOW_LEGACY_CRAWLER_DEFAULTS`는 더 이상 사용하지 않습니다. 토큰 전송은 HTTPS만 허용합니다.
실제 이벤트 계약 확인 전 운영에서 켜지 마세요.
세부 입력 매핑·이벤트·연결 중단 한계는 [통합 준비 문서](docs/integration-preparation.md)에 정리돼 있습니다.

## 구조와 기존 파일

- main.py: 기존 ASGI 진입점과 분석 import 호환
- app/api/: HTTP 요청·응답 DTO와 라우팅
- app/factory.py: 전용 결과 저장소 초기화와 선택적 크롤러 client 수명 관리
- app/repositories/mysql_jobs.py: 기본 MySQL 작업/결과 저장소
- app/repositories/analysis_jobs.py: 저장소 인터페이스와 테스트·과거 파일 조회용 SQLite 구현
- app/contracts/, app/integrations/: 크롤러 SSE 계약과 수신 어댑터
- app/services/collection_stream.py: 수집 진행, 분석 중 heartbeat, 저장 후 result
- app/services/data_analysis.py, app/services/analysis.py, app/scoring/meta_scorer.py: 공식 v0.5 분석·저장, 공통 진입점, RTI 계산
- app/schemas/analysis.py: Python 진입점·API·SSE가 공유하는 평면 -1 결과 serializer
- ai/analysis.py: 과거 스크립트 호환용 비운영 분석 모듈
- app/analyzers/: KoELECTRA P_text, 검증된 P_behavior/P_network (review-ai-new ff3c149 기준)
- ai/text_analyzer.py, behavior_analyzer.py, network_analyzer.py: 과거 스크립트 참고용 비운영 분석기
- ai/sentiment_client.py: 기존 선택적 Google Cloud 감성 분석
- tests/: HTTP·입력 검증·점수 회귀 검사
- scripts/, db/, 기존 repository/crawler/product 서비스: 과거 개발 참고용 보존 (새 결과 DB와 별개)
- app/core/database.py, app/worker/consumer.py, worker/redis_consumer.py: deprecated 안내만 제공

과거 DB 조회 API 5개(/api/internal/ai/...)는 등록을 해제했으며 404입니다.
Data 서버는 상품 ID만 보내던 방식에서 실제 reviews를 보내는 방식으로 전환해야 합니다.
크롤링 참고 도구가 필요하면 requirements-legacy.txt를 별도로 설치합니다.
SQLite 검증 스크립트는 production API 테스트와 별개입니다.

## 과거 Google Cloud 감성 분석 설정

아래 설정은 과거 비운영 분석 모듈의 참고용입니다. 공식 v0.5 API에는 Google 감성 adapter를
자동 연결하지 않으므로 GOOGLE_APPLICATION_CREDENTIALS를 설정해도 공식 분석 점수는 바뀌지 않습니다.
ENABLE_CLOUD_NLP는 기존 코드에서도 읽지 않았으므로 새 설정으로 사용하지 않습니다.
인증 JSON을 코드나 이미지에 넣지 마세요.

호스트의 인증 파일을 별도로 준비하고 컨테이너 uid 10001이 읽을 수 있도록 한 후:

```sh
export GOOGLE_CREDENTIALS_FILE=/absolute/secure/google-credentials.json
sudo --preserve-env=GOOGLE_CREDENTIALS_FILE docker compose -f docker-compose.yml -f compose.google.yml up -d --build
```

자동 배포에도 사용하려면 VM의 docker-compose.override.yml에 같은 읽기 전용
마운트와 GOOGLE_APPLICATION_CREDENTIALS 설정을 추가합니다.
실제 인증 파일은 VM에 보관하며 GitHub workflow는 .env를 생성하거나 덮어쓰지 않습니다.

새 VM 설치와 배포 절차: [Azure 배포 안내](docs/azure-ai-deployment.md).
전환 분석과 보존/종료 범위: [독립 서비스 전환 기록](docs/ai-service-migration.md).
