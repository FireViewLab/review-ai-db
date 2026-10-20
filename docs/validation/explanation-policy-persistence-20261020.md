# PR #34 후속 검증 — 설명 정책 버전 영속화 / README 정정

2026-10-20, 기존 `codex/batch-workers-reasons`의 `8af524239c81c2f29c5621696bd891a7b6d7e766`에서 이어서 작업했다.
새 브랜치·새 PR·main 변경·Azure 배포·운영 DB 마이그레이션은 수행하지 않았다.
기존 작업 폴더의 무관한 변경과 파일은 이번 커밋에서 제외한다.

## 변경 전후

- 이전: `neutral-summary-v1`은 설명 컨텍스트에만 존재하여 저장된 결과로 적용 정책을 식별하기 어려웠다.
- 이후: core에서 단일 정의하며 신규 분석의 최종 결과·DONE·설명 버전을 하나의 UPDATE/트랜잭션에 저장한다.
- `ai_analysis_jobs.explanation_policy_version`은 nullable `VARCHAR(64) DEFAULT NULL`이다.
  과거 DONE/RUNNING/FAILED 행, 기존 결과 JSON, 멱등성 레코드는 수정하지 않는다.
- 이 값은 원본 이유 코드·선택적 Groq·중립 안내를 포함한 **전체 설명 파이프라인의 작업 단위 버전**이다.
  한 작업의 리뷰마다 경로가 달라도 같은 버전을 사용하며 리뷰별 경로나 Groq 성공 여부를 뜻하지 않는다.
- `contract_version=v0.5`, `model_version=ptext-koelectra-v1-2epoch-20260929`,
  `policy_version=rti-v0.1`과 JSON/SSE 필드·점수·reasons 문구는 변경하지 않았다.
- MySQL startup은 기존 스키마를 자동 ALTER하지 않는다. 명시적 도구로 먼저 업그레이드한다.
  startup CREATE/검사와 migration ALTER가 DB별 동일 named lock을 사용한다.
- README PowerShell/Linux 실행 예시와 기본값을 OMP/MKL 각각 1로 통일하고,
  Worker(프로세스) / OMP·MKL(프로세스별 연산 스레드) / Batch Size(모델 입력 리뷰 수)를 구분했다.

## 자동 테스트

| 환경 | 실제 실행 결과 |
| --- | --- |
| Windows Python + 격리 MySQL 8 | 최종 전체 **584 passed**, skip 없음, 13.60초, 기존 deprecation warning 2건 |
| Docker Linux Python 3.12 + 격리 MySQL 8 | 최종 전체 **584 passed**, skip 없음, 12.09초, 기존 TestClient warning 1건 |

두 실행 모두 `RUN_MYSQL_TESTS=1`로 실제 MySQL 테스트를 활성화했다. Groq/Data 외부 네트워크를 호출하지 않는다.

신규 테스트는 다음을 포함한다.

- 신규 JSON/공식 SSE 완료 시 버전 저장, 결과 JSON/RTI/level/신호 점수 보존, SSE meta/result/done 키 불변.
- 코드 근거가 있는 리뷰와 중립 안내 리뷰가 섞인 한 작업의 전체 파이프라인 버전 저장.
- 설명 버전 상수만 변경하면 저장 메타데이터만 달라지고 전체 API 결과 JSON은 동일함.
- 과거 NULL 또는 이전 설명 버전의 DONE replay: 전체 DB 행 불변, 모델/Groq/중립 안내 미호출.
- 기존 MySQL 스키마 업그레이드 및 반복 실행: 과거 DONE/RUNNING/FAILED, timestamps,
  request/result JSON, idempotency 레코드와 과거 NULL 보존.
- 신규 설치·두 동시 migration·두 동시 Worker 초기화, named-lock 경합 시 DDL 미실행.
- 비호환 기존 컬럼은 자동 수정하지 않음, INSTANT DDL 실패 후 잠금 반환, 제한된 잠금 대기.
- SQLite 기존 파일 업그레이드·반복 실행·NULL 보존.
- SQLite/MySQL trigger로 완료 UPDATE를 거부했을 때 DONE/result/version이 함께 미반영됨.
- CLI 읽기 전용 검사 및 실패 출력에 DB 예외 원문/접속 설정 미노출.

관측한 불안정성도 기록한다. 첫 Windows 전체 실행은 583 passed / 1 failed였다.
기존 `test_total_deadline_bounds_a_500_review_batch`가 40ms wall-clock budget 안에 첫 호출을
시작하지 못해 호출 횟수 기대값에서 실패했다. 해당 테스트 단독 실행은 1 passed(0.15초),
뒤이은 전체 재실행은 584 passed(13.53초), 검증 도구 수정까지 반영한 최종 재실행도 위의 584 passed였다.
Groq 처리/시간 정책이나 이 테스트 기대값은 수정하지 않았다.
Linux 전체 실행에서는 같은 실패가 발생하지 않았다.

## Docker / 실제 모델 / Worker 2

운영과 별도인 `review-ai-runtime-check` 프로젝트, MySQL 8 tmpfs, 외부 모델 read-only mount,
CPU quota 2 / RAM 4 GiB, `UVICORN_WORKERS=2`, OMP/MKL 각각 1, `PTEXT_BATCH_SIZE=8`, Groq OFF에서 실행했다.
기존 로컬 운영용 컨테이너/볼륨과 실제 운영 DB는 대상이 아니다.

- Docker 이미지 build 및 Compose `config --quiet` 성공.
- 실제 `main:app` Worker 2 startup/health 확인 후 검증 전용 benchmark wrapper로 동시 요청도 확인했다.
  검증 wrapper/테스트 마운트는 운영 이미지·Compose에 추가하지 않았다.
- 명시 CLI `--check`는 compatible, migration 재실행은 already_applied, 모두 종료 코드 0.
- [실제 모델 API/SSE 보고서](explanation-policy-runtime-20261020.json): CPU inference 정상,
  JSON/공식 SSE/실험 SSE와 MySQL 결과 동일, 세 경로 모두 설명 버전 `neutral-summary-v1` 저장.
  공식 SSE DONE replay의 DB 행 불변과 분석 미호출, 인증 선행, lazy health, 캐시 1회 로딩 검증.
  이 smoke의 TestClient는 단일 검증 프로세스이며 실제 Worker 2 HTTP 검증은 다음 항목이다.
- [Worker 2 HTTP 검증](explanation-policy-worker2-20261020.json): 실제 모델은 두 PID(9, 10)에서 각각 1회 로딩.
  분석 실행 구간이 서로 겹쳤고 SSE 성공 6회, 실패 0회였다. 같은 key 동시 요청은 200/409,
  모델 분석 시작은 1회였다. DONE replay도 재분석하지 않았다.
  이번 run_id에 해당하는 저장 작업 5개를 별도 SQL로 조회하여 전부 DONE이며 설명 버전이 v1임을 확인했다.
  일반 길이 합성 리뷰 8건/요청, warm 2요청의 기능 smoke이며 운영 성능 추정 자료로 사용하지 않는다.

## Golden 18건 Parity

[전체 CPU 보고서](explanation-policy-parity-20261020.json).
기존 AI 팀 Golden 18건을 재생성하거나 변경하지 않고 비교했다.

- Batch 1/8/16/32 각각 2회: label/text_score/unavailable/order/RTI/level exact parity 통과.
- 단건↔배치 확률 최대 절대 차이는 `1.5497207641601562e-6`.
  확률의 작은 수치 차이를 점수/label 차이의 허용 근거로 사용하지 않았다.
- 원본 Git blob 그대로: Golden `673babdea503a68933b644306281f50d3bd82d48`,
  cases `80b6e5661a9feabfb6d6582a1b09900e07c9ee04`.
- 모델/threshold/RTI weight/등급, P_text Batch/behavior/network/Groq 구현은 수정하지 않았다.

## 운영 마이그레이션과 제한

[명시적 마이그레이션·복구 절차](../explanation-policy-migration.md)를 먼저 읽는다.
운영 적용 담당자가 기존 Compose 프로젝트/DB/env와 새 코드를 포함한 사전 빌드 이미지를 선택한 뒤 실행한다.

```sh
docker compose run --rm --no-deps ai python -m app.repositories.job_migrations
docker compose run --rm --no-deps ai python -m app.repositories.job_migrations --check
```

**기존 Actions는 main push 때 자동 배포하므로 병합 전에 drain·백업·명시 migration·호환 검사를 완료해야 한다.**
이 작업은 workflow를 바꾸거나 자동 마이그레이션을 추가하지 않는다. 컬럼이 없는 기존 DB에서
신규 Worker는 안전하게 startup을 거부한다. INSTANT가 지원되지 않으면 COPY/rebuild로 자동 우회하지 않는다.
MySQL DDL은 implicit commit이므로 SQL ROLLBACK으로 되돌릴 수 없다. 앱만 이전 이미지로
롤백할 때는 추가된 nullable 컬럼을 유지한다. 과거 JSON/버전 backfill이나 테이블·볼륨 초기화는 하지 않는다.

기존 강제 종료/OOM 뒤 RUNNING 잔류 정책, 과거 DONE의 빈 reasons, 실제 운영 길이 분포에 대한
최대 동시 부하 검증은 이번 후속 변경 범위 밖이다. 실제 Groq 호출·운영 secret 수정·Azure 배포도 하지 않았다.

## 변경 파일

- 런타임/저장: `app/core/analysis_versions.py`, `app/services/reason_explanations.py`,
  `app/services/data_analysis.py`, `app/repositories/analysis_jobs.py`, `app/repositories/mysql_jobs.py`.
- 마이그레이션: `app/repositories/job_migrations.py`, `scripts/migrate_analysis_jobs.py`.
- 테스트/검증 도구: `tests/test_job_migrations.py`, `tests/test_explanation_policy_persistence.py`,
  `tests/test_mysql_jobs.py`, `tests/test_groq_reason_api.py`, `tests/test_reason_explanations.py`,
  `scripts/validate_integrated_runtime.py`.
- 문서: `README.md`, `docs/README.md`, `docs/reason-explanations.md`,
  `docs/explanation-policy-migration.md`, `docs/pr-batch-inference-workers-reasons.md`,
  이 보고서와 링크된 실측 JSON 3개.

사전 존재하던 `docs/data-analysis-stream.md` 수정과 무관한 untracked 파일은 커밋하지 않는다.
