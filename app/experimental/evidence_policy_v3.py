"""Offline evidence tier v3 and ordinal index; never used by the v0.5 API.

Adapt the policy shipped in the 2026-11-16 integrated candidate and
review-ai-new b37d5ac. Keep the existing scorer and Network calculations as
inputs, rather than importing the candidate's single-review orchestration.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass
from math import isfinite
from typing import Literal

from app.analyzers.network import (
    CLUSTER_SIZE,
    SIMILARITY_SCORE_FLOOR,
    NetworkAnalysisResult,
)
from app.scoring.meta_scorer import classify_rti


EvidenceLevel = Literal["SAFE", "WARN", "DANGER"]
EvidenceValue = Literal["YES", "NO", "UNCERTAIN"]
NetworkState = Literal[
    "NO_PEERS",
    "INSUFFICIENT_TEXT",
    "COMPARISON_SUFFICIENT_NO_PATTERN",
    "WEAK_ABOVE_FLOOR",
    "STRONG_PAIR",
    "STRONG_CLUSTER",
]
_NETWORK_STATES = {
    "NO_PEERS", "INSUFFICIENT_TEXT", "COMPARISON_SUFFICIENT_NO_PATTERN",
    "WEAK_ABOVE_FLOOR", "STRONG_PAIR", "STRONG_CLUSTER",
}
POLICY_INDEX_METHOD = "BALANCED_INTERIOR_V1"
POLICY_INDEX_TYPE = "ORDINAL_V3_POLICY_INDEX"
POLICY_INDEX_INTERPRETATION = "ORDINAL_V3_POLICY_INDEX_NOT_PROBABILITY"


def _compile(*patterns: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(pattern, re.IGNORECASE) for pattern in patterns)


# Same expression pairs and Unicode letter/number counting as the supplied policy.
_EXPERIENCE_CONTEXT = _compile(
    r"(?:직접\s*)?(?:사용|착용|복용|설치|조립)(?:해|하|했|한|해\s*보|해봤|해보|하고|하며|중)",
    r"(?:직접\s*)?(?:써|입어|신어|먹어|마셔|발라|바르고)(?:\s*보|봤|보니|보니까|보았|난\s*후)",
    r"(?:구매|배송|수령)\s*(?:후|하고|받고)",
)
_EXPERIENCE_DETAIL = _compile(
    r"(?:며칠|\d+\s*(?:일|주|개월|달|시간)|한\s*달|두\s*달)\s*(?:동안|째|간)?",
    r"(?:출근|퇴근|운동|여행|외출|회사|학교|집|요리|세탁|산책|잠들기\s*전|아침|오전|오후|저녁)(?:할|하|에서|때|용)",
    r"(?:발림성|지속력|착용감|밀착감|흡수력|보온성|크기|사이즈|무게|향|맛|질감|자극|건조|소음|배터리|세척|조립|마감|두께|핏|색감)",
    r"(?:장점|단점|아쉽|불편|편했|편하|하지만|다만|반면|처음에는|나중에는)",
    r"(?:사용|착용|복용|설치)\s*(?:전|후)|전보다|기존(?:에|의)?|다른\s*제품|전에\s*쓰던|보다\s*(?:좋|나쁘|편|불편)",
)
_MARKETING = _compile(
    r"혁신적인\s*기술력", r"독자적인\s*포뮬러", r"압도적인\s*성능",
    r"차별화된\s*솔루션", r"프리미엄\s*기술", r"획기적인\s*효과",
)
_HYPE = _compile(
    r"무조건\s*(?:구매|사야|추천)", r"단연\s*최고", r"절대\s*후회하지",
    r"완벽한\s*효과(?:를)?\s*보장", r"누구에게나\s*반드시\s*추천",
    r"사용하면\s*무조건\s*효과", r"반드시\s*효과를?\s*(?:봅니다|본다)",
)


def _evidence_value(
    content: str, context: tuple[re.Pattern[str], ...], detail: tuple[re.Pattern[str], ...],
) -> EvidenceValue:
    found_context = any(pattern.search(content) for pattern in context)
    found_detail = any(pattern.search(content) for pattern in detail)
    if found_context and found_detail:
        return "YES"
    return "UNCERTAIN" if found_context or found_detail else "NO"


def _finite_within(value: object, high: int) -> bool:
    return (
        isinstance(value, (int, float)) and not isinstance(value, bool)
        and 0 <= value <= high and isfinite(value)
    )


def _verified_source(source: str | None, verified: bool) -> bool:
    return verified is True and isinstance(source, str) and bool(source.strip())


@dataclass(frozen=True, slots=True)
class PolicyEvidence:
    """Inputs to the offline policy only, not a Data API request model.

    Unverified ratings/images must be supplied as None, never assumed to be
    five stars or zero photos. Availability flags describe actual analyzers;
    Behavior influences the original RTI but is not a separate v3 tier rule.
    """

    content: str
    original_rti: float
    text_predicted_label: str | None = None
    network_evidence_state: NetworkState | None = None
    rating: float | None = None
    rating_scale_max: float | None = None
    rating_source: str | None = None
    rating_source_verified: bool = False
    image_count: int | None = None
    image_count_source: str | None = None
    image_count_source_verified: bool = False
    behavior_available: bool = False
    network_available: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.content, str):
            raise ValueError("CONTENT_INVALID")
        if not _finite_within(self.original_rti, 100):
            raise ValueError("ORIGINAL_RTI_UNAVAILABLE_OR_INVALID")
        if self.text_predicted_label not in (None, "NORMAL", "SUSPICIOUS"):
            raise ValueError("TEXT_PREDICTION_LABEL_INVALID")
        if self.network_evidence_state is not None and (
            not isinstance(self.network_evidence_state, str)
            or self.network_evidence_state not in _NETWORK_STATES
        ):
            raise ValueError("NETWORK_EVIDENCE_STATE_INVALID")
        for value in (
            self.rating_source_verified, self.image_count_source_verified,
            self.behavior_available, self.network_available,
        ):
            if not isinstance(value, bool):
                raise ValueError("EVIDENCE_FLAG_INVALID")
        if self.rating is None:
            if self.rating_source_verified:
                raise ValueError("RATING_VERIFIED_WITHOUT_VALUE")
        else:
            if not _finite_within(self.rating, 5):
                raise ValueError("RATING_INVALID_OR_OUT_OF_RANGE")
            if self.rating_scale_max != 5:
                raise ValueError("RATING_SCALE_MUST_BE_VERIFIED_FIVE_POINT")
            if not _verified_source(self.rating_source, self.rating_source_verified):
                raise ValueError("RATING_PROVENANCE_NOT_VERIFIED")
        if self.image_count is None:
            if self.image_count_source_verified:
                raise ValueError("IMAGE_COUNT_VERIFIED_WITHOUT_VALUE")
        else:
            if (isinstance(self.image_count, bool) or not isinstance(self.image_count, int)
                    or self.image_count < 0):
                raise ValueError("IMAGE_COUNT_INVALID_OR_NEGATIVE")
            if not _verified_source(self.image_count_source, self.image_count_source_verified):
                raise ValueError("IMAGE_COUNT_PROVENANCE_NOT_VERIFIED")


@dataclass(frozen=True, slots=True)
class PolicyV3Result:
    original_rti: float
    original_level: str
    evidence_level_v3: EvidenceLevel
    evidence_reasons_v3: tuple[str, ...]
    policy_index_v3: float
    grade_conflict: bool
    meaningful_character_count: int
    short_text_penalty: float
    short_text_penalty_band: str
    image_evidence_state: Literal["UNKNOWN", "PRESENT", "ABSENT"]
    verified_rating_five: bool
    experience_evidence: EvidenceValue
    promotional_evidence: EvidenceValue
    strong_risk_reasons: tuple[str, ...]
    network_evidence_state: NetworkState | None
    missing_information: tuple[str, ...]
    policy_index_method: str = POLICY_INDEX_METHOD
    policy_index_type: str = POLICY_INDEX_TYPE
    policy_index_interpretation: str = POLICY_INDEX_INTERPRETATION

    def to_dict(self) -> dict[str, object]:
        """Serialize only comparison results, never the input content or provenance."""
        return asdict(self)


def network_evidence_state(result: NetworkAnalysisResult, *, peer_count: int) -> NetworkState:
    """Map the unchanged Network diagnostics to upstream's evidence states.

    No valid comparison is distinct from running a valid comparison and
    finding no pattern, even though both can have an unavailable Network score.
    """
    if isinstance(peer_count, bool) or not isinstance(peer_count, int) or peer_count < 0:
        raise ValueError("PEER_COUNT_INVALID")
    features = result.features
    if peer_count == 0:
        return "NO_PEERS"
    if features.compared_review_count == 0:
        return "INSUFFICIENT_TEXT"
    # The production analyzer already counts raw similarities >= 0.85.
    strong = features.similar_review_count or 0
    if strong >= CLUSTER_SIZE:
        return "STRONG_CLUSTER"
    if strong > 0:
        return "STRONG_PAIR"
    if features.similarity_max is not None and features.similarity_max <= SIMILARITY_SCORE_FLOOR:
        return "COMPARISON_SUFFICIENT_NO_PATTERN"
    return "WEAK_ABOVE_FLOOR"


def _length_band(count: int) -> tuple[str, str]:
    if count < 10:
        return "STRONG_0_9", "MEANINGFUL_TEXT_0_9_STRONG_PENALTY"
    if count < 30:
        return "GENERAL_10_29", "MEANINGFUL_TEXT_10_29_GENERAL_PENALTY"
    return "NONE_30_PLUS", "MEANINGFUL_TEXT_30_PLUS_NO_PENALTY"


_INDEX_BANDS = {
    "DANGER": (5.0, 35.0, 0.0, 39.0),
    "WARN": (45.0, 65.0, 40.0, 69.0),
    "SAFE": (75.0, 95.0, 70.0, 100.0),
}


def evaluate_policy_v3(data: PolicyEvidence) -> PolicyV3Result:
    """Reproduce the candidate's tier ordering and BALANCED_INTERIOR_V1 projection."""
    count = sum(unicodedata.category(char)[0] in {"L", "N"} for char in data.content)
    band, length_reason = _length_band(count)
    experience = _evidence_value(data.content, _EXPERIENCE_CONTEXT, _EXPERIENCE_DETAIL)
    promotional = _evidence_value(data.content, _MARKETING, _HYPE)
    original_level = classify_rti(data.original_rti).value.upper()
    image_state = (
        "UNKNOWN" if data.image_count is None else "PRESENT" if data.image_count > 0 else "ABSENT"
    )
    five_star = data.rating == 5
    risks = []
    if data.text_predicted_label == "SUSPICIOUS":
        risks.append("TEXT_PREDICTION_SUSPICIOUS")
    if data.network_evidence_state in {"STRONG_PAIR", "STRONG_CLUSTER"}:
        risks.append("STRONG_NETWORK_SIMILARITY")
    reasons = [
        length_reason,
        "IMAGE_COUNT_UNKNOWN" if image_state == "UNKNOWN" else f"IMAGE_{image_state}_VERIFIED",
        "RATING_UNKNOWN" if data.rating is None else (
            "RATING_FIVE_VERIFIED" if five_star else "RATING_BELOW_FIVE_VERIFIED"
        ),
        f"EXPERIENCE_EVIDENCE_{experience}",
        {
            "YES": "PROMOTIONAL_STYLE_DETECTED",
            "UNCERTAIN": "PROMOTIONAL_STYLE_UNCERTAIN",
            "NO": "PROMOTIONAL_STYLE_NOT_DETECTED",
        }[promotional],
        *risks,
    ]

    level: EvidenceLevel
    if original_level == "DANGER":
        level, decision = "DANGER", "ORIGINAL_RTI_DANGER_PRESERVED"
    elif promotional == "YES":
        level, decision = "DANGER", "PROMOTIONAL_STYLE_DANGER_CANDIDATE"
    elif image_state == "ABSENT" and five_star and count < 10:
        level, decision = "DANGER", "VERIFIED_NO_PHOTO_ULTRASHORT_FIVE_STAR"
    elif image_state == "PRESENT":
        if risks:
            level, decision = "WARN", "PHOTO_SAFE_BLOCKED_BY_STRONG_RISK"
        elif promotional == "NO":
            level, decision = "SAFE", "VERIFIED_PHOTO_AND_NO_PROMOTIONAL_STYLE"
        else:
            level, decision = "WARN", "PHOTO_SAFE_BLOCKED_BY_PROMOTIONAL_UNCERTAINTY"
    elif risks:
        level, decision = "WARN", "DETAILED_SAFE_BLOCKED_BY_STRONG_RISK"
    elif (image_state == "ABSENT" and count >= 30 and data.rating is not None and not five_star
          and experience == "YES" and promotional == "NO"):
        level, decision = "SAFE", "NO_PHOTO_DETAILED_EXPERIENCE_SAFE"
    else:
        level = "WARN"
        if promotional == "UNCERTAIN":
            decision = "PROMOTIONAL_UNCERTAINTY_FORCES_WARN"
        elif image_state == "UNKNOWN":
            decision = "PHOTO_EVIDENCE_UNKNOWN"
        else:
            decision = "SAFE_REQUIREMENTS_INCOMPLETE"
    reasons.append(decision)

    anchor_low, anchor_high, band_low, band_high = _INDEX_BANDS[level]
    base = anchor_low + (anchor_high - anchor_low) * (float(data.original_rti) / 100.0)
    penalty = 6.0 * (max(0.0, 30.0 - count) / 30.0)
    index = round(min(max(base - penalty, band_low), band_high), 6)
    missing = tuple(name for name, absent in (
        ("rating", data.rating is None),
        ("image_count", data.image_count is None),
        ("behavior_evidence", not data.behavior_available),
        ("network_evidence", not data.network_available),
    ) if absent)
    return PolicyV3Result(
        original_rti=float(data.original_rti),
        original_level=original_level,
        evidence_level_v3=level,
        evidence_reasons_v3=tuple(reasons),
        policy_index_v3=index,
        grade_conflict=original_level != level,
        meaningful_character_count=count,
        short_text_penalty=round(penalty, 6),
        short_text_penalty_band=band,
        image_evidence_state=image_state,
        verified_rating_five=five_star,
        experience_evidence=experience,
        promotional_evidence=promotional,
        strong_risk_reasons=tuple(risks),
        network_evidence_state=data.network_evidence_state,
        missing_information=missing,
    )
