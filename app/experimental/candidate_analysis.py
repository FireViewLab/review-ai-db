"""Offline v3.1 B analysis; keep RTI, policy and optional website preview separate."""

from datetime import datetime
import os
from typing import Annotated, Callable, Self

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from app.analyzers.network import NetworkAnalysisResult
from app.experimental.evidence_policy_v3 import PolicyEvidence, evaluate_policy_v3, network_evidence_state
from app.schemas.analysis import to_review_response
from app.scoring.meta_scorer import classify_rti
from app.services.analysis import ReviewAnalysisInput, analyze_product_reviews

CANDIDATE_FLAG = "ENABLE_AI_V3_CANDIDATE"
WEB_PREVIEW_FLAG = "ENABLE_AI_V3_WEB_PREVIEW"
Identifier = Annotated[str, Field(min_length=1)]


class LocalInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class ObservedBehavior(LocalInput):
    """Caller must supply observed values only, never a display nickname as user_id."""

    user_id: Identifier | None = None
    review_date: datetime | None = None
    verified_purchase: StrictBool | None = None
    account_created_at: datetime | None = None
    user_review_dates: tuple[datetime, ...] | None = None


class CandidateReview(LocalInput):
    platform: Identifier
    product_id: Identifier
    review_id: Identifier
    content: str
    rating: Annotated[float, Field(strict=True, ge=0, le=5)] | None = None
    rating_scale_max: Annotated[float, Field(strict=True, gt=0)] | None = None
    rating_source: str | None = None
    rating_source_verified: StrictBool = False
    image_count: Annotated[int, Field(strict=True, ge=0)] | None = None
    image_count_source: str | None = None
    image_count_source_verified: StrictBool = False
    behavior: ObservedBehavior | None = None

    @field_validator("platform", "product_id", "review_id")
    @classmethod
    def reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Identifiers must not be blank")
        return value

    @model_validator(mode="after")
    def require_provenance(self) -> Self:
        if (self.rating is None and self.rating_source_verified
                or self.image_count is None and self.image_count_source_verified):
            raise ValueError("A missing value cannot be marked as verified")
        if self.rating is not None and not (
            self.rating_source_verified and (self.rating_source or "").strip() and self.rating_scale_max == 5
        ):
            raise ValueError("A supplied rating requires a verified source and five-point scale; otherwise use null")
        if self.image_count is not None and not (
            self.image_count_source_verified and (self.image_count_source or "").strip()
        ):
            raise ValueError("A supplied photo count requires a verified source; otherwise use null")
        return self


class CandidateBatch(LocalInput):
    reviews: tuple[CandidateReview, ...] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_group(self) -> Self:
        keys = {(item.platform, item.product_id) for item in self.reviews}
        if len(keys) != 1:
            raise ValueError("All reviews must share platform and product_id")
        if len({item.review_id for item in self.reviews}) != len(self.reviews):
            raise ValueError("Duplicate review_id")
        return self


def require_candidate_enabled() -> None:
    if os.getenv(CANDIDATE_FLAG) != "1":
        raise RuntimeError(f"Local candidate is disabled; set {CANDIDATE_FLAG}=1 explicitly")


def analyze_candidate_batch(
    batch: CandidateBatch, *, text_predictor: Callable[[list[str]], list[dict]],
) -> dict:
    """Local Python entry point only. No HTTP route, MySQL, SSE or Groq calls."""
    require_candidate_enabled()
    return analyze_policy_batch(batch, text_predictor=text_predictor,
                                preview_enabled=os.getenv(WEB_PREVIEW_FLAG) == "1", require_text=True)


def analyze_policy_batch(batch: CandidateBatch, *, text_predictor: Callable[[list[str]], list[dict]],
                         preview_enabled: bool = False, require_text: bool = False) -> dict:
    """Shared pure policy pipeline; caller owns model selection and service authorization."""
    observations: dict[str, tuple[dict, NetworkAnalysisResult]] = {}

    def observe(review_id: str, prediction: dict, network: NetworkAnalysisResult) -> None:
        observations[review_id] = (prediction, network)

    platform, product_id = batch.reviews[0].platform, batch.reviews[0].product_id
    inputs = [
        ReviewAnalysisInput(row.review_id, row.product_id, row.content,
                            **(row.behavior.model_dump() if row.behavior is not None else {}))
        for row in batch.reviews
    ]
    results = analyze_product_reviews(product_id, inputs, text_predictor=text_predictor, observe_signals=observe)
    rows = []
    for review, result in zip(batch.reviews, results, strict=True):
        prediction, network = observations[result.review_id]
        network_state = network_evidence_state(network, peer_count=len(inputs) - 1)
        original = to_review_response(result, platform=platform, review_id=result.review_id,
                                      product_id=product_id).model_dump(mode="json")
        if require_text and review.content.strip() and not result.signals.text.available:
            raise RuntimeError("Candidate inference failed for a non-empty review; no comparison published")
        row = {
            "review_id": result.review_id,
            # The source policy consumes full-precision Meta-Scorer output. The v0.5
            # serializer's one-decimal RTI and consistent grade remain distinct.
            "original_rti": result.rti,
            "original_level": result.level.value if result.level is not None else None,
            "original_v05": original,
            "available_signals": result.used_signals,
            "unavailable_signals": result.unavailable_signals,
            "policy_v3": None,
            "policy_unavailable_reason": None,
        }
        if result.rti is not None:
            policy = evaluate_policy_v3(PolicyEvidence(
                content=review.content,
                original_rti=result.rti,
                text_predicted_label=prediction["predicted_label"],
                network_evidence_state=network_state,
                rating=review.rating,
                rating_scale_max=review.rating_scale_max,
                rating_source=review.rating_source,
                rating_source_verified=review.rating_source_verified,
                image_count=review.image_count,
                image_count_source=review.image_count_source,
                image_count_source_verified=review.image_count_source_verified,
                behavior_available=result.signals.behavior.available,
                network_available=network.available,
            ))
            row["policy_v3"] = policy.to_dict()
        else:
            row["policy_unavailable_reason"] = "original_rti_unavailable"
        if preview_enabled:
            row["proposed_web_preview"] = _web_preview(original, row["policy_v3"])
        rows.append(row)
    return {"platform": platform, "product_id": product_id, "review_count": len(rows), "results": rows}


def _web_preview(original: dict, policy: dict | None) -> dict | None:
    """Proposed mapping only. Copy score fields, never mutate the original result."""
    if policy is None:
        return None
    score = policy["policy_index_v3"]
    level = policy["evidence_level_v3"].lower()
    if classify_rti(score).value != level:
        raise ValueError("Policy index and evidence level are inconsistent")
    return {
        "review_id": original["review_id"],
        "rti": score,
        "level": level,
        "text_score": original["text_score"],
        "behavior_score": original["behavior_score"],
        "network_score": original["network_score"],
        # The proposed "rti" field is NOT the original RTI or a probability.
        "score_interpretation": policy["policy_index_interpretation"],
        "reasons": [_REASON_MESSAGES.get(code, "실험 정책의 추가 판단 근거가 기록됐습니다.")
                    for code in policy["evidence_reasons_v3"]],
    }


_REASON_MESSAGES = {
    "MEANINGFUL_TEXT_0_9_STRONG_PENALTY": "의미 있는 본문이 10자 미만이라 길이 감점이 크게 반영됐습니다.",
    "MEANINGFUL_TEXT_10_29_GENERAL_PENALTY": "의미 있는 본문이 10~29자라 길이 감점이 반영됐습니다.",
    "MEANINGFUL_TEXT_30_PLUS_NO_PENALTY": "의미 있는 본문이 30자 이상이라 길이 감점을 적용하지 않았습니다.",
    "IMAGE_COUNT_UNKNOWN": "사진 개수는 확인되지 않았습니다.",
    "IMAGE_PRESENT_VERIFIED": "전달된 데이터에서 첨부 사진이 확인됐습니다.",
    "IMAGE_ABSENT_VERIFIED": "전달된 데이터에 첨부 사진이 없습니다.",
    "RATING_UNKNOWN": "검증된 별점 정보가 없습니다.",
    "RATING_FIVE_VERIFIED": "검증된 별점은 5점입니다.",
    "RATING_BELOW_FIVE_VERIFIED": "검증된 별점은 5점 미만입니다.",
    "EXPERIENCE_EVIDENCE_YES": "본문에 구체적인 사용 경험을 서술한 표현이 있습니다.",
    "EXPERIENCE_EVIDENCE_NO": "구체적인 사용 경험을 서술한 표현을 찾지 못했습니다.",
    "EXPERIENCE_EVIDENCE_UNCERTAIN": "본문 표현만으로는 구체적인 경험 근거가 불분명합니다.",
    "PROMOTIONAL_STYLE_DETECTED": "본문에서 홍보성 표현 조합이 감지됐습니다.",
    "PROMOTIONAL_STYLE_UNCERTAIN": "일부 표현의 홍보성 여부가 불분명합니다.",
    "PROMOTIONAL_STYLE_NOT_DETECTED": "정책이 찾는 홍보성 표현 조합은 감지되지 않았습니다.",
    "TEXT_PREDICTION_SUSPICIOUS": "텍스트 모델에서 의심 신호가 감지됐습니다.",
    "STRONG_NETWORK_SIMILARITY": "같은 상품의 다른 리뷰와 높은 본문 유사도가 관측됐습니다.",
    "ORIGINAL_RTI_DANGER_PRESERVED": "기존 방식의 RTI가 위험 구간이라 정책에서도 위험 구간을 유지했습니다.",
    "PROMOTIONAL_STYLE_DANGER_CANDIDATE": "감지된 홍보성 표현 조합이 정책의 위험 조건에 해당합니다.",
    "VERIFIED_NO_PHOTO_ULTRASHORT_FIVE_STAR": "확인된 사진은 없고, 별점 5점에 본문이 10자 미만입니다.",
    "PHOTO_SAFE_BLOCKED_BY_STRONG_RISK": "사진이 있어도 강한 의심 신호로 인해 긍정 구간을 적용하지 않았습니다.",
    "VERIFIED_PHOTO_AND_NO_PROMOTIONAL_STYLE": "확인된 사진이 있고 정책상 홍보성 표현 조합이 감지되지 않았습니다.",
    "PHOTO_SAFE_BLOCKED_BY_PROMOTIONAL_UNCERTAINTY": "사진이 있지만 홍보성 여부가 불분명해 주의 구간으로 분류했습니다.",
    "DETAILED_SAFE_BLOCKED_BY_STRONG_RISK": "강한 의심 신호가 있어 긍정 구간을 적용하지 않았습니다.",
    "NO_PHOTO_DETAILED_EXPERIENCE_SAFE": "사진 없이도 상세한 경험 표현과 검증된 별점이 정책 조건을 충족했습니다.",
    "PROMOTIONAL_UNCERTAINTY_FORCES_WARN": "홍보성 여부가 불분명해 주의 구간으로 분류했습니다.",
    "PHOTO_EVIDENCE_UNKNOWN": "사진 정보를 확인할 수 없어 해당 조건을 판정에 사용하지 않았습니다.",
    "SAFE_REQUIREMENTS_INCOMPLETE": "긍정 구간의 정책 조건을 모두 확인하기 어렵습니다.",
}
