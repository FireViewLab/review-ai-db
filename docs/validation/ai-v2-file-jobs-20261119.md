# PR #36 — v2 파일 저장 분리 로컬 검증 (2026-11-19)

- 로컬 `feat/ai-v3-candidate-integration`에서 v2 DTO, `evaluate_v2()`, v3.1 B 로더, Policy Index 코드에 변경이 없는 것을 확인했습니다. v1 MySQL 저장소는 기존 `main` 코드와 일치하고 기본 Compose·Dockerfile·GitHub Actions는 변경하지 않았습니다.
- 기존 로컬 `.env`를 읽지 않는 프로세스에서 전체 pytest **799 통과, 18 건너뜀, 경고 2건**. 파일 테스트는 별도 프로세스 간 동일 키 잠금 충돌, 강제 종료 후 동일 작업 ID로 RUNNING 복구, FAILED 및 손상 파일 처리, 영속화 실패 시 SSE `result`/`done` 미전송을 확인했습니다. 기존 MySQL을 요구하는 별도 테스트는 운영 DB에 연결하지 않았습니다.
- 기본/v2 Compose 구문 검사 및 v2 이미지 빌드 통과. 격리된 v2 Compose에서 MySQL 없이 Worker **2개**를 시작했습니다(테스트 설정: CPU 2, RAM 4 GiB, OMP 1, Groq OFF).
- 실제 v3.1 B 가중치로 전달된 합성 리뷰 **12건**의 직접 `evaluate_v2()` 결과와 SSE의 전 필드 및 순서가 일치했습니다. 이전 v2 MySQL 방식의 같은 12건 `text_score`와도 일치했습니다.
- 96리뷰 작업 중 동일 키 재요청은 `409 IDEMPOTENCY_IN_PROGRESS`, 다른 입력과 같은 키는 `409 IDEMPOTENCY_KEY_REUSED`였습니다. 첫 SSE 연결을 중간에 닫아도 작업이 완료되고, 이후 같은 작업 ID로 재생됐습니다. 이 작업과 서로 다른 키의 동시 작업 두 건을 합쳐, 컨테이너 재생성 후 같은 볼륨에서 **3개 작업**의 UUID·버전·결과가 일치했습니다.

GCP, 운영 MySQL, 운영 Docker 및 Data 서버는 변경하지 않았습니다. 운영 승인·프록시 경로·Data 적재, 500건/동시 트래픽에서의 리소스 여유는 별도의 스테이징 확인이 필요합니다.
