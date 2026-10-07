<!-- GitHub PR 작성 시 아래 제목과 본문을 복사한다. 자동 PR/merge/배포는 수행하지 않는다. -->
# feat: Data POST 분석 SSE 및 DB 기반 idempotency 추가

## 목적

Data 서버가 리뷰를 POST하고 장시간 분석 중 heartbeat와 확정된 리뷰별 결과를 받을 수 있도록
`POST /api/v1/data/analyze/stream`을 추가합니다.

Base: `main` / Head: `feature/data-analysis-sse`

## 계약 및 호환성

- 기존 `POST /api/v1/data/analyze` JSON API, `DataAnalyzeRequestV05`, v0.5 serializer는 변경하지 않았습니다.
- 동일 `X-Internal-Token` 인증을 사용합니다. 신규 경로만 `X-Request-ID`, `Idempotency-Key`가 필수입니다.
- SSE: `meta`, `progress`, `heartbeat`, 리뷰별 `result`, `done`, 안전한 `error`.
- MySQL 커밋 후 저장 결과를 다시 읽고 요청 리뷰 순서대로 전달합니다.
- 기존 KoELECTRA/behavior/network/RTI/Groq 로직 및 -1 unavailable, 가중치 .50/.30/.20, 70/40 경계를 변경하지 않았습니다.
- 500건 제한 유지, 501건은 422. 분석 집합을 임의로 나누지 않습니다.
- 기존 experimental 수집 SSE는 별도 경로로 유지합니다.
- 새 환경변수/패키지/Docker/Azure workflow 변경이 없습니다. 모델/secret 파일을 추가하지 않았습니다.

## Idempotency 및 DB 변경

- 기존 `ai_analysis_jobs` schema/데이터는 그대로 유지합니다.
- `ai_analysis_idempotency`를 `CREATE TABLE IF NOT EXISTS`로 추가합니다.
- 키 해시 고유 제약, payload 해시, job FK, 시작 시점 버전을 저장합니다.
- MySQL은 두 insert를 하나의 트랜잭션으로 처리하고, 중복 키 패배 시 새 job도 롤백합니다.
- SQLite 테스트 저장소에서도 같은 의미를 구현합니다.
- 같은 키 + 동일 payload + DONE: 저장 결과 replay, 분석/Groq 재호출 없음.
- RUNNING: 409 `IDEMPOTENCY_IN_PROGRESS`; 다른 payload: 409 `IDEMPOTENCY_KEY_REUSED`.
- FAILED: 409 `IDEMPOTENCY_FAILED`, 자동 재분석하지 않습니다.
- 클라이언트 연결 종료 후에도 시작한 분석은 DB 저장까지 계속합니다. 정상 shutdown은 진행 작업을 기다립니다.

## 실제 검증 결과 (2026-10-07, 로컬)

- 기본 전체 pytest: **434 passed, 6 skipped** (실제 MySQL 선택 테스트 비활성).
- 격리 MySQL 8.0 활성 전체 pytest: **440 passed**, 실패/skip 없음.
  - 기존 JSON/실험 SSE 회귀, 신규 헤더/인증/422/OpenAPI.
  - heartbeat, disconnect 후 저장, 분석/DB 오류, DB 저장 선행.
  - 동일 키 8개 동시 등록: job 하나, 나머지 7개 conflict.
  - SQLite 별도 프로세스 replay, SQLite/MySQL API replay 시 분석 미호출.
- Docker Compose config `--quiet`: 성공.
- 기존 Dockerfile 기반 격리 이미지 build: 성공.
- 외부 KoELECTRA 모델 읽기 전용 마운트, worker 1, Groq OFF인 로컬 컨테이너 smoke:
  - `/health` 200, Docker health `healthy`, startup error 없음.
  - 실제 리뷰 3건 JSON 200 / SSE 200, 리뷰별 최종 결과 동일.
  - MySQL DONE 저장 결과와 JSON/SSE 결과 동일.
  - 동일 키 replay에서 같은 AI job ID 및 `meta → result×3 → done` 확인.
  - 잘못된 인증 401, 501건 422.
  - 관측 시간: 첫 JSON(모델 로딩 포함) 4.823초 / 후속 SSE 0.192초 / replay 0.039초.
  - 이는 로컬 3건 smoke 수치이며 Azure/500건 성능 보장은 아닙니다.
- 외부 실제 Groq/Data 호출, Azure 배포 및 운영 secret 수정은 수행하지 않았습니다.
- 기존 로컬 운영용 MySQL 컨테이너/볼륨은 수정·초기화하지 않았습니다.
- 테스트 경고: 기존 Starlette/httpx deprecation 및 Windows pytest cache 권한 경고. 테스트 실패는 없습니다.

## 변경 파일

- `app/api/data_analysis_stream.py`: 신규 라우트, 인증·헤더·HTTP 오류.
- `app/services/data_analysis_stream.py`: 분석 task 수명, heartbeat, 저장 결과 SSE/replay.
- `app/core/analysis_versions.py`: contract/model/policy 버전 중앙 관리.
- `app/repositories/idempotency.py`: claim 및 canonical hash/충돌 판정.
- `app/repositories/analysis_jobs.py`, `app/repositories/mysql_jobs.py`: additive DB 테이블과 원자적 claim.
- `app/factory.py`: 신규 router 등록, task 보관/정상 종료 대기.
- `tests/test_data_analysis_stream.py`, `tests/test_idempotency_store.py`: 신규 자동 검증.
- `tests/test_api_contract.py`, `tests/test_mysql_jobs.py`: 새 route/table의 추가를 기대 목록에 반영.
- `README.md`, `docs/README.md`, `docs/data-ai-v05-integration.md`: 신규 공식 경로 안내, 과거 기록 보존.
- `docs/data-analysis-stream.md`: Data 구현용 전체 계약/예시/curl/재시도 정책.
- `docs/pr-data-analysis-stream.md`: 이 PR 초안 및 검증 기록.

## 운영 적용 전 확인 및 남은 한계

- DB 사용자에게 신규 테이블 CREATE 권한이 필요합니다. 기존 테이블/볼륨 초기화는 필요 없습니다.
- Data는 POST streaming client를 사용하고, result만으로 완료 판단하지 말고 done을 기다려야 합니다.
- 동일 키 재시도/backoff와 replay 전체 결과의 중복 upsert 정책을 Data와 맞춰야 합니다.
- 프록시 buffering 비활성 및 read timeout이 heartbeat/장시간 분석에 적합한지 확인해야 합니다.
- 강제 프로세스 종료/OOM 시 RUNNING 자동 회수·작업 자동 재개는 지원하지 않습니다. 운영자 확인이 필요합니다.
- 키/결과 보존 TTL은 미정이며, idempotency 행을 남긴 상태에서 참조 job을 삭제할 수 없습니다.
- 신규 키를 무제한 접수하는 부하 제어/작업 큐는 이번 범위 밖입니다. worker 1을 유지합니다.
- 기본 API 목록: GET `/health`, POST `/api/v1/data/analyze`, POST `/api/v1/data/analyze/stream`.
- 기존 main push 배포 workflow는 그대로입니다. 이 PR merge 이후 배포 여부/시점은 담당자가 확인합니다.
