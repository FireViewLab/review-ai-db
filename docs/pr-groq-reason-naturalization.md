<!-- feature/groq-reason-naturalization → main PR에 복사할 제목과 본문. -->
# PR 제목

feat: Groq 기반 reasons 한국어 후처리 추가

# PR 본문

## 목적

기존 분석기가 생성한 reason code를 실제 리뷰 원문에 근거한 짧은 한국어 안내 문장으로 변환합니다. Groq는 점수 계산이 끝난 뒤 적용하는 best-effort 후처리이며, 기본값은 OFF입니다.

## 주요 변경

- 기존 HTTPX로 Groq strict JSON 출력을 요청하고, 사유별 코드·순서·개수와 문장 형식을 검증합니다.
- reason이 있는 리뷰만 최대 20건씩 처리합니다. 요청 크기 제한은 32 KiB, 전체 시간 예산은 timeout의 두 배(기본 10초)입니다.
- PRIMARY의 인증·rate limit·timeout·네트워크·5xx 오류에 한해 SECONDARY로 한 번 전환합니다. 두 키 실패나 잘못된 출력은 기존 reason code로 fallback합니다.
- Groq 요청 payload에 `reasoning_effort: "low"`를 지정합니다. 운영 예정 모델은 `openai/gpt-oss-20b`이지만, `GROQ_MODEL`의 기본값은 추가하지 않고 환경변수로 명시하도록 유지합니다.
- API와 SSE가 공통 후처리·저장 경로를 사용하며, 최종 응답 JSON을 기존 MySQL에 저장합니다.
- PR에서는 테스트만 실행하고, 기존 Azure 자동 배포는 main push에만 실행되도록 구분합니다.

## 기존 계약과 기능 유지

- 응답 필드와 `reasons: list[str]` 형식은 변경하지 않습니다. `explanation` 등 새 필드를 추가하지 않습니다.
- RTI·level·각 score·식별자·review_count, 기존 KoELECTRA/behavior/network 계산과 deterministic reason 생성은 그대로입니다.
- unavailable=-1, RTI 재가중, threshold와 가중치, DB schema는 변경하지 않습니다.
- health·내부 인증·MySQL 저장·Docker 구조·기존 Azure 배포 절차를 유지합니다.
- Groq 장애만으로 분석 API가 실패하지 않습니다. 일부 배치만 성공하면 reasons에는 자연어와 코드가 함께 있을 수 있습니다.

## 검증

- 이번 수정 후 전체 pytest: 전용 임시 MySQL을 포함한 Windows 검증 **405 passed**(5.36초). DB 미설정 기본 실행은 **403 passed, 2 skipped**이며, 기존 TestClient deprecation 경고 2개는 유지됩니다.
- 실제 Groq 호출 없이 MockTransport로 성공·primary/secondary 전환·timeout·잘못된 출력·score/계약 보존을 검증합니다.
- 신규 회귀 테스트 4개: 실제 primary/secondary request payload의 `reasoning_effort: "low"`, 명시한 모델 전달, 모델 환경변수 미설정/빈 값의 무호출 fallback을 확인합니다.
- 직전 Groq 기능 단계(low 추가 전)의 검증 기록: Windows 및 Docker Linux 각각 401 passed, Docker build/Compose config/health, 실제 CPU 모델·API/SSE·MySQL 저장 통과. 이번 low payload 수정에서는 Docker build를 다시 실행하지 않았습니다.
- 실제 Groq 키·모델의 문장 품질 및 quota는 이번 mock 검증 범위에 포함되지 않습니다.

## 운영 설정과 후속 작업

아래 설정은 배포 환경에서 별도로 입력합니다. 저장소 예제에는 키와 모델의 기본값을 넣지 않습니다.

```dotenv
ENABLE_GROQ_REASON_NATURALIZATION=0
GROQ_API_KEY_PRIMARY=
GROQ_API_KEY_SECONDARY=
GROQ_MODEL=
GROQ_TIMEOUT_SECONDS=5
```

운영 활성화 전 `GROQ_MODEL`을 운영 예정 모델로 명시하고, 키·quota·문장 품질·리뷰 원문의 외부 전송 정책을 확인해야 합니다. 실제 API 키와 운영 `.env`는 이번 작업에서 입력하거나 수정하지 않았습니다.

PR 생성과 merge는 담당자가 직접 진행합니다. main 병합 시 기존 GitHub Actions의 테스트 후 Azure 자동 배포가 실행되며, 환경변수를 별도 설정하지 않으면 Groq는 OFF 상태를 유지합니다.

관련 문서: [Groq reasons 한국어 후처리](https://github.com/FireViewLab/review-ai-db/blob/feature/groq-reason-naturalization/docs/groq-reason-naturalization.md)
