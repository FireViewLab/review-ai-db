# P_text batch parity test plan

운영 저장소 이식 기준: review-ai-new `74b923c5e3c61116f312c4aeb6cbc907e9a7f558`.
이 저장소에서는 단위 테스트 경로가 `tests/team_core/`이며 실제 배치 검증은
`python scripts/validate_ptext_batch_parity.py --output parity-report.json`으로 실행한다.
Golden을 새로 생성하지 않고 참조한다. 아래 생성 절차는 원본 기준선 수집 도구의 설명이다.

## 목적과 기준선

이 검증의 목적은 모델 성능이나 사람이 부여한 정답을 평가하는 것이 아니라, 현재 `app.analyzers.p_text.predict_text_score()`의 단건 추론 결과를 향후 배치 추론에서도 보존하는 것이다. 기준 모델은 `ptext-koelectra-v1-2epoch-20260929`이며 `NORMAL=0`, `SUSPICIOUS=1`이다. Golden 값은 현재 단건 함수를 순차 호출해 생성하며 수작업으로 보정하지 않는다.

## 필수 parity 조건

- 같은 입력의 `predicted_label`이 같아야 한다.
- 같은 입력의 `text_score`가 같아야 한다.
- 빈 문자열, whitespace-only 문자열 및 지원되는 비문자열 입력의 unavailable 처리(`text_score=-1`)가 같아야 한다.
- 입력 순서와 반환 결과 순서가 같아야 한다.
- 한 batch에 같은 `content`가 여러 번 있으면 각 항목이 해당 단건 결과와 같아야 한다.
- 향후 integration 검증에서는 각 리뷰의 최종 RTI와 `level`도 기존 단건 orchestration 경로의 결과와 같아야 한다.

`suspicious_probability`는 부동소수점 연산 결과이므로 처음부터 exact equality나 임의의 `1e-6` tolerance를 계약으로 고정하지 않는다. 같은 하드웨어·소프트웨어 환경에서 single과 batch를 반복 실행해 항목별 absolute delta의 실제 최댓값을 기록하고, 관측값과 재현성에 근거해 tolerance를 결정한다. 환경이 달라지면 tolerance를 다시 측정한다. Threshold 인접 샘플은 label 반전 위험이 있으므로 probability delta와 label을 함께 검증한다.

## Tokenization 조건

Batch에서는 길이가 다른 입력을 묶기 위해 padding 방식이 달라질 수 있다. 각 리뷰에 적용되는 tokenizer, truncation 활성화 여부, `max_length=256` 및 기타 모델 입력 설정은 단건 경로와 같아야 한다. Padding token이 실제 sequence representation에 영향을 주지 않도록 현재 tokenizer가 만든 `attention_mask`를 모델에 그대로 전달해야 한다. 긴 fixture로 truncation 동작을 별도 확인한다.

## 현재 자료와 실행 절차

1. `tests/fixtures/ptext_batch_parity_cases.json`의 구조, 고유 `case_id`, unavailable 및 duplicate 사례를 단위 테스트로 검사한다.
2. `python scripts/generate_ptext_batch_parity_golden.py`로 현재 단건 결과를 생성한다. 분석 가능한 입력 중 하나라도 inference unavailable이면 파일을 쓰지 않고 실패한다.
3. 향후 batch API가 생기면 fixture 전체를 한 번에 전달하고, 순서대로 golden과 label/score/unavailable을 비교한다. Probability tolerance는 먼저 observed delta를 측정한 뒤 추가한다.
4. 서비스 integration에서는 동일 입력을 기존 단건 경로와 새 batch 경로에 전달해 최종 RTI와 level까지 비교한다.

Threshold 인접 후보는 다음처럼 원본 CSV를 읽기 전용으로 탐색한다.

```powershell
python scripts/find_ptext_threshold_edge_cases.py input.csv --limit 1000
```

탐색기는 `inference_config.json`의 threshold를 읽고, 검사한 행 가운데 threshold 미만 최인접 1건과 threshold 이상 최인접 1건을 출력한다. `--limit`은 읽는 data row 수의 상한이며 기본값은 1000이다. `--output candidates.json`을 지정해 별도 파일로 저장할 수도 있다. 이 도구는 fixture를 수정하지 않으며 후보의 fixture 편입은 사람이 개인정보와 목적 적합성을 검토한 뒤 수행한다.

Windows pytest 실행에는 기존 임시 폴더를 재사용하거나 변경하지 않고 매번 새로운 project-local basetemp를 사용한다.

```powershell
python -m pytest tests/team_core/test_ptext_batch_parity_assets.py -q -p no:cacheprovider --basetemp=.tmp_pytest_batch_parity_01
python -m pytest tests/team_core/test_ptext_runtime.py -q -p no:cacheprovider --basetemp=.tmp_pytest_batch_parity_02
```
