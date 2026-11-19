# GCP v1/v2 병행 운영 준비 및 롤백 (실행 승인 전)

## 현재 범위와 승인

현재 저장소의 GCP workflow는 `review-ai-gcp-prod` (`asia-northeast3-a`)에 IAP/OS Login으로 접속해
`/home/reviewadmin/review-ai-db`에서 **기본 Compose**를 사용합니다. workflow는 수정하지 않았습니다.
2026-11-17 실제 운영 관측과 11-18 v3.1 B 파일 배치 상태는 아래와 같습니다. 디스크 여유, live `.env` 전체,
기존 v1 모델의 호스트 절대 경로는 이번 검토에서 확인하지 않았습니다.
기본 Compose 컨테이너 내부 v1 경로는 `/models/ptext-koelectra-v1-2epoch-20260929`이고,
호스트 경로는 운영 측 `PTEXT_MODEL_HOST_PATH` 설정을 확인하고 그대로 유지해야 합니다.

**배포 보류:** 전달된 v3.1 B `package_manifest.json`은 `PTEXT_V3_1_B_EXPERIMENTAL_NOT_APPROVED_FOR_PRODUCTION`으로 표시합니다.
독립적인 사람 라벨 평가/모델 담당자의 운영 승인이 아직 필요합니다. 로컬 합성 추론·SSE 테스트 결과는
모델의 실제 정확도 또는 운영 전환 승인을 뜻하지 않습니다. 운영 호스트에 모델 파일만 배치해도 v2가 활성화되지는 않습니다.

## 모델 파일 배치 완료 — 2026-11-18 (사용자 확인)

- VM: `review-ai-gcp-prod`
- v3.1 B 호스트 디렉터리: `/opt/review-ai-models/ptext-koelectra-v3.1-b`
- 가중치·`config.json`·`inference_config.json`·토크나이저 포함 7개 파일
- 가중치 SHA-256: `012b65174cdbc2e1fb36d75519438043bebd4317a69ea9997325b66bb436474b`
- 기존 v1 호스트 경로와 운영 컨테이너·DB는 변경되지 않았습니다. 이번 파일 배치는 v2 활성화를 의미하지 않습니다.

이 상태는 사용자가 확인한 값으로 기록했습니다. 이 검토에서는 운영 VM에 접속하거나 파일 무결성·권한을 재확인하지 않았습니다.

### PR 병합 안전 경계

`main` 병합으로 실행되는 `AI CI`는 테스트와 러너 내부의 기본 Compose 검증·이미지 빌드만 수행합니다.
`GCP AI Manual Deploy`는 `main`의 명시적 `workflow_dispatch` 전용입니다. 병합 자체는 운영 VM의 Git pull,
컨테이너 재생성, DB 마이그레이션 또는 v2 활성화를 트리거하지 않습니다.

기본 `docker-compose.yml`은 기존 v1 마운트·MySQL 볼륨·v1 Groq 설정을 유지합니다.
v2는 명시적으로 `docker-compose.v2.yml`과 `--profile v2`를 사용하는 **별도 프로젝트**입니다.
두 Compose 파일을 함께 지정하지 마세요. 기존 GCP workflow는 v2를 시작·중지·업데이트하지 않습니다.

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
| 컨테이너 경로 | 기존 `PTEXT_MODEL_PATH` 그대로 | `/models/ptext-koelectra-v3.1-b` (전용 서비스), `PTEXT_V31B_MODEL_PATH` |

각 폴더에는 `config.json`, `inference_config.json`, 가중치와 tokenizer/config/vocab/special tokens가 필요합니다.
v3.1 B의 raw probability threshold는 0.5, score temperature는 2, max length는 256입니다.
버전 메타데이터의 `ptext-koelectra-v3.1-b-seed20261115`는 위 manifest의 체크포인트를 뜻합니다.

## 승인 후에만 실행할 병행 배포 절차

1. 운영팀과 v3.1 B 체크포인트 출처·승인을 확인합니다. 이미 배치된 `/opt/review-ai-models/ptext-koelectra-v3.1-b`의
   SHA-256과 UID 10001 읽기 권한을 확인합니다. v1 모델 경로는 수정하지 마세요. 리소스·보존 정책 예산(이미지 및 파일 볼륨)을 확인합니다.
2. v1의 Compose 프로젝트/환경은 그대로 두고, v2용 **별도 비공개** 환경 파일(예: `.env.v2`)에
   `V2_INTERNAL_TOKEN`을 필수 설정합니다. 기본 모델 경로를 변경할 때만 `PTEXT_V31B_MODEL_HOST_PATH`를 변경합니다.
   `ENABLE_V2_GROQ_SUMMARY=0`에서 시작하세요. Groq가 따로 승인되면 `V2_GROQ_API_KEY_PRIMARY`를 설정합니다.
   v1의 DB/토큰/추론 설정을 복사할 필요는 없습니다.
3. 아래는 **운영 승인 이후에만** 사용할 명령입니다. 기본 Compose와 함께 `-f`로 합치지 말고
   v1 프로젝트 이름도 재사용하지 마세요. `-p review-ai-v2`는 셸의 `COMPOSE_PROJECT_NAME`이 v2의
   프로젝트 이름을 덮어쓰지 않도록 고정합니다. 이 구성은 v1의 MySQL 서비스·네트워크·볼륨을 공유하지 않습니다.

   ```bash
   docker compose -p review-ai-v2 --env-file .env.v2 -f docker-compose.v2.yml --profile v2 config --quiet
   docker compose -p review-ai-v2 --env-file .env.v2 -f docker-compose.v2.yml --profile v2 up -d --build ai-v2
   curl --fail --retry 12 --retry-all-errors http://127.0.0.1:8001/health
   ```

4. v2는 호스트 `127.0.0.1:${V2_HOST_PORT:-8001}`에만 게시합니다. Data 서버가 접속하는 승인된 프록시/내부 경로에서
   **`POST /api/v2/data/analyze/stream`만 v2 포트로** 별도 라우팅하세요. v1 경로를 바꾸지 않습니다.
   프록시가 컨테이너인 경우 호스트 루프백 접근 방식을 별도로 검토하고, 포트를 공인 인터페이스에 열지 마세요.
   SSE 버퍼링·응답 읽기 타임아웃을 검증하고 Data 팀과 인증/계약/적재를 시험합니다.
5. `/health`는 HTTP 기동 확인입니다. v3.1 B 모델은 지연 로딩이므로 승인된 합성 리뷰로 `meta` 버전,
   유효 점수, `done`, 재전송을 별도 검사합니다. v1의 기존 JSON/SSE도 확인하고, 500건·동시 트래픽·응답 지연,
   CPU·RAM·MySQL 여유를 스테이징에서 먼저 측정하세요. v2 기본은 Worker 1 / OMP 1 / CPU 상한 1 / RAM 상한 2 GiB입니다.
   제한은 v1 용량을 보장하지 않습니다. 같은 VM에서 Worker를 늘리면 모델 적재와 자원 경합도 늘 수 있습니다.

## 파일 저장 및 복구

- v2는 `v2-job-data` named volume 아래 `/service/data/v2-jobs`에서 상태를 관리합니다. 키와 입력의 해시, UUID,
  상태/버전, DONE 재전송용 결과만 기록합니다. 요청 본문·공유 토큰·평문 멱등 키를 저장하지 않습니다.
  결과의 리뷰 식별자와 설명은 민감할 수 있으므로 접근과 백업을 제한하세요.
- 파일 fsync → 원자적 rename → 디렉터리 fsync 후에만 새 SSE 결과를 전송합니다. 커밋 실패 시 `result`/`done`은 보내지 않습니다.
  작업별 파일 잠금을 사용하며, 동일 Linux 로컬 볼륨을 공유하는 **한 호스트 내**의 여러 Uvicorn Worker만 지원합니다.
  다른 VM/복제 서비스, NFS/SMB, 볼륨 유실, 호스트 저장장치 장애까지 멱등성을 보장하지 않습니다.
- 완료 상태는 **같은 프로젝트와 볼륨**을 유지하면 컨테이너 재생성 후에도 동일 결과로 재생합니다. 프로세스 종료로
  RUNNING 잠금이 사라진 작업은 클라이언트가 같은 키·입력·모델/정책 버전으로 재요청할 때 같은 UUID로 재계산합니다.
  분석의 exactly-once나 Data DB 적재 완료를 보장하지 않습니다.
- AI MySQL v2 테이블 또는 마이그레이션은 필요하지 않습니다. 이전 시험 배포에서 테이블이 존재해도 이번 변경은
  조회·이관·삭제하지 않습니다. 기존 DB의 멱등 키·완료 결과는 파일 볼륨으로 자동 이전되지 않습니다. 필요한 경우 트래픽
  전환 전 기존 작업을 끝까지 처리하고 Data DB 적재를 확인한 뒤 새 작업을 보내세요.
- 캐시는 자동 만료되지 않습니다. 디스크 사용량을 감시하고 재전송 보존 기간을 Data 팀과 합의하세요.
  실행 중에는 잠금 파일을 포함한 작업 파일을 삭제하지 마세요. `down -v`, 볼륨 초기화는 사용하지 않습니다.

## 롤백

v2 라우팅/신규 데이터 작업부터 중지하고, 이미 열린 스트림을 정상 종료시킨 후 `docker compose -p review-ai-v2 --env-file .env.v2 -f docker-compose.v2.yml --profile v2 stop ai-v2`를 사용합니다.
v1의 Compose, MySQL, 기존 트래픽은 그대로 두고 v2 모델과 재전송 볼륨을 보존합니다. v2 결과를 같은 상품의 v1 RTI로 바꾸지 않습니다.
현재 검증은 로컬이며 GCP·라우팅·Data DB 적재는 실제 변경하지 않았습니다.
