# PR 제목

feat: Text 배치 추론·Worker 2 설정·빈 reasons 중립 안내

## 목적

리뷰별 KoELECTRA 호출을 실제 tensor Batch Inference로 바꾸고 두 Worker의 요청 병렬 처리를 지원합니다.
점수 근거가 빈 결과에는 확정 점수와 신호 가용성을 설명하는 한국어 중립 안내를 제공합니다.

## 구현

- 기존 predict_text_score 보존, predict_text_scores 추가(기본 8 / 상한 32).
- 모델/tokenizer/config/truncation/attention_mask 및 worker별 cache/RLock 유지.
- Text 호출만 배치로 변경. Network는 상품 리뷰 집합 전체 비교 유지.
- UVICORN_WORKERS 설정(기본 후보 2), OMP/MKL 각 1, 종료 유예 180초.
- raw reasons를 내부 불변 context로 보존하고 원본 []인 신규 결과만 중립 안내.
- 기존 이유 코드/Groq prompt·budget·failover는 유지.
- contract v0.5, 모델 버전, rti-v0.1, 점수/level/식별자 및 API 필드 변경 없음.
- 기존 MySQL 저장/DB idempotency/DONE replay 유지. 과거 결과 자동 수정 없음.

## 검증

- Windows / Docker Linux: 각각 전체 pytest **544 passed**, 실제 격리 MySQL 포함, skip 없음.
- AI 팀 commit 74b923c의 Golden 18건 보존. Batch 1/8/16/32 각 2회에서 label/score/unavailable/order/RTI/level exact parity.
- 단건↔배치 확률 최대 절대 차이 1.5497207641601562e-6 측정. 점수/label 불일치를 tolerance로 허용하지 않음.
- Docker build/config/실제 main:app Worker 2 startup 및 health/501건 422 검증.
- JSON/공식 SSE/실험 SSE/MySQL 결과 일치, worker별 모델 1회 로딩 확인.
- 서로 다른 두 PID의 실제 분석 실행 구간 중첩 확인.
- 동일 key 동시 요청은 분석 1회 + 409. 컨테이너 재생성 후 DONE replay 재분석 없음.
- 실제 500건 로컬 SSE: 34.71초, heartbeat 2 / result 500 / done 1, 저장 결과 일치.

## 성능 (2 CPU quota / 4 GiB, 일반 길이 합성 리뷰 32건 × 2동시 요청)

| 구성 | warm reviews/s | 평균 요청(s) | 기준선 대비 |
| --- | --- | --- | --- |
| 기존 단건 / Worker 1 | 13.28 | 4.369 | 1.00× |
| Batch 8 / Worker 1 | 16.87 | 2.861 | 1.27× |
| Batch 16 / Worker 1 | 17.90 | 2.687 | 1.35× |
| Batch 8 / Worker 2 | 30.46 | 2.072 | 2.29× |
| Batch 16 / Worker 2 | 30.12 | 2.048 | 2.27× |
| Batch 32 / Worker 1 | 17.64 | 2.725 | 1.33× |
| Batch 32 / Worker 2 | 30.17 | 2.095 | 2.27× |

표본은 구성별 warm 요청 6개입니다. 운영 데이터 전체 성능이나 p95로 외삽하지 않습니다.
긴 입력 혼합 Golden에서는 padding 비용 때문에 batch가 단건보다 느렸습니다.
Batch 8 / Worker 2의 sampled Docker memory 약 583.5 MiB, 프로세스 RSS 합 약 1446.3 MiB였습니다.
두 지표는 다르며 최대 길이 500건 × 2의 worst-case 측정은 아닙니다.

## 운영 적용 전

- 후보: UVICORN_WORKERS=2 / OMP_NUM_THREADS=1 / MKL_NUM_THREADS=1 / PTEXT_BATCH_SIZE=8.
- 기존 운영 .env의 명시값이 기본값보다 우선하므로 별도 설정 확인 필요.
- 실제 길이 분포와 최대 길이 동시 요청의 메모리 검증 필요.
- 강제 종료/OOM 후 잔류 RUNNING은 자동 회수하지 않음. lease/owner/fencing 기반 복구는 후속 작업.
- 중립 안내는 구체적인 위험 판정이 아니며 과거 DONE reasons=[]는 그대로 replay될 수 있음.
- AI 담당자의 구조화된 설명 규격 및 설명 버전 영속 추적은 후속 협의.
- 실제 Groq/Data 외부 호출, 운영 secret 수정, Azure 배포 없음.
- benchmark override는 운영에서 사용하지 않음.

전체 실측·한계·재현 자료: docs/validation/batch-workers-reasons-20261018.md

PR 생성/merge는 사용자가 직접 진행합니다. main push 시 기존 Actions 배포가 동작할 수 있으므로
운영 설정과 데이터 파트 확인 후 병합해 주세요.
