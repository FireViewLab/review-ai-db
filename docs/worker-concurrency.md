<!-- Worker/스레드 실행 설정과 SSE 작업 종료·DB 멱등성의 운영상 한계를 설명한다. -->
# Worker 병렬 처리와 작업 종료

## 실행 설정

Docker/Compose의 운영 후보는 다음과 같다. 실제 배포 사양에서 Worker 1과 2의 처리량·RSS·CPU를
비교한 뒤 적용한다. [2026-10-18 로컬 실측](validation/batch-workers-reasons-20261018.md)에서는
일반 길이 합성 리뷰 기준 Batch 8 / Worker 2가 단건 / Worker 1 대비 2.29배 처리량을 보였다.
긴 입력 혼합에서는 padding 때문에 배치가 느려질 수 있으며 운영 데이터 전체의 성능 보장은 아니다.

```dotenv
UVICORN_WORKERS=2
OMP_NUM_THREADS=1
MKL_NUM_THREADS=1
PTEXT_BATCH_SIZE=8
```

Uvicorn CLI의 기존 `UVICORN_WORKERS` 환경변수를 사용한다. Docker CMD는 shell을 거치지 않는
JSON 형식을 유지하며 Worker 수를 고정하지 않는다. Compose는 변수 미설정 시 2를 전달한다.
`UVICORN_WORKERS=1`로 기존 단일 Worker 구성을 실행할 수 있다. Docker 밖에서 실행하는 Uvicorn은
이 환경변수가 없으면 기본 Worker 1을 사용하며, 명시한 `--workers 1` 옵션이 환경변수보다 우선한다.
`uvicorn --env-file`로 읽은 파일은 Uvicorn 자체 옵션을 설정하는 수단이 아니므로, 직접 실행할 때는
프로세스 환경변수나 `--workers` 옵션을 사용한다.

Worker는 별도의 Python 프로세스다. 모델 캐시, 추론 RLock, event loop, threadpool 및 진행 task 집합은
Worker마다 독립적이다. 모델은 각 Worker에서 첫 분석 시 lazy load되므로 두 Worker가 모두 분석하면
모델과 라이브러리 메모리도 각각 사용한다. 한 Worker의 RLock이 다른 Worker의 추론을 잠그지는 않는다.
`/health`는 모델을 로드하지 않으며 응답 성공은 모든 Worker의 모델 준비 상태를 뜻하지 않는다.

Python threadpool은 같은 Worker 안에서 blocking 분석·DB 작업을 실행해 SSE event loop가 heartbeat를
전송할 수 있게 한다. OMP/MKL 스레드 수는 모델의 CPU 연산 병렬성 설정이다. Worker 수와 서로 다른
설정이며 `2 Worker × CPU thread 1`부터 CPU 경합을 측정한다. Batch Inference는 여러 리뷰를 하나의
모델 tensor로 처리하는 별도 최적화다. `PTEXT_BATCH_SIZE`는 P_text에만 적용하며 P_network는 항상
상품의 요청 리뷰 집합 전체를 비교한다. Worker 수를 늘려도 상품 리뷰 집합을 나누지 않는다.

## SSE·MySQL·Idempotency

공식 POST SSE는 분석 task를 Worker의 `app.state.analysis_tasks`에 등록한다. 클라이언트가 연결을
끊어도 이미 시작한 분석을 취소하지 않고 DB 저장까지 계속한다. 결과가 DB에 저장된 뒤에만
`result`와 `done`을 전송한다. Worker 수와 무관하게 기존 인증, request/job ID 및 이벤트 계약을 유지한다.

MySQL `ai_analysis_idempotency.key_hash`의 고유 제약과 job/key 동시 트랜잭션이 Worker 간 중복 실행을
차단한다. 각 DB 작업은 자체 연결을 열고 닫으며 전역 메모리 dict가 중복 판정의 기준이 아니다.
기존 key와 같은 payload의 DONE 재요청은 저장된 결과·버전을 replay하며 모델과 설명을 다시 생성하지 않는다.
RUNNING은 `409 IDEMPOTENCY_IN_PROGRESS`, 다른 payload는 `409 IDEMPOTENCY_KEY_REUSED`,
FAILED는 `409 IDEMPOTENCY_FAILED`를 유지한다. Worker를 늘리거나 재시작해도 이 기록을 삭제하지 않는다.

## 정상 종료와 잔류 RUNNING

Uvicorn은 정상 종료 시 진행 중 요청을 기다리며, 앱 lifespan은 등록된 공식 SSE 분석 task를 기다린다.
Compose의 `stop_grace_period: 180s`는 Docker가 SIGTERM 후 SIGKILL까지 기다리는 시간이다.
기존 기본 유예 10초보다 긴 작업이 저장을 마칠 여유를 주지만, 최대 처리 시간이나 무손실을 보장하지 않는다.
동시 요청이 추론 락을 기다리거나 긴 모델/Groq/DB 작업을 수행하면 180초를 넘을 수 있다.

강제 종료, OOM, VM 중단 또는 종료 유예 만료 직후에는 RUNNING이 DB에 남을 수 있다. 현재 스키마에는
실행 owner/lease/worker heartbeat가 없어 작업이 실제 실행 중인지 DB 시각만으로 판단할 수 없다.
같은 key 재시도는 계속 409이며, 자동 재실행·자동 삭제·자동 FAILED 전환을 추가하지 않는다.
기존 key를 바꾸어 무조건 재요청하는 것도 동시 재분석을 만들 수 있으므로 자동 복구 방법이 아니다.

계획된 배포에서는 Data 요청 유입을 잠시 멈추고 정상 종료 시간을 확보한다. 배포 후에는 미완료 job의
상태와 실제 실행 프로세스를 대조하고 운영자가 복구 대상을 확인한다. 기존 입력·결과·idempotency
기록을 보존한 상태에서 Data 팀과 재처리 방법을 합의해야 한다. 이번 변경은 복구용 DB UPDATE나
데이터 정리 작업을 실행하지 않는다.

실험용 역방향 collection SSE는 자체 task에 실패 소비 callback을 붙이지만 공식 SSE task 집합에는
등록하지 않는다. 연결 종료 후 서버까지 종료하는 상황의 작업 보장은 공식 SSE와 다르다.
이번 변경은 실험 API 수명주기를 재설계하지 않는다.

안전한 자동 복구는 후속 작업이다. 실행 owner, 만료 lease, 주기적 실행 heartbeat, 경쟁 복구의 원자적
claim 및 이전 실행자의 늦은 저장을 막는 fencing이 함께 필요하다. 단순한 시간 기준 RUNNING 삭제는
진행 중 작업의 중복 실행이나 결과 충돌을 일으킬 수 있다.

## 검증 기준

Worker 1/2 각각 Batch 1/8/16(가능하면 32), 동일 synthetic 리뷰 및 외부 모델을 사용해 격리 DB에서
비교한다. 최초 모델 로딩과 warm 요청을 구분하고 전체 시간, reviews/s, 평균/p95, CPU, worker RSS,
컨테이너 peak memory, SSE done 성공률을 측정한다. 두 Worker의 동시 처리는 검증 전용 PID와
분석 시작·종료 구간을 기록해 실제 중첩을 확인한다. 공식 응답 스키마에 검증용 PID를 추가하지 않는다.
운영 서버·운영 DB에 부하를 보내거나 기존 영속 볼륨을 초기화하지 않는다.

검증 전용 `docker-compose.benchmark.yml`을 기본 Compose와 validation override 뒤에 적용하면
읽기 전용 테스트/스크립트 마운트와 PID 관측용 factory를 사용한다. 이 override는 운영에 사용하지 않는다.
일반 이미지에는 benchmark 스크립트/경로를 포함하지 않는다. 측정기는 loopback 및 전용 DB를 확인한다.

```powershell
# .env를 읽지 않는 격리 검증용 예. 외부 모델 호스트 경로는 별도로 설정한다.
$env:DB_PASSWORD="synthetic-local-batch-validation"
$env:UVICORN_WORKERS="2"
$env:PTEXT_BATCH_SIZE="8"
$env:BENCHMARK_USE_SINGLE="0"
docker compose --env-file .env.example -p review-ai-runtime-check -f docker-compose.yml -f docker-compose.validation.yml -f docker-compose.benchmark.yml up -d
docker port review-ai-runtime-check-api 8000
python scripts/benchmark_batch_workers.py --allow-disposable --base-url http://127.0.0.1:PORT --workers 2 --batch-size 8 --reviews 32 --rounds 3 --output benchmark-report.json
```

각 구성 전에 검증용 ai 컨테이너만 재생성한다. 진짜 이전 단건 기준선은 batch=1, worker=1,
`BENCHMARK_USE_SINGLE=1`로 측정한다. 일반 배치 경로는 `BENCHMARK_USE_SINGLE=0`을 사용한다.
벤치 결과의 p95는 6개 warm 요청의 기술 통계이며 운영 p95 예측값으로 사용하지 않는다.

`docker-compose.benchmark.yml`은 검증 override 다음에만 추가하는 로컬 전용 설정이다.
scripts 및 parity fixture를 읽기 전용 마운트하고, 검증 전용 앱 factory에서 PID·모델 로딩·분석 구간을
관측한다. 검증용 상태/저장 조회 경로는 이 factory에만 있으며 production 이미지와 앱에는 추가하지 않는다.
Groq는 강제로 OFF이고 runtime_validation DB 및 전용 Compose 프로젝트를 검사한다.

각 구성별로 API 컨테이너만 새로 만들어 cold 모델 캐시에서 시작한다. 기존 MySQL 컨테이너/볼륨에는
적용하지 않는다. 다음은 이미 기동한 검증용 HTTP 포트에 대한 측정 예시다.

```sh
python scripts/benchmark_batch_workers.py --allow-disposable \
  --base-url http://127.0.0.1:LOCAL_QA_PORT --workers 2 --batch-size 8 \
  --reviews 32 --rounds 2 --concurrency 2 --output artifacts/batch8-worker2.json
```

Worker/Batch 구성 변경과 API `--force-recreate`는 검증 Compose에서 별도로 수행한다.
Batch 1 기준선에서는 `BENCHMARK_USE_SINGLE=1`을 지정한다. 검증 factory에서만 기존 단건 함수를
리뷰마다 호출하므로 batch tensor 크기 1을 단건 기준선으로 잘못 취급하지 않는다. 다른 Batch에서는 0이다.
두 Worker가 모두 모델을 로드한 뒤 warm 시간을 측정하며, 추가 준비 요청은 warm 통계에서 제외한다.
요청 결과는 SSE 수신 후 DB 결과와 대조하고 DONE replay가 분석 호출을 추가하지 않는지도 확인한다.
warm 통계와 분리한 동일 key 동시 요청도 확인한다. 200과 RUNNING 409 또는 빠른 완료 후 200 replay를
허용하되 동일 job과 분석 시작 1회를 검증한다. 예상된 409는 SSE 실패 횟수에 포함하지 않는다.

JSON의 CPU/컨테이너 메모리는 Docker 통계 표본의 최대값이며 kernel이 기록한 절대 peak가 아니다.
프로세스 RSS/HWM과 모델 로딩 시간도 함께 기록한다. 평균/p95는 작은 synthetic 표본의 기술 통계이므로
운영 58만 건 처리 용량이나 실제 리뷰 분포에 대한 추정으로 사용하지 않는다.
