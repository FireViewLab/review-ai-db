"""Compose a Korean paragraph from reviewed Evidence v3 combinations, without an LLM."""

from dataclasses import dataclass


SUMMARY_VERSION = "python-evidence-summary-v1"
LENGTH = {
    "MEANINGFUL_TEXT_0_9_STRONG_PENALTY": ("short", "의미 있는 본문이 10자 미만이라 길이 감점이 크게 반영됐습니다."),
    "MEANINGFUL_TEXT_10_29_GENERAL_PENALTY": ("medium", "의미 있는 본문이 10~29자라 길이 감점이 반영됐습니다."),
    "MEANINGFUL_TEXT_30_PLUS_NO_PENALTY": ("long", "의미 있는 본문이 30자 이상이라 길이 감점은 없었습니다."),
}
PHOTO = {"IMAGE_PRESENT_VERIFIED": "present", "IMAGE_ABSENT_VERIFIED": "zero", "IMAGE_COUNT_UNKNOWN": "unknown"}
RATING = {"RATING_FIVE_VERIFIED": "five", "RATING_BELOW_FIVE_VERIFIED": "below_five", "RATING_UNKNOWN": "unknown"}
EXPERIENCE = {"EXPERIENCE_EVIDENCE_YES": "yes", "EXPERIENCE_EVIDENCE_NO": "no", "EXPERIENCE_EVIDENCE_UNCERTAIN": "uncertain"}
PROMOTION = {"PROMOTIONAL_STYLE_DETECTED": "detected", "PROMOTIONAL_STYLE_NOT_DETECTED": "not_detected",
             "PROMOTIONAL_STYLE_UNCERTAIN": "uncertain"}
PHOTO_CONTEXT = {"present": "첨부 사진이 확인됐고", "zero": "첨부 사진은 0장이고", "unknown": "첨부 사진 개수는 확인되지 않았고"}
RATING_CONTEXT = {"five": "별점은 5점으로 검증됐습니다.", "below_five": "검증된 별점은 5점 미만입니다.",
                  "unknown": "검증된 별점 정보는 없습니다."}


@dataclass(frozen=True, slots=True)
class SummaryEvidence:
    length: str
    length_ko: str
    photo: str
    rating: str
    experience: str
    promotion: str
    decision: str


def _evidence_from_codes(codes: tuple[str, ...]) -> SummaryEvidence | None:
    # Only the five observation families and one final decision are currently
    # reviewed. A new signal, duplicated/conflicting observation, or unrecognized
    # code must not silently disappear behind an otherwise familiar summary.
    if len(codes) != 6 or len(set(codes)) != 6:
        return None
    observations = set(codes[:-1])
    matched = [observations.intersection(group) for group in (LENGTH, PHOTO, RATING, EXPERIENCE, PROMOTION)]
    if any(len(group) != 1 for group in matched):
        return None
    length, photo, rating, experience, promotion = (next(iter(group)) for group in matched)
    band, length_ko = LENGTH[length]
    return SummaryEvidence(band, length_ko, PHOTO[photo], RATING[rating], EXPERIENCE[experience], PROMOTION[promotion], codes[-1])


def _context(e: SummaryEvidence) -> str:
    if e.photo == e.rating == "unknown":
        return "사진 개수와 별점 정보는 확인되지 않았습니다."
    return f"{PHOTO_CONTEXT[e.photo]} {RATING_CONTEXT[e.rating]}"


def compose_policy_summary(codes: tuple[str, ...]) -> str | None:
    """Explain a recorded decision; never compute a new score or assign a tier."""
    e = _evidence_from_codes(codes)
    if e is None:
        return None

    # These are eligibility guards for the prose, not a replacement for evaluate_policy_v3.
    # In particular, keep each compound policy condition within a single statement.
    ultrashort = e.photo == "zero" and e.rating == "five" and e.length == "short"
    detailed = (e.photo == "zero" and e.length == "long" and e.rating == "below_five"
                and e.experience == "yes" and e.promotion == "not_detected")

    if e.decision == "VERIFIED_PHOTO_AND_NO_PROMOTIONAL_STYLE" and e.photo == "present" and e.promotion == "not_detected":
        basis = "사진이 확인되고 정책에서 찾는 홍보성 표현 조합이 감지되지 않아 두 조건이 함께 긍정 근거로 반영됐습니다."
        rating_gap = " 별점 정보는 확인되지 않았습니다." if e.rating == "unknown" else ""
        return f"{basis} {e.length_ko}{rating_gap} 사진만으로 실제 구매나 사용이 확인된 것은 아닙니다."

    if e.decision == "VERIFIED_NO_PHOTO_ULTRASHORT_FIVE_STAR" and ultrashort and e.promotion != "detected":
        return ("첨부 사진이 0장이고 별점이 5점으로 검증됐으며 의미 있는 본문이 10자 미만이라는 세 조건이 함께 나타나 "
                "정책상 주의 근거로 반영됐습니다. 짧은 본문에는 길이 감점도 크게 반영됐지만, "
                "이 조합만으로 리뷰가 허위라고 판단할 수는 없습니다.")

    if e.decision == "PHOTO_EVIDENCE_UNKNOWN" and e.photo == "unknown" and e.promotion == "not_detected":
        context = ("사진 개수와 별점 정보는 확인되지 않았고, 사진 조건은 판정에 사용하지 않았습니다."
                   if e.rating == "unknown" else
                   f"{RATING_CONTEXT[e.rating]} 사진 개수는 확인되지 않아 판정에 사용하지 않았습니다.")
        return f"{context} {e.length_ko}"

    if e.decision == "SAFE_REQUIREMENTS_INCOMPLETE" and e.photo == "zero" and e.promotion == "not_detected" and not (ultrashort or detailed):
        return (f"{_context(e)} {e.length_ko} "
                "이 리뷰에서는 긍정 구간에 필요한 정책 조건이 모두 충족됐다고 확인하기 어려웠습니다.")

    if e.decision == "NO_PHOTO_DETAILED_EXPERIENCE_SAFE" and detailed:
        return ("첨부 사진은 없지만, 의미 있는 본문이 30자 이상이고 본문에 구체적인 사용 경험을 설명한 표현이 있으며 "
                "검증된 별점이 5점 미만입니다. 정책에서 찾는 홍보성 표현 조합도 감지되지 않아 "
                "이 조건들이 함께 긍정 근거가 됐습니다. 30자 이상이므로 길이 감점은 적용하지 않았습니다.")

    if e.decision == "PROMOTIONAL_STYLE_DANGER_CANDIDATE" and e.promotion == "detected":
        return ("본문에 정책이 찾는 홍보성 표현 조합이 감지돼 주의할 근거로 반영됐습니다. "
                f"{_context(e)} {e.length_ko} 이 표현만으로 광고나 허위 리뷰라고 단정할 수는 없습니다.")

    uncertain = (e.decision == "PHOTO_SAFE_BLOCKED_BY_PROMOTIONAL_UNCERTAINTY" and e.photo == "present"
                 or e.decision == "PROMOTIONAL_UNCERTAINTY_FORCES_WARN" and e.photo != "present" and not ultrashort)
    if uncertain and e.promotion == "uncertain":
        basis = ("사진이 확인됐어도 일부 표현의 홍보성 여부가 불분명해 긍정 구간을 적용하지 않았습니다."
                 if e.photo == "present" else "일부 표현의 홍보성 여부가 불분명해 주의 구간으로 분류했습니다.")
        context = RATING_CONTEXT[e.rating] if e.photo == "present" else _context(e)
        return f"{basis} {context} {e.length_ko} 홍보성 여부가 불분명하다는 것만으로 광고라고 볼 수는 없습니다."

    return None


def summary_preview(codes: tuple[str, ...], rules_reasons_ko: tuple[str, ...]) -> dict:
    paragraph = compose_policy_summary(codes)
    return {"analysis_summary_ko": paragraph, "rules_reasons_ko": list(rules_reasons_ko),
            "display_source": "analysis_summary_ko" if paragraph else "rules_reasons_ko"}
