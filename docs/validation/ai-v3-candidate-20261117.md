# Re:view v3.1 B 모델·Evidence Tier v3 로컬 통합 검증

이 문서는 **v2 SSE 연결 이전의 후보 실험 기록**입니다. 현재 API·DB 연결과 GCP 관측은
[v1/v2 격리 검증 보고서](ai-v1-v2-20261117.md)를 기준으로 보세요.

- 기준: `review-ai-db`의 `origin/main` `eba4dffae611c437789c7c4c5a5fdfa17caf0a9b`, 작업 브랜치 `feat/ai-v3-candidate-integration`.
- 원천: [review-ai-new `b37d5ac`](https://github.com/FireViewLab/review-ai-new/tree/b37d5acb9faf1c770bfddaf23dcd3fa91962f252)와 2026-11-16 최종 후보 ZIP, 두 인수인계 자료.
- 범위: 로컬 실험만. 운영 웹사이트·v0.5 계약·DB·배포 환경 변경 없음.

## 하연님 검토용 핵심

합성 리뷰 12건에서 v1 → v3.1 B의 **Text와 기존 방식 RTI가 평균 41점 하락**했습니다.
원 RTI 등급은 12건 모두 SAFE → WARN으로 이동했습니다. v3.1 B에 v3 후보 정책을 별도로 적용하면
**SAFE 7 / WARN 3 / DANGER 2**이고, v3.1 B의 원 등급과 **9/12건(75%)**이 달랐습니다.
이 수치는 모델/정책의 차이일 뿐 정확도나 실서비스 분포가 아닙니다.

두 모델 모두 CPU 단건과 실제 텐서 배치에서 정수 Text 점수·라벨·리뷰 매핑이 동일했습니다.
제안된 웹 매핑은 두 플래그를 켠 로컬 결과에만 표시합니다. 원 RTI·원 등급·근거는 그대로 남으며,
미리보기 점수는 순서형 정책 지수로 표기됩니다. 운영 적용 판단은 점수 분포·규칙 검토 후 별도로 합니다.

## 구조와 변경 파일

기존 서버는 상품 단위 `analyze_product_reviews()`에서 P_text 텐서 배치, Network peer,
Behavior, Meta-Scorer를 실행합니다. 운영 JSON/SSE는 그 결과의 v0.5 직렬화, 기존 Reasons
후처리, MySQL 저장을 그대로 실행합니다.

- `app/analyzers/p_text.py`: 기존 모델 경로·캐시와 배치를 유지. 후보 전용 경로와 프로세스 캐시,
  안전한 로컬 safetensors 로딩, 설정·가중치 해시 검증을 추가했습니다.
- `app/services/analysis.py`: 실험 호출에서만 주입되는 Text batch 함수와 Network 관측 hook을
  추가했습니다. 기존 호출에는 기본 v1 예측 함수가 적용됩니다.
- `app/experimental/__init__.py`, `app/experimental/evidence_policy_v3.py`,
  `app/experimental/candidate_analysis.py`: 운영 API에서 import/등록하지 않는 로컬 정책,
  provenance DTO, 별도 오프라인 결과 및 웹 매핑. DB·SSE·Groq를 호출하지 않습니다.
- `scripts/compare_ai_v3_candidate.py`: 전달된 원본 합성 fixture에 대한 모델·정책 비교 및 성능 기록.
- `tests/team_core/test_ptext_v31b.py`, `tests/test_evidence_policy_v3.py`,
  `tests/test_ai_v3_candidate.py`: 후보 로더·배치·정책·API 격리 회귀.
- 본 보고서: 재현 방법·검증 결과·후속 합의 사항.

ZIP의 상품 묶음 어댑터는 내부에서 P_text를 건별 호출하므로 가져오지 않았습니다.
원천 commit과 후보 ZIP의 Network·통합 어댑터·v3 정책을 비교했고, ZIP의
`similarity.py`에만 추가된 진단용 `hybrid_similarity_edges()`는 활성 분석 경로에서 호출되지
않습니다. 기존 유사도·Behavior·Network·RTI 가중치/경계는 수정하지 않았습니다.
Network 근거는 기존 관측값을 사용합니다. 비교 대상 없음, 비교 가능한 본문 부족,
비교했으나 유의미 패턴 없음은 서로 다릅니다. 강한 유사도는 기존 0.85 이상의 peer이며,
1~4개는 강한 쌍, 5개 이상은 군집으로 구분합니다.

## 무결성과 모델 기준

| 파일 | SHA-256 |
| --- | --- |
| v3.1 B 모델 ZIP | `f1fc7abe2248c32a9d310834ca38687e17d72d4eb24a528045be345836e368b9` |
| 통합 코드 후보 ZIP | `80d6b59bc4649e1be53852bfcf084751e848638bdea3b949e4f225b745b7457c` |
| v3.1 B `model.safetensors` | `012b65174cdbc2e1fb36d75519438043bebd4317a69ea9997325b66bb436474b` |
| 비교에 사용한 기존 v1 `model.safetensors` | `e7677366e93afef3f26890c81fffa62e7a1db96f24cba55a19cdeaf9efb5f354` |
| 전달된 단건/배치 합성 JSON | `c9a708684e1603777c8ce6f5d7b343c0c8a5518b6c6c5c60d9841c9071ad42a4` |

두 ZIP은 전달 문서의 해시와 일치했고 후보 코드 manifest 26개 항목의 크기·해시도 일치했습니다.
모델 ZIP의 12개 항목은 사용한 압축 해제본과 바이트가 일치했습니다. ZIP 안의 실행 코드는
실행하지 않았습니다. 후보 로더는 가중치 해시를 재확인하고, 제공된 case-sensitive Electra
토크나이저·잘림 최대 256·padding/attention mask를 사용합니다. 클래스 0 NORMAL / 1 SUSPICIOUS,
raw softmax class 1에 임계값 0.5를 적용하고 **표시 점수에만 T=2.0을 한 번 적용**합니다.
문서에 있는 별도의 검증용 보정 T=0.09259259259259259는 런타임에서 쓰지 않습니다.
이미 해석 가능한 제공 모델을 로드한 검증이며 재학습·임계값 변경은 없었습니다.

## 실제 비교 결과

전달 JSON의 파일명 v1은 **fixture 버전**입니다. 이 실험은 같은 상품의 합성 리뷰 12건에서
기존 KoELECTRA v1과 v3.1 B를 나란히 실행했습니다. 두 모델 모두 이 표본에서는 Behavior와
Network 점수가 전부 `-1`이라 원 RTI와 Text가 같았습니다. Network 비교는 상품 전체의 peer로
실행했지만 강한 유사 증거가 없었습니다. 별도 테스트에서는 실제 유사 리뷰 묶음과 강한 쌍·군집
경계를 검증했습니다. 단건과 상품 배치의 Network/RTI는 비교 peer가 다를 수 있으므로 P_text
parity와 섞어서 판정하지 않습니다.

| 합성 ID | v1 Text / RTI | v3.1 B Text / 원 RTI | Evidence Tier v3 | Policy Index v3 |
| --- | ---: | ---: | --- | ---: |
| T01 | 95 | 54 | SAFE | 80.4 |
| T02 | 95 | 54 | DANGER | 15.8 |
| T03 | 96 | 54 | SAFE | 85.8 |
| T04 | 95 | 54 | SAFE | 85.6 |
| T05 | 95 | 53 | WARN | 50.4 |
| T06 | 95 | 54 | SAFE | 82.4 |
| T07 | 96 | 55 | WARN | 56.0 |
| T08 | 94 | 54 | SAFE | 81.4 |
| T09 | 95 | 54 | DANGER | 17.0 |
| T10 | 95 | 55 | WARN | 52.0 |
| T11 | 95 | 55 | SAFE | 86.0 |
| T12 | 95 | 53 | SAFE | 84.4 |

- 두 점수의 v3.1 B−v1 변화 범위는 각각 **−42 ~ −40점**입니다. v3.1 B 원 등급은 표에서
  모두 WARN이고, Policy Index는 새 등급 구간 안에서 원 RTI와 길이 감점을 반영한
  `BALANCED_INTERIOR_V1` **순서형 지수**입니다. 진위 확률·confidence·정확도가 아닙니다.
- T01: 사진 확인으로 짧은 글도 SAFE 후보가 되지만 길이 감점 유지. T02·T09: 검증된 사진 0장,
  5점, 의미 문자 10자 미만으로 DANGER. T10은 정확히 10자여서 해당 DANGER 조건에 들지 않습니다.
- T03: 5점 미만·상세 경험 표현으로 SAFE. T07은 5점으로 경험 기반 SAFE 조건이 미충족입니다.
- T05: 사진·별점은 미확인 그대로이며 사진 0장이나 5점으로 채우지 않았습니다.
- T04 포함 12건에서 홍보성 결합 검출은 모두 NO였습니다. T04의 fixture 조건은 **YES라면
  DANGER**라는 조건부 기대입니다. 이를 양성 검출 성공으로 판단하지 않았고, 결합 규칙 YES 및
  자동 SAFE 차단은 별도 합성 테스트에서 재현했습니다.
- 전달 문서의 기존 603건(600+별도 DANGER 3), 374/222/7 분포는 전달팀의 과거 요약입니다.
  원 입력·독립 라벨이 없어 여기서는 재실행하거나 실서비스 정확도로 해석하지 않았습니다.

## CPU 단건/배치와 성능

Windows Python 3.12, torch `2.8.0+cpu` / transformers `4.57.6`, intra/inter-op 1개에서
같은 입력으로 실행했습니다. 두 모델 모두 batch 1/2/4/8/12 및 순서를 섞은 batch 8에서
리뷰 ID 기준 점수·라벨·가용성 불일치 0건입니다. raw 확률 최대 절대 차이는 v1
`3.725290298461914e-9`, v3.1 B `5.960464477539063e-8`로 fixture 제안 허용차 `1e-5` 이내입니다.
기존 v1 Golden 18건도 별도로 다시 실행해 batch 1/8/16/32의 점수·라벨·RTI·등급 parity를 확인했습니다.

| P_text 12건, 한 번 실행 | v1 (초) | v3.1 B (초) | 프로세스 누적 최고 working set, v1 / v3.1 B (MiB) |
| --- | ---: | ---: | ---: |
| 단건 ×12 | 0.668 | 0.646 | 616.61 / 644.15 |
| Batch 1 | 0.578 | 0.566 | 617.98 / 644.15 |
| Batch 2 | 0.494 | 0.485 | 619.73 / 644.15 |
| Batch 4 | 0.599 | 0.611 | 623.61 / 644.15 |
| Batch 8 | 0.615 | 0.656 | 631.91 / 644.15 |
| Batch 12 | 0.662 | 0.652 | 643.97 / 648.49 |
| 순서를 섞은 Batch 8 | 0.639 | 0.632 | 644.03 / 648.49 |

모델 전 누적 최고 207.54 MiB, 전체 648.49 MiB. 모델 로딩 v1 2.044초, v3.1 B
0.311초(후보 내부 SHA 확인 포함)였습니다. 같은 프로세스에서 먼저 v1을 로드하고 참조를
해제한 뒤 v3을 로드했으므로 로딩 수치는 import/OS 캐시의 영향을 받습니다. 누적 최고치에는
이전 실행 메모리가 포함되며 batch별 독립 peak나 운영 Worker 메모리 추정은 아닙니다.
로컬 상품 분석·정책 계산까지 포함한 batch 8은 v1 0.609초, v3.1 B 0.633초였습니다.
단일 합성 실행으로 성능 개선, 운영 최대 동시 처리량이나 GPU 호환을 판단하지 않았습니다.

## 운영 호환성과 미확정 웹 매핑

- MySQL·멱등성·저장 순서·관리 중인 Reasons/Groq 경로는 그대로입니다. 기존 `rti`, `level`,
  `reasons`, `-1` 표현을 변경하거나 v3 실험 필드를 API·SSE·DB에 넣지 않았습니다.
- 로컬 결과는 원 Meta-Scorer의 반올림 전 `original_rti/original_level`과, 운영 직렬화의
  소수 한 자리 `original_v05`를 보존합니다. `original_v05.reasons`는 분석기가 만든
  코드이며 운영 Groq/중립 문장 후처리를 흉내 내지 않습니다. 기존 등급 경계는 **계속
  v0.5 반올림된 RTI**의 등급과 일치합니다. 정책 비교에는 원천 정책과 같이 반올림 전 RTI가
  들어갑니다. 이번 12건에서 원 등급과 v0.5의 v3.1 B 등급은 일치했습니다.
- `ENABLE_AI_V3_CANDIDATE=1`에서만 로컬 분석을 실행합니다. 후보 경로는
  `--v3-model-path` 또는 `PTEXT_V31B_MODEL_PATH`입니다. 운영 `PTEXT_MODEL_PATH`는 그대로이며,
  전달된 후보 모델 설정을 이 변수에 잘못 지정하면 로더가 v1로 오인하지 않도록 거부합니다.
- 추가 `ENABLE_AI_V3_WEB_PREVIEW=1`에서만 독립 `proposed_web_preview`에
  `rti ← policy_index_v3`, `level ← evidence_level_v3`(소문자), 세부 점수 그대로,
  `reasons ← evidence_reasons_v3` 한국어 문구를 넣습니다. `score_interpretation`에는
  순서형 지수임을 명시합니다. 구매·조작 사실을 단정하지 않고 Groq 없이 정해진 문구만 씁니다.
- 매핑이 v0.5 의미와 충돌하므로 운영 API에는 넣지 않았습니다. 두 플래그 모두 OFF가 기본이고,
  플래그를 실수로 API 프로세스에 넣어도 후보 로더 호출/필드 추가가 없어야 합니다.

## 실제로 수행한 검증

- Windows 전체 pytest: **665 passed, skip 없음**, 13.13초, 기존 deprecation warning 2건.
  실 MySQL은 격리 tmpfs 컨테이너로 연결. 기존 API·SSE·저장·
  멱등성·Groq 모의 후처리와 신규 후보/증거 정책/미리보기 격리 테스트 통과.
- 후보 정책 53개 집중 테스트, 기존 ZIP 정규식 21개와 정책 합성 예시 3건×12개 결과 필드
  비교(아카이브 코드 실행 없이 정적 자료 대조).
- Windows 실제 모델: v1/v3.1 B 12건 CPU 추론과 단건/배치 일치, v1 Golden 18건 재검증.
- Docker Compose config/build 및 격리 컨테이너 health. Linux CPU에서 실제 v1 모델로
  3개 합성 리뷰 JSON/공식 SSE/실험 SSE의 응답·MySQL 결과 일치, 저장된 설명 버전,
  인증, lazy health·모델 캐시, DONE replay 재분석 없음 확인. 로컬 smoke에서 두
  실험 플래그를 ON으로 둬도 기존 SSE model version은 v1였습니다.
- 실제 Groq API, Data 서버 및 GCP 운영 서버는 호출하지 않았습니다. 시험의 Groq·Data 동작은
  mock입니다. 운영 DB·볼륨·환경 설정·사이트 표시·main·배포에 변경은 없습니다.

## 재현

로컬 가상환경에 기존 `requirements-ml.txt` 의존성을 설치하고 압축을 푼 두 모델 폴더와
원본 fixture 경로를 지정합니다. 다음은 **로컬 전용** PowerShell 예시입니다.

```powershell
$env:ENABLE_AI_V3_CANDIDATE = "1"
$env:ENABLE_AI_V3_WEB_PREVIEW = "1"  # 생략하면 제안 UI 매핑은 출력되지 않음
.\.venv\Scripts\python.exe -X utf8 scripts/compare_ai_v3_candidate.py `
  --fixture "C:/external/[Test]Re_view_KoELECTRA_Single_vs_Batch_v1.json" `
  --v1-model-path "C:/external/ptext-koelectra-v1-2epoch-20260929" `
  --v3-model-path "C:/external/ptext-v3.1-b-experimental/model" `
  --output "output/ai-v3-candidate-local.json"
```

CLI는 전달된 합성 fixture의 해시를 검증하며 결과를 덮어쓰지 않습니다. `output/`의 로컬
JSON에는 입력 원문·출처 값·계정 식별자 없이 합성 케이스 ID 및 점수·근거만 담습니다.
원본 fixture·가중치·결과 JSON은 Git에 추가하지 않습니다.

## 남은 결정

- **하연님:** v3.1 B의 낮아진 표시 점수와 SAFE→WARN 이동이 의도한 결과인지,
  사진 우선 분기·강한 위험 정책 차단·T04 홍보 표현 미탐지의 기대 동작을 확인합니다.
  독립 150건/공식 Human Ground Truth, 실제 양성 광고 표현과 비교 리뷰가 충분한 상품,
  길이별 편향·최대 동시 요청은 추가 검증이 필요합니다.
- **정빈님과 추후 합의:** 별점 원천/5점 척도/검증 여부, 사진 0장과 모름(null)의 구분 및
  사진 출처·검증 여부, 실제 관측 행동 정보와 안정 사용자 ID의 전달 가능성을 확인합니다.
  모델 담당자 검토 이후 소비자·웹에서 지수 명칭/표시, 원 RTI 보존, 버전/멱등성/재처리,
  기존 데이터 호환을 협의해야 합니다. 지금은 전달 메시지 발송·계약 변경·운영 전환을 하지 않았습니다.
