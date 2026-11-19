"""Closed-set Korean review paragraph candidates for the local Evidence v3 experiment."""

from dataclasses import dataclass


SUMMARY_CANDIDATE_VERSION = "fixed-policy-summary-v1"


@dataclass(frozen=True)
class SummaryRule:
    evidence: frozenset[str]
    decision_ko: str
    endings_ko: tuple[str, str]


# This pilot is intentionally limited to the four reviewed profiles. Matching the
# whole evidence set prevents an unexpected or contradictory signal from disappearing
# behind an otherwise familiar decision code. Extend only with reviewed wording.
RULES = {
    "VERIFIED_PHOTO_AND_NO_PROMOTIONAL_STYLE": SummaryRule(
        frozenset({"MEANINGFUL_TEXT_0_9_STRONG_PENALTY", "IMAGE_PRESENT_VERIFIED", "RATING_FIVE_VERIFIED",
                   "EXPERIENCE_EVIDENCE_NO", "PROMOTIONAL_STYLE_NOT_DETECTED"}),
        "사진이 확인되고 정책에서 찾는 홍보성 표현 조합이 감지되지 않아 두 조건이 함께 긍정 근거로 반영됐습니다.",
        ("다만 사진만으로 실제 구매나 사용을 확인할 수는 없습니다.",
         "본문은 짧아 구체적인 사용 경험까지는 파악하기 어렵습니다."),
    ),
    "VERIFIED_NO_PHOTO_ULTRASHORT_FIVE_STAR": SummaryRule(
        frozenset({"MEANINGFUL_TEXT_0_9_STRONG_PENALTY", "IMAGE_ABSENT_VERIFIED", "RATING_FIVE_VERIFIED",
                   "EXPERIENCE_EVIDENCE_NO", "PROMOTIONAL_STYLE_NOT_DETECTED"}),
        "첨부 사진이 0장이고 별점이 5점으로 확인됐으며 의미 있는 본문이 10자 미만이라는 세 조건이 함께 나타나, "
        "정책상 주의 근거로 반영됐습니다.",
        ("이 조합만으로 리뷰가 허위라고 판단할 수는 없습니다.",
         "본문에서 구체적인 사용 상황을 파악하기 어렵지만, 실제 사용 여부는 이 근거만으로 알 수 없습니다."),
    ),
    "PHOTO_EVIDENCE_UNKNOWN": SummaryRule(
        frozenset({"MEANINGFUL_TEXT_0_9_STRONG_PENALTY", "IMAGE_COUNT_UNKNOWN", "RATING_UNKNOWN",
                   "EXPERIENCE_EVIDENCE_NO", "PROMOTIONAL_STYLE_NOT_DETECTED"}),
        "사진 개수와 별점 정보는 확인되지 않았으며, 미확인 상태인 사진 조건은 판정에 사용하지 않았습니다.",
        ("사진이 없거나 별점이 낮다는 뜻은 아닙니다.",
         "본문에서 구체적인 사용 경험을 파악하기도 어렵지만, 실제 사용 여부는 이 정보만으로 알 수 없습니다."),
    ),
    "SAFE_REQUIREMENTS_INCOMPLETE": SummaryRule(
        frozenset({"MEANINGFUL_TEXT_10_29_GENERAL_PENALTY", "IMAGE_ABSENT_VERIFIED", "RATING_FIVE_VERIFIED",
                   "EXPERIENCE_EVIDENCE_NO", "PROMOTIONAL_STYLE_NOT_DETECTED"}),
        "의미 있는 본문은 10~29자에 해당해 길이 감점이 반영됐습니다. "
        "이 리뷰에서는 긍정 구간에 필요한 정책 조건이 모두 충족됐다고 확인하기 어려웠습니다.",
        ("이는 리뷰가 허위이거나 제품에 문제가 있다는 판정이 아닙니다.",
         "본문에 구체적인 사용 경험이 드러나지 않지만, 실제 사용 여부까지 알 수는 없습니다."),
    ),
}


def approved_summary_options(codes: tuple[str, ...]) -> tuple[str, ...]:
    """Render vetted whole paragraphs or decline if the Evidence profile differs."""
    if not codes or len(set(codes)) != len(codes):
        return ()
    # The evidence policy emits the decision after the supporting observations.
    evidence, decision = codes[:-1], codes[-1]
    rule = RULES.get(decision)
    if rule is None or frozenset(evidence) != rule.evidence:
        return ()
    return tuple(f"{rule.decision_ko} {ending}" for ending in rule.endings_ko)


def select_approved_summary(codes: tuple[str, ...], proposed: str) -> str | None:
    """Return the Python-owned candidate, never an edited or novel provider string."""
    return next((option for option in approved_summary_options(codes) if option == proposed), None)
