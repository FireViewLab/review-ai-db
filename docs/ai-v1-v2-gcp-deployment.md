# GCP v1/v2 병행 운영 준비 및 롤백 (실행 승인 전)

## 현재 범위와 승인

현재 저장소의 GCP workflow는 `review-ai-gcp-prod` (`asia-northeast3-a`)에 IAP/OS Login으로 접속해
`/home/reviewadmin/review-ai-db`에서 **기본 Compose**를 사용합니다. workflow는 수정하지 않았습니다.
2026-11-17 실제 운영 관측값은 아래와 같습니다. 디스크 여유, live `.env` 전체, 현재 모델 호스트 절대 경로는 확인되지 않았습니다.
기본 Compose 컨테이너 내부 v1 경로는 `/models/ptext-koelectra-v1-2epoch-20260929`이고,
호스트 경로는 운영 측 `PTEXT_MODEL_HOST_PATH` 설정을 확인하고 그대로 유지해야 합니다.

**배포 보류:** 전달된 v3.1 B `package_manifest.json`은 `PTEXT_V3_1_B_EXPERIMENTAL_NOT_APPROVED_FOR_PRODUCTION`으로 표시합니다.
독립적인 사람 라벨 평가/모델 담당자의 운영 승인이 아직 필요합니다. 합성 로드·판정 동작 및 MySQL/SSE 테스트 결과는
모델의 실제 정확도 또는 운영 전환 승인을 뜻하지 않습니다. 다음 절차는 문서화만 했습니다.

## GCP 운영 관측 — 2026-11-17

사용자가 `review-ai-gcp-prod`에 SSH로 직접 접속해 확인한 값입니다. 로컬 검증 환경에서 다시 측정한 값은 아닙니다.

| 항목 | 운영 관측 |
| --- | --- |
| VM | 2 vCPU, RAM 약 8 GiB |
| 분석 설정 | Uvicorn Worker 2, `PTEXT_BATCH_SIZE=8`, `OMP_NUM_THREADS=1`, v1 Groq 활성 |
| AI 컨테이너 | 표본에서 CPU 최대 약 198%, 메모리 약 2 GiB |
| 최근 1시간 작업 | DONE 77건, 해당 작업의 생성→갱신 평균 96.5초 |
| RUNNING | 5건 중 3건 장기 잔류 가능성. 상태/원인 미확정 |

2 vCPU에서 198%는 표본 시점에 CPU 여유가 작았음을 뜻하지만, 지속 포화나 최대 처리량을 입증하지 않습니다.
DONE 77건은 관측 구간의 완료 건수이며, 96.5초는 DB 타임스탬프 기준이라 추론 시간이나 SSE 지연으로 해석하지 않습니다.
운영 전에는 시간대·배치 크기별 CPU와 완료 지연 분포, v1 병행 영향, 장기 RUNNING의 실제 원인과 복구 책임을
스테이징/승인된 관측으로 확인합니다. 이 자료만으로 worker 수를 늘리거나 잔류 작업을 실패로 바꾸지 않습니다.

## 모델과 Compose

| 항목 | v1 | v2 |
| --- | --- | --- |
| 체크포인트 | `ptext-koelectra-v1-2epoch-20260929` 인수인계 폴더 | `ptext-v3.1-b-experimental-seed20261115-share-20261116/model` |
| 검증된 가중치 바이트 | 451,715,672 | 451,715,672 |
| `model.safetensors` SHA-256 | `e7677366e93afef3f26890c81fffa62e7a1db96f24cba55a19cdeaf9efb5f354` | `012b65174cdbc2e1fb36d75519438043bebd4317a69ea9997325b66bb436474b` |
| 컨테이너 경로 | 기존 `PTEXT_MODEL_PATH` 그대로 | `/models/ptext-koelectra-v3.1-b` (오버레이), `PTEXT_V31B_MODEL_PATH` |

각 폴더에는 `config.json`, `inference_config.json`, 가중치와 tokenizer/config/vocab/special tokens가 필요합니다.
v3.1 B의 raw probability threshold는 0.5, score temperature는 2, max length는 256입니다.
버전 메타데이터의 `ptext-koelectra-v3.1-b-seed20261115`는 위 manifest의 체크포인트를 뜻합니다.

1. 운영팀과 체크포인트 출처를 확정합니다. 승인 이후에만 기존 IAP/OS Login 및 제한된 운영 권한을 사용하여
   새 호스트 디렉터리(예시 `/opt/review-ai-models/ptext-koelectra-v3.1-b`)에 handoff의 `model/` 파일을 전송합니다.
   임시 폴더에서 체크섬을 검증한 뒤 v2 전용 최종 경로로 옮깁니다. 기존 v1 경로를 수정하거나 덮지 않습니다.
2. 서버에서 `sha256sum "$PTEXT_V31B_MODEL_HOST_PATH/model.safetensors"` 및 제공된 `SHA256SUMS.txt`에
   포함된 파일의 무결성을 확인합니다. Docker 사용자(UID 10001)에게 읽기 권한만 허용합니다.
   디스크는 신규 가중치 약 431 MiB + 설정/임시 업로드 분 + 이미지 빌드 여유 + DB 성장/백업 여유를 따로 확인합니다.
3. `docker-compose.v2.yml`을 명시할 때만 외부 v2 디렉터리를 읽기 전용으로 마운트합니다.
   `PTEXT_V31B_MODEL_HOST_PATH`는 실제 존재하는 절대 디렉터리입니다. `ENABLE_DATA_AI_V2=1`은 migration 후 켜고,
   `ENABLE_V2_GROQ_SUMMARY=0`을 초기값으로 사용합니다. `V2_GROQ_MODEL=openai/gpt-oss-20b`, 기존 PRIMARY와
   `GROQ_TIMEOUT_SECONDS`를 사용하되 v1 활성화 플래그/설정은 그대로 둡니다. 운영 비밀값은 로그 또는 Git에 쓰지 않습니다.
4. `docker compose -f docker-compose.yml -f docker-compose.v2.yml config --quiet`로 확인합니다. 호스트 경로가
   없으면 Docker가 마운트에 실패할 수 있으므로 기동 **전** 확인합니다. 디렉터리는 기동됐으나 체크포인트가 손상된 경우
   모델은 지연 로딩이므로 v2 작업만 `V2_MODEL_UNAVAILABLE`이 되고 v1과 `/health`는 그 실패에 의존하지 않습니다.

## MySQL: 추가 전용 마이그레이션

기존 `ai_analysis_jobs`와 `ai_analysis_idempotency`는 그대로입니다. 기존 테이블의 버전 JSON을 확장하는 선택지는
운영 DDL 및 기존 조회/멱등성 키 호환 위험이 있어 쓰지 않았습니다. `ai_analysis_v2_jobs`(입력/결과 JSON,
상태/설명 버전)와 `ai_analysis_v2_idempotency`(유일 키 해시, payload 해시, 작업 FK, 버전 JSON)를 별도로 만듭니다.
Data 서버 DB 변경과는 독립입니다. 기존 `DONE` 행 백필이나 점수 재계산은 없습니다.

1. 실제 `DB_NAME`·연결 대상을 확인하고, 운영 승인 하에 사내 비밀 관리 방식으로 기존 스키마와 결과를 백업합니다.
   예: `mysqldump --single-transaction --no-tablespaces --databases <db>`의 결과를 접근 제한된 백업 저장소에 보관.
   인증값은 명령줄이나 문서에 넣지 않습니다. InnoDB 스냅샷과 복원 절차를 별도 환경에서 검증합니다.
2. 아래 쿼리로 v1 행 수/상태와 기존 DONE 작업을 기준점에 기록합니다. 운영은 신규 요청이 동시에 들어오므로 단순 행 수
   비교에 더해 샘플 기존 UUID의 버전과 replay 결과를 비교합니다.

   ```sql
   SELECT status, COUNT(*) FROM ai_analysis_jobs GROUP BY status;
   SELECT COUNT(*) FROM ai_analysis_idempotency;
   SHOW TABLES LIKE 'ai_analysis_v2%';
   ```

3. 승인 후에만, **staged** 이미지에서 기존 DB 네트워크/환경변수를 물려받아 다음을 실행합니다.

   ```bash
   docker compose -f docker-compose.yml -f docker-compose.v2.yml run --rm --no-deps ai python -m app.repositories.v2_job_migration --apply
   docker compose -f docker-compose.yml -f docker-compose.v2.yml run --rm --no-deps ai python -m app.repositories.v2_job_migration --check
   ```

   v2 DDL만 실행되고 기존 DB advisory lock을 사용합니다. 두 테이블 CREATE는 재실행 가능합니다.
   일부 DDL만 성공한 경우 권한/기존 테이블 구조를 확인하고 재실행하세요. 운영 데이터나 테이블을 삭제하지 않습니다.
4. v1 쿼리와 기존 DONE replay를 반복하고, `SHOW CREATE TABLE ai_analysis_v2_jobs` 및
   `SHOW CREATE TABLE ai_analysis_v2_idempotency`로 키/외래 키/컬럼을 검사합니다. 격리 테스트와 동일하게 같은 키의
   v1/v2 결과를 각각 저장하고, v2 결과 재조회 및 동일 payload replay가 기존 반영 데이터에 영향을 주지 않는지 검사합니다.

## 배포 순서 및 롤백

1. [로컬·격리 검증](validation/ai-v1-v2-20261117.md) 후 Data 팀과 스테이징 계약·수집을 검증합니다.
   운영 승인 후 체크포인트와 VM 여유·마이그레이션을 확인하고, v1 트래픽을 유지한 상태에서만 v2를 기동합니다.
   아래 예시는 승인 후에만 실행합니다.

   ```bash
   docker compose -f docker-compose.yml -f docker-compose.v2.yml up -d --build ai
   curl --fail --retry 12 --retry-all-errors http://127.0.0.1:8000/health
   ```

   현재 GCP workflow는 기본 Compose만 실행합니다. v2가 켜진 뒤에 기존 deploy workflow를 그대로 실행하면
   오버레이 없이 재생성돼 v2가 꺼질 수 있습니다. 차후 일관된 운용 경로를 승인하기 전에는 배포를 섞지 않습니다.
2. `/health`, v1 JSON/SSE, v2 SSE 순서로 확인합니다. v2는 합성 리뷰로 인증·`meta` 버전·`result`·`done` 및
   replay를 확인합니다. `/health`가 정상이라고 v2 체크포인트/테이블까지 정상이라는 뜻은 아닙니다.
3. 권장 첫 시험은 v2 Groq OFF입니다. Groq ON은 별도 품질/운영 승인 후에만 적용합니다. 초기에는 v1/v2 실패율,
   응답 시간, RSS/컨테이너 RAM·CPU, DB 지연, Groq 429·반복 복귀율을 보세요. 프로세스별 Groq 제한은 공유 쿼터 제어가
   아니므로 다중 worker·v1 Groq 트래픽을 합산합니다.
4. 오류 증가 시 Data 트래픽을 v1로 되돌리고 v2/Groq 플래그를 끕니다. 필요하면 정상으로 검증된 v1 이미지 및 기본
   Compose로 복귀합니다. **v2 모델 파일과 두 새 테이블은 남겨 둡니다.** `down -v`·볼륨 삭제·기존 데이터 롤백은 하지 않습니다.

같은 컨테이너는 기존 인증/DB/캐시 구조를 재사용하기 쉽지만 worker마다 모델 두 개까지 적재될 수 있습니다.
별도 컨테이너·프록시 라우팅은 운영 변경이 더 크지만 리소스 제한을 별도로 설정하고 v1을 독립 롤백하기 쉽습니다.
동일 2 vCPU VM에서 컨테이너만 분리한다고 CPU 용량이 늘어나지는 않습니다. 로컬 2 vCPU/4 GiB 실험과
운영 VM 2 vCPU/약 8 GiB 관측은 별개입니다. 운영 관측에서 이미 CPU 여유가 작았으므로 500건·동시 부하 검증 전에는
모델 2개·worker 2개 동작을 운영 기본으로 켜지 않습니다. MySQL·OS에 필요한 여유도 따로 확인합니다.
