<!-- 기존 팀 문서를 보존하면서 현재 기준·협의 중·과거 기록으로 안내하는 목차. -->
# 문서 목차

분류 기준일: 2026-10-02 · 대상: `FireViewLab/review-ai-db`의 로컬 런타임 통합 작업

이 목차는 현재 작업에서 문서를 읽는 순서를 안내합니다. 팀원의 원문을 삭제·이동하거나
내용을 수정하지 않았으며, **분류 자체가 팀의 설계 채택·폐기 또는 운영 배포 완료를 뜻하지 않습니다.**
문서 안의 “현재”, “완료”는 해당 문서 작성 당시의 표현일 수 있습니다.

## 처음 읽는 순서

1. [프로젝트 README](../README.md): 역할, 기능 상태, 설치와 첫 분석 요청.
2. [AI 통합 준비 상태](integration-preparation.md): DB 저장·실험 SSE 구현과 미확정 계약, 복구 한계.
3. [Azure 배포 안내](azure-ai-deployment.md): 배포 준비와 검증 제한. 배포 전에 상단 주의사항 확인.
4. [2차 런타임 통합 검증 보고서](validation/review-ai-runtime-integration-20261002.md): 비교·변경·실제 모델/DB/컨테이너 검증과 배포 TODO.
5. 아래 협의 문서에서 필요한 안건을 선택하되, 과거 구조를 현재 계약으로 적용하지 않기.

## 현재 기준

현재 구현·실행 준비를 확인할 때 우선 읽는 문서입니다. “현재 기준”도 모든 기능의 운영 검증이나
API 계약 확정을 의미하지 않습니다. 세부 동작은 같은 브랜치의 코드·테스트와 함께 확인하세요.

| 문서 | 읽을 내용 / 주의점 |
| --- | --- |
| [프로젝트 README](../README.md) | 기본 분석 API, 로컬 실행, 기능별 상태, 결과 DB 보관 주의사항 |
| [Data AI v0.5 연동](data-ai-v05-integration.md) | KoELECTRA runtime, -1 결과 계약, 70/40 등급, RTI 재가중, 공유 토큰·HTTPS, 이전 결과와 차이 |
| [AI 통합 준비 상태](integration-preparation.md) | 자체 MySQL 결과 저장, 팀원 코드와의 차이, 실험 SSE, 미구현 복구 기능. 검증·커밋 관련 기록은 작성 시점 기준 |
| [Azure 배포 안내](azure-ai-deployment.md) | VM·Secrets·외부 모델 읽기 전용 마운트·worker 1 준비. 이번 단계는 로컬 검증이며 실제 배포 없음 |
| [2차 런타임 통합 검증](validation/review-ai-runtime-integration-20261002.md) | review-ai-new ff3c149와 기존 운영 구조의 통합 결과·근거·배포 전 TODO |
| [Groq reasons 한국어 후처리](groq-reason-naturalization.md) | 기본 OFF인 reasons 전용 후처리, 환경변수·배치·시간 제한·키 전환·코드 fallback·외부 전송 범위 |

현재 브랜치의 핵심 구분:

- 기본 분석 API는 입력·결과를 AI 자체 DB에 저장한 뒤 최종 JSON을 반환합니다.
- 수집/SSE와 결과 조회는 실험 경로이며 기본 비활성화입니다.
- Redis 기반 Worker는 현재 실행 경로가 아닙니다.
- 자동 작업 재개·멱등 요청·자동 재전송은 아직 구현되지 않았습니다.
- API/SSE는 공통 analyze_reviews()와 결과 serializer를 사용하고 KoELECTRA를 lazy load합니다.
- 선택적 Groq 후처리는 점수 계산 뒤 reasons만 한국어로 바꿉니다. 기본 OFF이며 외부 오류 시 원래 코드로 fallback합니다.
- 이 결과 계약은 신규 분석 작업 기준입니다. 기본 OFF인 저장 작업 조회는 과거 JSON을 그대로 반환하며 자동 계약 변환은 하지 않습니다.
- Docker는 CPU 전용 추론 의존성과 외부 모델 읽기 전용 마운트, worker 1을 사용합니다. 모델 파일은 Git에 넣지 않습니다.
- 경로·입력·인증·MySQL은 유지합니다. 이전 null·80/50·사유 코드와 통합 -1·70/40·출처 접두사 결과의 차이는 Data 소비자 확인이 필요합니다.

## 협의 중

합의가 필요한 질문·후보 설계를 찾아보기 위한 분류입니다. 아래 문서에는 **이전의
Spring → Redis → FastAPI Worker 구조와 `product_analysis_job` / `review_trust_scores`
테이블을 전제로 한 초안**이 포함됩니다. 현재 `ai_analysis_jobs` 저장소나 SSE 계약과 동일하지
않으며, 문서의 예시 endpoint·상태값·필드를 확정 계약으로 복사하지 마세요.

### 역할·API·실패 처리

| 문서 | 협의에 활용할 내용 |
| --- | --- |
| [Data/AI v0.4 계약 차이·질문·샘플 점검표](data-ai-contract-v04-review.md) | 2026-09-16 PDF 검토본과 현재 코드 비교. 실제 정규화 입력을 받기 전 질문·점검 항목; 최종 승인 계약이 아님 |
| [비동기 분석 Job/Queue 논의](async-analysis-job-discussion.md) | 역할 분담, Job 생성·완료 알림, 기존 API 전환 안건. Queue 도입은 현재 구현이 아님 |
| [비동기 분석 API 계약 질문](async-analysis-api-contract-questions.md) | 요청·상태·결과 조회, 식별자, 오류 응답을 정하기 위한 질문 |
| [팀 연동 체크리스트](team-integration-checklist.md) | 파트별 책임과 연동 전 확인 항목. 이전 Redis 흐름은 재검토 필요 |
| [비동기 분석 실패 처리 정책 초안](async-analysis-failure-policy.md) | 실패 판정, 오류 기록, 재시도와 사용자 메시지 후보 |
| [FastAPI Worker 책임 범위 초안](fastapi-worker-responsibility-draft.md) | Worker를 도입할 경우의 책임 후보. 현재 Worker 운영 안내가 아님 |
| [Redis 분석 작업 메시지 초안](redis-analysis-job-message-draft.md) | Queue를 채택할 경우의 메시지·식별자 후보. 현재 API 입력 형식이 아님 |

### 상태·저장·입력·확장 필드

| 문서 | 협의에 활용할 내용 |
| --- | --- |
| [Product Analysis Job 스키마 검토](product-analysis-job-schema-review.md) | 기존 팀 테이블의 필드·인덱스·오류 정보 검토. 새 결과 DB 스키마와 구분 |
| [Product Analysis Job 상태 초안](product-analysis-job-status-draft.md) | 파트 간 상태 이름·전이·업데이트 책임 후보 |
| [Saved RTI 비동기 분석 흐름](saved-rti-async-flow.md) | 저장된 점수의 재사용·완료 판단·조회 정책 후보 |
| [Crawler Raw JSON 스키마 초안](crawler-raw-json-schema.md) | 이전 파일 기반 raw JSON의 필드와 DB 매핑 후보. 현재 SSE 리뷰 계약과 다름 |
| [NAVER RTI 결과 → DB 매핑 초안](naver-rti-result-to-db-mapping.md) | 네이버 분석 출력과 `review_trust_scores` 컬럼의 매핑·저장 정책 후보 |
| [v1 확장 필드 계획](v1-extended-fields-plan.md) | `key_signal`, `patterns`, `tags`, `highlights`의 의미와 도입 조건. 구현 완료 API 필드가 아님 |

현재 미확정 사항의 출발점은 [통합 준비 상태의 합의 후 해야 할 일](integration-preparation.md)입니다.
협의 결과가 나오면 먼저 현재 기준 문서에 반영하고, 이전 초안은 변경 이력을 확인할 수 있게 보존합니다.

## 과거 기록

이전 구조·단계별 개발 계획·로컬 검증의 맥락을 보존합니다. 유용한 내용이 남아 있어도
현재 서버의 실행 절차나 API 계약을 설명하는 문서로 간주하지 마세요.

### 구조 전환과 검증 기록

| 문서 | 보존 목적 / 현재와의 차이 |
| --- | --- |
| [독립 AI 서비스 전환 기록](ai-service-migration.md) | DB·Redis 제거 당시 분석과 변경 이력. 이후 AI 자체 결과 DB를 추가했으므로 DB 제거 설명은 과거 기록 |
| [프로젝트 파일 역할 안내](project-file-guide.md) | 모듈 정리 당시 파일 위치·역할. MySQL 초기화·Redis consumer 설명은 현재 실행 구조와 다름 |
| [RTI 계산 로직 문서](rti-logic.md) | 과거 v0 scoring 설명. 공식 API는 app/scoring/meta_scorer.py와 v0.5 연동 문서 기준. ai/analysis.py는 비운영 과거 참고용 |
| [Crawler MVP 전략](crawler-mvp-strategy.md) | 파일 기반 raw → converter → DB → RTI 검증 전략의 배경 |
| [NAVER Worker 로컬 시뮬레이션 결과](naver-worker-local-simulation-result.md) | 당시 네이버 상품 1개·리뷰 30개 검증 기록. 현재 실서비스 통합 성공을 뜻하지 않음 |
| [Crawler raw JSON 예제](examples/crawler_raw_sample.json) | 이전 raw 스키마의 샘플 데이터. 현재 분석 API나 SSE에 그대로 보내는 요청 본문이 아님 |

### v1 개발 계획 원문

이전 DB 조회·정규화·저장 파이프라인을 전제로 한 계획입니다. 원문을 그대로 보존하며,
운영 DB나 점수 재사용 정책을 재논의할 때 참고할 수 있습니다.

| 문서 | 보존 목적 |
| --- | --- |
| [v1 개발 계획](v1-plan.md) | v0 데모에서 실제 리뷰 누적·분석으로 확장하려던 단계별 목표 |
| [v1 DB 계획 (.md)](v1-db-plan.md) | 초기화·seed 분리·적재·저장 설계 원문 |
| [v1 DB 계획 (확장자 없는 파일)](v1-db-plan) | 별도로 존재하는 팀 문서도 유지. 위 파일과 임의로 통합하거나 삭제하지 않음 |
| [v1 데이터 적재 계획](v1-data-ingestion-plan.md) | raw → normalized → 기존 products/reviews 적재 계획 |
| [v1 RTI 저장 계획](v1-rti-persistence-plan.md) | 실시간 계산과 저장 후 재사용을 비교한 계획 |
| [v1-10 Saved RTI API 연결 계획](v1-10-saved-rti-api-plan.md) | 과거 `/api/internal/ai/reviews/product-detail`의 저장 점수 우선 조회 계획. 해당 API는 현재 등록 해제됨 |

## 목차 유지 원칙

- 기존 문서의 내용·파일명은 작성자와 협의 없이 정리 목적으로 바꾸거나 삭제하지 않습니다.
- 새 문서는 목적·상태·전제 구조를 확인한 뒤 이 목차에 링크합니다.
- 협의 문서가 채택돼도 구현 여부와 검증 범위를 확인한 뒤 “현재 기준”으로 옮깁니다.
- 서로 충돌하는 설명은 최신 날짜만으로 판단하지 않고 현재 브랜치 코드와 팀 합의를 확인합니다.
- 이 목차 작성은 코드·점수 공식·API·DB 스키마를 변경하지 않습니다.
