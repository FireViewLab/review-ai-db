<!-- 작업 단위 설명 정책 버전 저장을 위한 MySQL 명시적 마이그레이션과 배포·복구 절차를 설명한다. -->
# 설명 정책 버전 DB 마이그레이션

2026-10-20 PR #34 후속 변경의 운영 적용 가이드다. 이번 개발 작업에서는 운영 DB에서
명령을 실행하거나 Azure에 배포하지 않는다. 아래 명령은 운영 적용 담당자가 승인된
배포 절차 안에서 실행하는 예시다. 실제 접속 정보·토큰·키를 명령이나 문서에 넣지 않는다.

## 저장 의미와 호환성

`app/core/analysis_versions.py`의 `EXPLANATION_POLICY_VERSION`은 현재
`neutral-summary-v1`이다. 설명 모듈과 공통 분석·저장 서비스가 이 정의를 사용한다.
점수 정책 `rti-v0.1`, 모델 버전, 계약 `v0.5`와 독립적이다.

추가 컬럼은 다음과 같다.

```sql
explanation_policy_version VARCHAR(64) NULL DEFAULT NULL
```

신규 작업은 최종 `result_json`, `DONE` 상태, 실제 적용한 설명 버전을 동일한 UPDATE와
트랜잭션으로 완료한다. 분석/저장이 실패한 작업에 성공 버전을 먼저 기록하지 않는다.
버전은 전체 설명 파이프라인의 버전이다. 한 작업에 원본 이유 코드·선택적 Groq 자연어화·
중립 안내가 섞일 수 있으며 이 값만으로 리뷰별 경로나 Groq 성공 여부를 판단할 수 없다.

마이그레이션은 기존 행이나 `result_json`, `ai_analysis_idempotency`를 갱신하지 않는다.
과거 버전은 `NULL`로 남으며 자동 backfill·재분석을 하지 않는다. DONE replay는 저장된
결과·버전을 유지하고 현재 설명기로 재생성하지 않는다. 설명 버전은 Idempotency-Key나
payload hash 구성에 추가하지 않는다. RUNNING/FAILED 및 멱등성 충돌 정책도 유지한다.

이 메타데이터는 DB 컬럼이며 공식 JSON 응답이나 SSE `meta/result/done`에 새 필드를 넣지 않는다.
`analysis_versions()`는 계속 기존 contract/model/policy 세 필드만 제공한다.

## 명시적 도구와 잠금

배포 이미지에는 다음 모듈형 CLI가 포함된다.

```sh
python -m app.repositories.job_migrations
python -m app.repositories.job_migrations --check
```

저장소에서 실행할 때는 동일한 CLI의 wrapper도 사용할 수 있다.

```sh
python scripts/migrate_analysis_jobs.py
python scripts/migrate_analysis_jobs.py --check
```

Docker 이미지는 `scripts/`를 복사하지 않으므로 컨테이너에서는 모듈형 명령을 사용한다.
접속 설정은 기존 `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_NAME`을 사용한다.
실행 계정에는 대상 DB 접속 및 필요한 DDL 권한이 있어야 한다. 비밀번호를 CLI 인자에 쓰지 않는다.

- 신규 설치: 두 작업 테이블을 생성하며 설명 컬럼을 포함한다. 출력 상태는 `created`다.
- 기존 스키마: 설명 컬럼이 없을 때만 `ALGORITHM=INSTANT`로 nullable 컬럼을 추가한다.
  출력 상태는 `added`다. MySQL 8 환경에서 해당 테이블이 INSTANT를 지원하지 않으면
  table COPY/rebuild로 자동 전환하지 않고 실패하므로 별도의 정비 계획을 검토한다.
- 재실행: 호환 컬럼이 이미 있으면 변경 없이 `already_applied`를 출력한다.
- `--check`: 컬럼의 존재·nullable·VARCHAR 길이 64 이상·DEFAULT NULL을 읽기 전용으로 검사한다.
  성공 시 종료 코드 0, 실패 시 비밀값과 DB 예외 원문이 없는 오류 안내 및 종료 코드 1이다.
- `--lock-timeout-seconds`는 명시적 마이그레이션의 named lock 대기를 0~20초로 설정한다(기본 10초).
  named lock 획득 뒤 DDL 세션의 `lock_wait_timeout=10`으로 metadata lock 대기도 제한한다.
  두 설정은 별개이며 ALTER 전체 실행 시간의 제한을 의미하지 않는다.

MySQL의 신규 설치 startup CREATE/검사와 명시적 ALTER는 DB명에 대응하는 동일한
`GET_LOCK`을 사용한다. Worker 두 개가 동시에 시작하거나 별도 마이그레이션이 겹쳐도
DDL 구간을 직렬화하며 해당 세션이 잠금을 해제하거나 연결을 닫으면 반납된다.
이미 존재하는 스키마를 Worker startup에서 자동 ALTER하지 않는다. 컬럼이 없거나
호환되지 않으면 startup이 스키마 오류로 중단되므로 새 Worker를 띄우기 전에 마이그레이션한다.

SQLite는 격리 테스트·과거 로컬 파일용이다. 초기화 시 쓰기 잠금 안에서 nullable TEXT 컬럼을
추가하고 기존 행을 NULL로 유지한다. 운영 MySQL의 명시적 적용 절차를 대체하지 않는다.

## 운영 적용 순서

기존 Actions는 main push 후 테스트가 성공하면 `docker compose up -d --build`로 자동 배포한다.
이 변경은 Actions에 자동 마이그레이션 단계를 추가하지 않는다. **병합 전에** 아래 준비를
끝내야 한다. 신규 코드가 먼저 배포되면 이전 DB 스키마를 감지한 Worker가 시작하지 못한다.

1. 검토된 PR commit의 새 이미지를 별도 준비 작업에서 사전 빌드한다. 운영 main checkout을
   임의 변경하지 않으며, 운영 Compose의 `ai` 서비스가 그 사전 빌드 이미지를 가리키도록 준비한다.
   기존 이미지 태그/이미지 ID도 보관해 되돌릴 수 있게 한다. 새 이미지로 아직 서버를 시작하지 않는다.
2. Data 팀과 점검 시간을 맞추고 신규 분석 요청을 막은 뒤 진행 중 JSON/SSE 작업을 drain한다.
   필요하면 기존 AI 서비스만 정상 종료한다. DB 컨테이너와 영속 볼륨은 유지한다.
   강제 종료 뒤 남은 RUNNING은 이 마이그레이션이 자동 복구하지 않는다.
3. 운영 백업 절차로 DB 스키마·작업 데이터·멱등성 테이블을 백업하고 복구 가능성을 확인한다.
   기존 `NULL`/행 수·idempotency 수 등 비교할 기준도 기록한다. 원본 리뷰와 결과를 로그에 출력하지 않는다.
4. **기존 운영 Compose 프로젝트·DB·`.env`를 그대로 선택하고**, 위에서 준비한 새 이미지로 다음을 실행한다.
   `--no-deps`는 DB를 재생성하거나 시작하지 않으므로 기존 DB가 정상 실행 중이어야 한다.

   ```sh
   docker compose run --rm --no-deps ai python -m app.repositories.job_migrations
   docker compose run --rm --no-deps ai python -m app.repositories.job_migrations --check
   ```

   `compose run`은 서버 CMD를 마이그레이션 명령으로 대체하며 API 포트를 게시하지 않는다.
   여기서는 이미지를 빌드하지 않는다. 실행 전 서비스가 신규 코드를 포함한 이미지를 사용하는지
   반드시 확인한다. 임의의 새 Compose 프로젝트명이나 테스트 override를 운영에서 사용하지 않는다.
5. 정상 종료 및 스키마 호환을 확인한 다음 PR 병합과 기존 자동 배포를 진행한다.
   운영 `UVICORN_WORKERS=2`, `OMP_NUM_THREADS=1`, `MKL_NUM_THREADS=1`, `PTEXT_BATCH_SIZE=8`
   설정과 외부 모델 마운트를 유지한다. health와 신규 JSON/SSE 작업의 저장 메타데이터를 확인한 뒤
   트래픽을 재개한다. health 성공만으로 실제 모델 추론 검증을 대신하지 않는다.

호환 컬럼의 상태는 운영 DB 도구에서 다음과 같이 읽기 전용으로 확인할 수 있다.

```sql
SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, IS_NULLABLE, COLUMN_DEFAULT
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA = DATABASE()
  AND TABLE_NAME = 'ai_analysis_jobs'
  AND COLUMN_NAME = 'explanation_policy_version';

SELECT explanation_policy_version, COUNT(*) AS job_count
FROM ai_analysis_jobs
GROUP BY explanation_policy_version;
```

과거 행이 `NULL`인 것은 정상이다. 새로 성공한 분석은 현재 버전을 기록하며, 기존 DONE replay는
과거 NULL/이전 버전 그대로다. 배포 시 과거 행 수나 버전을 자동 변환하지 않는다.

## 실패 시 복구와 앱 롤백

MySQL ALTER는 implicit commit을 일으키므로 SQL `ROLLBACK`으로 이미 추가된 컬럼을
되돌릴 수 없다. 도구의 예외 처리도 완료된 DDL을 취소한다고 보장하지 않는다.

- named lock을 얻지 못하면 다른 startup/migration 완료 여부를 확인한 뒤 다시 실행한다.
  장시간 트랜잭션의 metadata lock 대기 가능성도 확인한다. 데이터 삭제나 세션 강제 종료를
  자동 대응으로 사용하지 않는다.
- 연결·권한·DDL 오류가 발생하면 새 Worker 배포를 멈추고 컬럼 실제 상태부터 확인한다.
  응답 전에 ALTER가 완료됐을 수 있으므로 호환 `--check` 후 필요하면 같은 명령을 재실행한다.
- 기존 컬럼이 예상 타입/nullable/default와 다르면 도구는 임의 수정하지 않는다. 백업과
  해당 스키마의 생성 경위를 검토한 뒤 별도 수정 계획을 세운다.
- 앱만 이전 이미지로 되돌릴 때는 추가된 nullable 컬럼을 유지한다. 이전 코드는 기존 컬럼을
  명시해서 사용하므로 결과·멱등성 데이터를 그대로 읽을 수 있다. 이전 코드로 실행한 새 작업은
  설명 버전이 NULL일 수 있다.
- 컬럼 삭제는 새 버전 메타데이터를 잃게 하므로 일반 앱 롤백에 포함하지 않는다.
  전체 DB 복원은 백업 이후 작업을 잃을 수 있으므로 단순 실패 대응으로 실행하지 않는다.

`docker compose down -v`, 테이블 재생성, 과거 `result_json` 수정, 설명 버전 backfill은
이 적용·복구 절차에 포함하지 않는다.
