<!-- 새 Azure Ubuntu VM의 AI 서비스 최초 설치와 자동 배포 절차. -->
# Azure for Students 배포

Batch Inference 도입 후 실행 설정은 [Worker 병렬 처리 안내](worker-concurrency.md)를 따릅니다.
현재 운영 후보는 `UVICORN_WORKERS=2`, CPU 스레드 1개씩, `PTEXT_BATCH_SIZE=8`입니다.
Worker 1/2 성능 및 메모리 비교는 별도 실측 결과를 확인한 뒤 운영 적용합니다.
아래 2026-10-02 검증 수치와 worker 1 기록은 당시 실행 조건입니다.

2026-10-02의 KoELECTRA 통합은 로컬 검증 단계입니다. 아래 명령은 향후 배포 준비용이며
이번 작업에서는 Azure 배포·commit/push를 실행하지 않습니다.
[2차 통합 검증 보고서](validation/review-ai-runtime-integration-20261002.md)의 결과와 Data 계약 확인 후 적용합니다.

> 현재 코드는 MySQL 8.0과 기존 ai-db-data 볼륨을 사용합니다.
> 배포 전에 VM의 .env에 유효한 DB 계정을 설정해야 합니다. 기존 볼륨은 초기 비밀번호 설정으로 재설정되지 않습니다.
> DB 백업/보존 정책을 확정해야 하며 실험 수집/SSE API는 운영에서 OFF로 유지하세요.
> 이전 검증 기록과 구분하고 [통합 준비 상태](integration-preparation.md)를 먼저 확인하세요.

대상: Ubuntu Server 24.04 LTS x64, reviewadmin, /home/reviewadmin/review-ai-db.
VM 생성과 GitHub Secrets 변경은 사용자가 수행한다.
NSG 포트는 SSH 22와 AI HTTP 8000이며, 8000은 가능한 Data 서버 출발지만
허용한다. MySQL은 Compose 내부에서 통신하므로 DB 포트를 NSG에 공개하지 않는다.
분석 API에 REQUIRE_INTERNAL_TOKEN=1 및 INTERNAL_TOKEN을 설정하고 TLS termination을 구성한다.
접근 허용 범위는 팀에서 확정한다. /health는 인증 없는 프로세스 상태 확인용이다.
`/health`는 모델을 lazy load하지 않으므로 health 성공만으로 모델 파일·추론 readiness를 판단하지 않는다.
2026-10-02 초기 후보는 2 vCPU / 4 GiB / worker 1이었다. 현재 Worker 2 후보는 같은 사양에서
추가 검증해야 한다. CPU 전용 이미지와 외부 모델 읽기 전용 마운트를 사용한다.

## 모델과 환경변수 준비

모델 폴더를 VM의 별도 위치(예: `/opt/review-ai-models/ptext-koelectra-v1-2epoch-20260929`)에 준비한다.
config.json, inference_config.json, model.safetensors, tokenizer.json, tokenizer_config.json,
special_tokens_map.json, vocab.txt가 필요하다. 이 파일은 Git에 commit/push하거나 이미지에 복사하지 않는다.
컨테이너 uid 10001이 폴더와 파일을 읽을 수 있어야 하며 가중치 자체를 변경하지 않는다.

VM의 `.env`에는 기존 DB·토큰·Data 설정과 함께 다음을 지정한다.

```dotenv
PTEXT_MODEL_HOST_PATH=/opt/review-ai-models/ptext-koelectra-v1-2epoch-20260929
PTEXT_MODEL_PATH=/models/ptext-koelectra-v1-2epoch-20260929
UVICORN_WORKERS=2
OMP_NUM_THREADS=1
MKL_NUM_THREADS=1
PTEXT_BATCH_SIZE=8
ENABLE_EXPERIMENTAL_COLLECTION=0
REQUIRE_INTERNAL_TOKEN=1
```

`PTEXT_MODEL_HOST_PATH`는 호스트 경로, `PTEXT_MODEL_PATH`는 컨테이너 경로다.
Compose는 읽기 전용 bind mount를 사용하며 호스트 폴더가 없으면 자동 생성하지 않고 시작 실패한다.
Dockerfile은 기본 Worker 후보를 2개로 설정하고 Uvicorn의 `UVICORN_WORKERS` 환경변수를 사용한다.
Worker 1 비교나 로컬 실행에는 `UVICORN_WORKERS=1`을 지정할 수 있다.
CPU torch 2.8.0+cpu·transformers 4.57.6 등 requirements-ml.txt의 고정 버전을 설치한다.
모델 캐시는 프로세스 단위이므로 worker를 추가하면 모델 메모리도 복제된다.
코드의 CUDA 선택/CPU fallback은 유지하지만 기본 이미지는 CPU 전용이다.

## 최초 한 번: 새 VM에서 reviewadmin으로 실행

Docker 공식 Ubuntu 설치 문서의 apt 저장소 방식을 사용한다:
[Docker Engine on Ubuntu](https://docs.docker.com/engine/install/ubuntu/).
아래 명령은 Docker가 없는 새 Ubuntu VM 기준이다.

```sh
sudo apt-get update
sudo apt-get install -y ca-certificates curl git
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu noble stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo docker compose version
cd /home/reviewadmin
git clone --branch main https://github.com/FireViewLab/review-ai-db.git
cd /home/reviewadmin/review-ai-db
# 실행 전에 .env에 DB·토큰 및 위 모델 경로를 준비한다. 기존 .env를 덮어쓰지 않는다.
sudo docker compose config --quiet
sudo docker compose up -d --build
curl --fail http://localhost:8000/health
# 인증 설정 시 INTERNAL_TOKEN을 안전하게 환경에 준비한 뒤 실행한다. 토큰을 출력하지 않는다.
curl --fail http://localhost:8000/api/v1/data/analyze -H "X-Internal-Token: $INTERNAL_TOKEN" -H 'Content-Type: application/json' -d '{"platform":"mall","product_id":"A001","reviews":[{"review_id":"1001","content":"배송 빠르고 제품도 좋아요"}]}'
```

먼저 Data 소비자의 -1 unavailable·70/40 등급·출처 접두사 reasons 처리를 확인하고,
통합 코드의 main 반영과 배포 승인이 있어야 위 main clone으로 새 런타임을 실행한다.
기존 디렉터리가 있다면 clone을 반복하지 말고 브랜치와 로컬 변경을 확인한다.
비공개 저장소라면 VM의 git pull에 필요한 읽기 인증을 별도로 설정한다.
토큰을 remote URL이나 workflow에 하드코딩하지 않는다.

SSH 배포는 sudo 비밀번호 프롬프트 없이 docker compose를 실행할 수 있어야 한다.
VM 관리자가 권한을 준비하고 `sudo -n docker compose version`으로 확인한다.
AZURE_PASSWORD는 SSH 인증용이며 sudo 프롬프트에는 자동 전달되지 않는다.

## Repository Secrets와 자동 배포

- AZURE_HOST: 새 VM 공인 IP
- AZURE_USER: reviewadmin
- AZURE_PASSWORD: 새 VM SSH 비밀번호

VM의 .env에 DB_PASSWORD를 반드시 설정한다. DB_USER/DB_NAME은 기본 root/review_system이며
운영에서는 별도 최소 권한 계정을 사전에 생성해 사용한다. DB_HOST_PORT는 로컬 공개 포트다.
Compose의 AI 컨테이너는 DB_HOST=ai-db, DB_PORT=3306으로 고정한다. Redis는 사용하지 않는다.
GitHub에 저장된 기존 secret 값 자체는 이번 작업에서 삭제하지 않았다.

main push → Python 3.12 pytest → Compose 설정·빌드 검증 → SSH →
main 브랜치 확인 → git pull --ff-only origin main →
sudo docker compose up -d --build → health 재시도 검사.

workflow는 .env를 생성하거나 덮어쓰지 않는다. 최초 실행 전 .env.example을 참고하여
VM에 DB·인증·외부 모델 설정을 직접 준비한다. CI 빌드는 모델 파일 없이 가능하며
CI 빌드의 임시 비밀번호는 배포에 사용되지 않는다. 기존 GitHub Actions 구조는 유지한다.
선택적 Google 인증은 README의 읽기 전용 마운트를 사용한다.
VM의 docker-compose.override.yml에 설정하면 자동 배포 명령에도 적용된다.
컨테이너 교체 시 중단이 있을 수 있다. Compose는 진행 중 작업의 정상 종료를 위해 최대 180초를
기다린다. 강제 종료/OOM으로 남은 RUNNING은 자동 회수하지 않는다. 배포 전 유입을 멈추고
진행 작업을 확인하는 절차와 잔류 작업 복구 제한은 [Worker 병렬 처리 안내](worker-concurrency.md)를 따른다.
기존 VM의 DB/Redis 컨테이너·볼륨은 자동 삭제하지 않는다.

## 검증 상태

현재 통합 버전의 로컬 테스트·실제 모델·MySQL 결과는 [2차 검증 보고서](validation/review-ai-runtime-integration-20261002.md)를 따른다.
2026-10-02 Windows/Docker Linux 전체 pytest는 각각 327개 통과했으며 실제 MySQL 테스트를 포함했다.
Docker config/build/up/healthy와 외부 모델 읽기 전용 마운트 및 2 CPU/4 GiB 제한 CPU 추론을 확인했다.
배포 전 실제 VM에서 긴 배치 timeout·동시 요청·외부 모델 권한과 정상 리뷰 분석을 재확인해야 한다.
실제 Data HTTPS·인증·SSE 샘플 연동은 별도 검증이다. 이번 검증의 SSE는 모의 HTTPS Data transport를 사용했다.

아래는 2026-09-08의 이전 runtime 기록이다. Python 3.12 pytest 22개(실제 Uvicorn HTTP 포함), pip check,
docker compose config 통과.
Linux 엔진 시작 후 docker compose build/up/ps와 시작 로그 검증도 통과했다.
review-ai는 healthy, 재시작 0회이며 startup error가 없었다.
localhost:8000의 health와 analyze는 HTTP 200, 예제 점수는 88/safe였다.
기존 review_ai_db orphan 경고는 공유 인프라 보존을 위해 그대로 두었다.
