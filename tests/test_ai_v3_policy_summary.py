"""Offline paragraph composition, decision consistency, and fallback tests."""

from itertools import product

import pytest

from app.experimental.candidate_analysis import _REASON_MESSAGES
from app.experimental.evidence_policy_v3 import PolicyEvidence, evaluate_policy_v3
from app.experimental.policy_summary import compose_policy_summary, summary_preview


DETAILED = "2주 동안 직접 사용했고 오후에는 조금 건조했지만 발림성과 지속력은 괜찮았습니다."
PROMOTIONAL = "혁신적인 기술력으로 완벽한 효과를 보장합니다. 누구에게나 반드시 추천합니다."


def policy(text, photo, rating, *, rti=55, **extra):
    return evaluate_policy_v3(PolicyEvidence(
        text, rti, image_count=photo, image_count_source="synthetic" if photo is not None else None,
        image_count_source_verified=photo is not None, rating=rating, rating_scale_max=5 if rating is not None else None,
        rating_source="synthetic" if rating is not None else None, rating_source_verified=rating is not None, **extra,
    ))


@pytest.mark.parametrize("text,photo,rating", product(
    ("가" * 9, "가" * 10, "가" * 29, "가" * 30, DETAILED, PROMOTIONAL, "혁신적인 기술력"), (None, 0, 1), (None, 4, 5),
))
def test_render_actual_policy_across_boundary_and_missing_data_matrix(text, photo, rating):
    result = policy(text, photo, rating)
    before = result.to_dict()
    original_reason = tuple(_REASON_MESSAGES[code] for code in result.evidence_reasons_v3)
    preview = summary_preview(result.evidence_reasons_v3, original_reason)
    paragraph = preview["analysis_summary_ko"]
    band = "10자 미만" if result.meaningful_character_count < 10 else "10~29자" if result.meaningful_character_count < 30 else "30자 이상"
    assert paragraph and "\n" not in paragraph and 2 <= paragraph.count(".") <= 4
    assert band in paragraph and "길이 감점" in paragraph
    if photo is None:
        assert "사진" in paragraph and "확인되지" in paragraph and "0장" not in paragraph
    if rating is None:
        assert "별점" in paragraph and "5점으로" not in paragraph
    if result.evidence_reasons_v3[-1] == "PHOTO_EVIDENCE_UNKNOWN":
        assert "판정에 사용하지 않았습니다" in paragraph
    if result.evidence_reasons_v3[-1] == "VERIFIED_PHOTO_AND_NO_PROMOTIONAL_STYLE":
        basis = paragraph.split(". ", 1)[0]
        assert "사진이 확인되고" in basis and "홍보성 표현 조합이 감지되지" in basis and "별점" not in basis
    if result.evidence_reasons_v3[-1] == "VERIFIED_NO_PHOTO_ULTRASHORT_FIVE_STAR":
        basis = paragraph.split(". ", 1)[0]
        assert all(term in basis for term in ("사진이 0장", "별점이 5점", "의미 있는 본문이 10자 미만", "세 조건"))
    if result.evidence_reasons_v3[-1] == "NO_PHOTO_DETAILED_EXPERIENCE_SAFE":
        assert all(term in paragraph for term in ("첨부 사진은 없지만", "구체적인 사용 경험을 설명한 표현", "검증된 별점이 5점 미만"))
    if result.evidence_reasons_v3[-1] == "SAFE_REQUIREMENTS_INCOMPLETE":
        assert "첨부 사진은 0장" in paragraph and "충족됐다고 확인하기 어려웠습니다" in paragraph
    assert result.to_dict() == before and preview["rules_reasons_ko"] == list(original_reason)
    assert preview["display_source"] == "analysis_summary_ko"


@pytest.mark.parametrize("overrides", [
    {"rti": 39}, {"text_predicted_label": "SUSPICIOUS"}, {"network_evidence_state": "STRONG_PAIR"},
    {"network_evidence_state": "STRONG_CLUSTER"},
])
def test_unreviewed_original_danger_or_strong_signals_keep_original_rules(overrides):
    result = policy(DETAILED, 1, 5, **overrides)
    rules = tuple(_REASON_MESSAGES[code] for code in result.evidence_reasons_v3)
    assert summary_preview(result.evidence_reasons_v3, rules) == {
        "analysis_summary_ko": None, "rules_reasons_ko": list(rules), "display_source": "rules_reasons_ko",
    }


def test_malformed_or_contradictory_record_is_not_summarized():
    good = policy("짧습니다", 0, 5).evidence_reasons_v3
    for invalid in ((), good[:-1], (*good, "FUTURE_CODE"), (*good[:-1], "FUTURE_DECISION"),
                    (good[0], "IMAGE_COUNT_UNKNOWN", *good[2:]), (good[0], good[1], good[1], *good[3:]),
                    (*good[:-1], "SAFE_REQUIREMENTS_INCOMPLETE")):
        assert compose_policy_summary(invalid) is None
