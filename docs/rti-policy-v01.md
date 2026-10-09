# RTI 점수 정책 rti-v0.1 (2026-10-09)

이 문서는 코드 변경 기준이며 운영 배포 완료를 뜻하지 않는다. 원본은
[review-ai-new ea34768](https://github.com/FireViewLab/review-ai-new/commit/ea347687e5016201922b1575dcb636231ba1a5b7)의
`fix: reduce RTI score saturation`이다. 서로 다른 이력을 병합하지 않고 두 analyzer의 변경만 이식했다.

## 변경 내용과 유지 범위

- Text: T=2 temperature scaling 후 `round((1 - scaled_probability) * 100)`.
  raw suspicious_probability와 raw probability 기반 predicted_label/threshold는 그대로다.
- Network: 유사 리뷰 수가 0이고 최대 유사도가 0.50 이하이면 unavailable(-1).
  계산된 feature/비교 개수는 보존하고 Network reasons는 비운다.
  0.50 초과의 연속 점수와 강한 유사도 기준 0.85는 그대로다.
- Meta: 50/30/20 중 가용 신호만 재정규화. safe >=70, warn >=40, danger <40 유지.
- contract_version=`v0.5`, model_version=`ptext-koelectra-v1-2epoch-20260929` 유지.
  새 작업의 policy_version만 `rti-v0` → `rti-v0.1`.
- JSON/SSE 필드·인증·순서·500건 제한·DB 커밋 선행 정책은 유지한다.
  모든 신호 unavailable이면 rti=-1, level=null. 0점과 -1은 다르다.
- KoELECTRA 가중치·tokenizer·lazy load·cache·RLock·worker 1·Docker·배포 설정 변경 없음.
- Groq는 기존 reasons 후처리만 수행한다. 프롬프트·예산·키 fallback은 변경하지 않는다.
  빈 reasons는 그대로이며 Network 근거가 없으면 Network 코드도 만들지 않는다.

## Text 점수 예시 (모델 추론이 아닌 확률 입력)

| raw probability | rti-v0 text_score | rti-v0.1 text_score |
| --- | --- | --- |
| 0 | 100 | 100 |
| 0.002 | 100 | 96 |
| 0.13 | 87 | 72 |
| 0.5 | 50 | 50 |
| 0.8 | 20 | 33 |
| 1 | 0 | 0 |

Temperature scaling은 높은 점수뿐 아니라 낮은 점수도 50 쪽으로 완화한다.
모든 리뷰의 RTI가 일괄 하락하는 변경은 아니다.

동일 합성 리뷰 2건(크림 사용 후기 / 배송 파손 후기), raw probability=0.13을
고정하고 이전 main Network 함수와 새 함수를 실행한 결과:

| 두 리뷰 각각 | rti-v0 | rti-v0.1 |
| --- | --- | --- |
| text_score | 87 | 72 |
| behavior_score | -1 | -1 |
| network_score | 100 | -1 |
| rti | 90.7 | 72.0 |
| level / reasons | safe / [] | safe / [] |

실제 리뷰 품질 분포를 대표하는 벤치마크가 아니라 정책 차이를 보여주는 합성 비교다.

## 기존 데이터·멱등 재시도

DB schema 변경/마이그레이션/기존 행 수정/자동 재분석은 없다.
동일 Idempotency-Key와 동일 payload로 DONE 작업을 재요청하면 저장 결과를 그대로 반환한다.
새 버전의 analyzer와 Groq는 실행하지 않는다. SSE meta는 저장된 metadata_json 버전을 사용하므로
과거 `rti-v0` 결과를 `rti-v0.1`로 표시하지 않는다.

SSE 작업은 `ai_analysis_idempotency.metadata_json` 및 meta의 policy_version으로 구분한다.
Data는 request_id, ai_job_id와 함께 meta의 contract/model/policy 버전을 보관해야 한다.
일반 JSON 응답/기존 ai_analysis_jobs result_json에는 정책 버전 필드가 없다.
JSON-only 과거 행의 버전을 점수나 현재 서버 버전만으로 확정해서는 안 된다.
별도 배포 이력·작업 출처가 없는 행은 버전 미상으로 취급한다. 이번에 응답 필드는 추가하지 않는다.

Data에 저장된 과거 점수는 배포만으로 갱신되지 않는다. 새 정책으로 재처리하려면:

1. Data/AI 담당자가 대상 상품·원본 리뷰 집합·비용·롤백 기준을 합의한다.
2. 동일 상품의 비교 집합을 보존한 별도 논리 작업과 **새 Idempotency-Key**를 만든다.
3. 새 결과와 policy_version을 기존 결과와 별도로 보관하고 비교한다.
4. done 수신과 저장 검증 후 Data 측 게시 결과를 승인된 범위에서 교체한다.

기존 key를 삭제하거나 DB 점수를 직접 덮어써 재분석을 유도하지 않는다.
500건 초과 집합을 임의 분할하면 Network 비교 집합이 바뀌므로 별도 합의가 필요하다.
이 재처리 계획은 제안이며 이번 작업에서는 실행하지 않는다.

## 문서 해석

[기존 SSE 문서](data-analysis-stream.md)의 rti-v0 meta 예시는 최초 구현 당시 버전이다.
신규 작업은 이 문서의 rti-v0.1을 사용하고 과거 replay는 저장 버전을 따른다.
과거 통합 검증 기록과 고정 점수 모의 JSON은 당시 맥락을 보존한다.

로컬 실행 결과는 [검증 기록](validation/rti-policy-v01-20261009.md)을 참조한다.
