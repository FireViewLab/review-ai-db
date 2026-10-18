# Text Batch / Worker / Reasons 검증 — 2026-10-18

기준 main `33918a0`(RTI 정책 rti-v0.1 merge 이후)에서 작업했다.
AI 기준은 review-ai-new `74b923c5e3c61116f312c4aeb6cbc907e9a7f558`의 parity 자산이다.
운영 서버 배포, main 변경, 과거 결과 수정, 모델 재학습은 수행하지 않았다.

## 조사 및 구현

기존 Text는 리뷰별 tokenizer/forward 호출, `lru_cache(maxsize=1)` 모델 캐시와 공통 RLock을 사용했다.
새 `predict_text_scores()`는 기본 8, 상한 32의 실제 tensor 배치를 수행한다. tokenizer의 동적 padding,
truncation/max_length 및 attention_mask를 유지하며 입력 인덱스로 결과 위치를 복원한다.
단건 공개 함수는 그대로이며 두 공개 함수가 같은 캐시/락을 사용한다. 실패 chunk만 -1로 반환하며
성공 chunk를 지우거나 단건 재추론을 시도하지 않는다.

서비스의 Text 호출만 배치로 바꾸었다. Network는 상품 요청 전체를 한 번 비교한다.
모델·T=2 변환·behavior/network·RTI 가중치·등급·v0.5 serializer·모델/정책 버전은 변경하지 않았다.

Worker는 프로세스이며 각자 모델/캐시/락/task 집합을 가진다. threadpool은 같은 프로세스 안에서
blocking 분석을 수행해 SSE loop의 heartbeat를 허용한다. Worker 2, OMP/MKL 각각 1을 설정 가능하게
했고 정상 종료 유예는 180초다. DB atomic idempotency를 유지하며 자동 RUNNING 회수는 추가하지 않았다.

기존 analyzer 이유 코드를 내부 불변 컨텍스트에 복사한 뒤 기존 Groq를 적용하고,
원본 reasons가 []였던 신규 결과에만 `neutral-summary-v1` 중립 안내를 붙인다.
외부 필드는 여전히 reasons: list[str]. 실제 관측 근거나 RTI 기여도를 새로 추측하지 않는다.
원본 코드의 영구 감사 테이블은 추가하지 않았다. 과거 DONE replay는 설명도 재생성하지 않는다.

## 테스트

- Windows + 실제 격리 MySQL: **544 passed**, 10.11초, 기존 deprecation warning 2건.
- Docker Linux + 실제 격리 MySQL: **544 passed**, 11.54초, 기존 TestClient warning 1건.
- 실패/skip 없음. 기존 Groq, 인증, 500/501, DB 실패, heartbeat, 순서, replay 테스트 포함.
- 신규 실제 tensor mock, mixed invalid 입력, 1/8/16/32 크기, 단건/배치 혼합 락,
  모델 첫 로딩 1회, malformed chunk, 전체 Network 범위, 중립 설명/0/-1 의미 검증 포함.
- 정상 종료의 분석 저장 대기와 재시작 후 잔류 RUNNING 보존/409를 검증했다.
- Docker build 및 Compose config 성공. 기본 후보 2와 override 1 모두 실행 가능.
- 초기 Linux 수집 오류는 이미지에서 의도적으로 제외한 레거시 ai 모듈 때문이었다.
  검증 override에만 읽기 전용 테스트 소스를 제공해 해결했다. 운영 이미지 범위는 확대하지 않았다.
- 최초 벤치 로그 파서의 동시 출력 오류를 수정하고 회귀 테스트를 추가한 뒤 아래 측정을 다시 실행했다.

## 실제 모델 Golden parity

[전체 CPU 보고서](ptext-batch-parity-20261018.json).
Python 3.12.15 / torch 2.8.0+cpu / transformers 4.57.6 / CPU intra-op 1 / WSL2 Linux에서 실행했다.
inference_config의 threshold=0.5, max_length=256을 사용했다. 모델은 검증 프로세스에서 1회 로딩했다.

- Golden 18건의 label/score/unavailable 정확히 일치.
- batch 1/8/16/32 각 2회, 단건 대비 label/score/unavailable/order 정확히 일치.
- 전체 v0.5 raw 결과/RTI/level도 이전 단건 경로와 정확히 일치.
- 중복 리뷰, 긴 입력의 truncation 및 unmasked token ID 일치.
- 단건→배치 확률 최대 절대 차이: **1.5497207641601562e-6**.
- Golden→현재 단건 확률 최대 차이: **1.6689300537109375e-6**.
- 허용 오차로 점수/label 차이를 묵인하지 않았다. 확률 비교의 관측 범위 후보는 해당 환경의 위 최댓값이며
  다른 하드웨어/라이브러리나 threshold에 더 가까운 입력은 다시 측정해야 한다.
- fixture/Golden Git blob은 원본과 동일하다: cases `80b6e5661a9feabfb6d6582a1b09900e07c9ee04`,
  Golden `673babdea503a68933b644306281f50d3bd82d48`. 기준 자료를 재생성/보정하지 않았다.

이 길이 혼합 fixture의 warm 단건은 약 1.40초였지만 batch 8은 약 4.49초,
16/32는 약 7.30~7.52초였다. 긴 리뷰 하나가 배치 전체 padding 길이를 늘린 실제 사례다.
일괄적인 성능 개선을 주장하지 않는다. 길이별 묶음/패딩 예산 최적화는 별도 parity 검증이 필요한 후속 작업이다.

## Worker 성능 실측

전용 Docker 프로젝트 `review-ai-runtime-check`, CPU quota 2 / 메모리 한도 4 GiB,
외부 모델 read-only, MySQL 8 tmpfs, Groq OFF. 운영 API/DB는 호출하지 않았다.
각 구성 cold 2요청 뒤 Worker 모두 모델 로딩 확인, warm 2동시 요청 × 3회(총 192리뷰)를 측정했다.
모든 구성에 같은 일반 길이 합성 리뷰 32건을 사용했다. 요청마다 상품 전체 Network 비교를 유지했다.
warm wall에는 SSE/DB 검증 HTTP 왕복도 포함되므로 순수 모델 throughput과는 다르다.

| Batch | Worker | warm wall(s) | reviews/s | 평균 요청(s) | p95(s) | sampled CPU % | Docker 메모리 MiB | 프로세스 RSS 합 MiB | 기준선 대비 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 (실제 단건) | 1 | 14.456 | 13.28 | 4.369 | 4.837 | 118.35 | 262.4 | 687.8 | 1.00× |
| 8 | 1 | 11.383 | 16.87 | 2.861 | 3.863 | 112.54 | 294.3 | 725.4 | 1.27× |
| 16 | 1 | 10.727 | 17.90 | 2.687 | 3.570 | 105.55 | 341.7 | 760.0 | 1.35× |
| 8 | 2 | 6.304 | 30.46 | 2.072 | 2.108 | 200.28 | 583.5 | 1446.3 | 2.29× |
| 16 | 2 | 6.374 | 30.12 | 2.048 | 2.117 | 195.45 | 617.8 | 1469.1 | 2.27× |
| 32 | 1 | 10.886 | 17.64 | 2.725 | 3.648 | 102.89 | 377.8 | 803.4 | 1.33× |
| 32 | 2 | 6.363 | 30.17 | 2.095 | 2.160 | 199.26 | 686.7 | 1530.4 | 2.27× |

기준선 재측정은 **13.09 reviews/s**였다. 소수의 합성 요청과 공유 데스크톱/WSL 환경 결과이며
58만 리뷰 전체의 완료 시간이나 운영 p95로 외삽하지 않는다. p95는 warm 요청 6개의 기술 통계다.
CPU는 2 core 합산이므로 약 200%가 관측된다. 메모리는 표본 최대치다.
Docker stats의 working set과 프로세스 RSS 합은 서로 다른 지표이며, RSS 합에는 공유 페이지 중복이 있다.
최악 길이 500건 × 2 요청 동시 실행의 최대 메모리를 측정한 것은 아니다.

각 Worker의 모델 최초 로딩은 약 2.32~2.66초, Worker별 1회였다.
Worker 2의 세 구성 모두 서로 다른 PID의 분석 시작/종료 구간 중첩을 확인했다.
원시 보고서는 `batch{1,8,16,32}-worker{1,2}-20261018.json` 중 위 표의 7개 파일과
`batch1-worker1-20261018-control.json`에 있다. 각 phase의 전체 시간/표본/PID/로딩도 포함한다.

## API·SSE·저장·재시도

- 7개 구성 모두 결과/점수 동일, DB 저장값 동일, SSE 성공 총 70건/실패 0건.
  멱등 키 충돌의 예상 409는 SSE 실패로 집계하지 않았다.
- 각 구성에서 동일 key 동시 요청은 200 + 409, analysis_start 정확히 1회.
- DONE replay는 meta/result/done만 보내고 새 분석 로그가 발생하지 않았다.
- 여러 차례 API 컨테이너 재생성 후 최초 구성의 DONE 결과도 0.0408초에 재분석 없이 replay했다.
- 실제 모델 **500건** 요청: 34.7101초, meta 1 / progress 1 / heartbeat 2 / result 500 / done 1.
  저장 결과·순서·done count 일치. 운영 74.2초 기록과 입력/환경이 다르므로 직접 속도 비교하지 않는다.
- [Batch 8 통합 smoke](batch-runtime-smoke-20261018.json): JSON/공식 SSE/실험 SSE 및 MySQL 결과 일치,
  cache miss 1, 신규 빈 원본 근거의 중립 안내만 최종 API에 반영. raw analyzer []는 유지한다.
- 기존 정책/모델/계약 버전 및 idempotency 저장 스키마 변경 없음.
- 계측 override를 제거한 실제 `main:app` 이미지에서도 Worker 2 startup 완료, /health 200,
  Docker healthy, 공식 JSON/SSE OpenAPI 존재 및 501건 422를 확인했다.
  benchmark 전용 경로는 없었다. 최초 기동 중 curl empty reply는 기존 retry 방식으로 회복했다.

## 운영 후보와 남은 작업

초기 후보는 `UVICORN_WORKERS=2`, `OMP_NUM_THREADS=1`, `MKL_NUM_THREADS=1`, `PTEXT_BATCH_SIZE=8`.
이번 일반 길이 표본에서는 16/32가 유의미하게 빠르지 않으면서 메모리를 더 사용했다.
기존 .env의 스레드 2 등 명시값은 새 기본값보다 우선하므로 운영 설정을 별도로 확인해야 한다.
운영 .env/secret은 이번에 수정하지 않았다.

실제 길이 분포의 비식별 샘플과 최대 길이/동시 500건 메모리 검증이 필요하다.
length bucketing은 배치 구성과 수치 결과를 다시 검증한 뒤 도입한다.
강제 종료/OOM 후 RUNNING은 자동 복구되지 않는다. lease/owner/fencing 설계 없이 시간으로 지우지 않는다.
구조화된 설명 근거/기여도는 AI 담당자 규격을 기다린다. 설명 버전은 내부 값이라 기존 저장 행별 추적은 미구현이다.
GPU는 이번에 실행하지 않았다. 기존 device 선택과 batched tensor 이동 경로를 유지했다.
기존 미추적 `scripts/validate_review_ai_handoff.py`의 과거 단건 service mock은 이번 변경 대상에서 제외했다.

기존 사용자 미커밋 문서 수정과 무관한 파일은 보존한다. 테스트용 컨테이너/임시 데이터만 정리한다.
