"""동일 상품 리뷰 집합의 관계와 유사도를 분석해 P_network를 계산하는 모듈.

역할:
- target 리뷰를 같은 상품의 다른 리뷰와 비교한다.
- 중복·유사 리뷰 feature와 현재 v0 P_network를 계산한다.
- 유사도 계산은 adapter로 분리해 향후 한국어 embedding 모델로 교체할 수 있다.

수정 범위:
- [AI CORE]
- Spring/크롤링 연동을 위해 scoring 로직을 직접 수정하지 않는다.
- similarity 판단 또는 scoring 정책 변경은 AI 담당자와 협의한다.

주의:
- exact/normalized duplicate 감점은 Ground Truth로 검증되지 않은 v0 heuristic이다.
- similarity 결과가 없으면 임의 점수를 만들지 않으며 crawler를 직접 호출하지 않는다.
"""

from dataclasses import dataclass
from math import exp
from statistics import fmean
from typing import Protocol, Sequence

from app.integrations.similarity import (
    NormalizedTextSimilarityAdapter,
    hybrid_similarity_rows,
    is_informative_text,
)

SIMILARITY_THRESHOLD = 0.85
CLUSTER_SIZE = 5
SIMILARITY_SCORE_FLOOR = 0.50


class SimilarityAdapter(Protocol):
    """두 리뷰 본문의 유사도를 계산하는 adapter 인터페이스."""

    def calculate(self, left: str, right: str) -> float | None:
        """0~1 유사도 또는 계산 불가 시 None을 반환한다."""


@dataclass(frozen=True, slots=True)
class NetworkReview:
    """Spring이 전달하는 network 분석용 최소 리뷰 정보."""

    review_id: str
    product_id: str
    content: str


@dataclass(frozen=True, slots=True)
class NetworkFeatures:
    """실제로 계산된 리뷰 관계 feature."""

    similar_review_count: int | None
    similarity_max: float | None
    similarity_mean: float | None
    top3_mean_similarity: float | None
    compared_review_count: int


@dataclass(frozen=True, slots=True)
class NetworkReason:
    """P_network 점수에 영향을 준 판단 근거."""

    code: str
    message: str


@dataclass(frozen=True, slots=True)
class NetworkAnalysisResult:
    """P_network 점수 또는 명시적인 unavailable 상태."""

    available: bool
    p_network: float | None
    features: NetworkFeatures
    reasons: tuple[NetworkReason, ...]
    unavailable_reason: str | None

    @property
    def network_score(self) -> float:
        return self.p_network if self.p_network is not None else -1


def analyze_network(
    target: NetworkReview,
    reviews: Sequence[NetworkReview],
    *,
    similarity_adapter: SimilarityAdapter | None = None,
) -> NetworkAnalysisResult:
    """target을 같은 상품의 다른 리뷰들과 비교해 P_network를 반환한다.

    product_id가 다른 리뷰는 Spring → AI 계약 오류이므로 ``ValueError``를
    발생시킨다. review_id가 target과 같으면 자기 자신으로 보고 비교에서 제외한다.
    """

    _validate_review(target, field_name="target")
    for index, review in enumerate(reviews):
        _validate_review(review, field_name=f"reviews[{index}]")
        if review.product_id != target.product_id:
            raise ValueError(
                "all comparison reviews must have the target product_id: "
                f"expected {target.product_id!r}, got {review.product_id!r}"
            )

    candidates = [review for review in reviews if review.review_id != target.review_id]
    if not candidates:
        return _unavailable_result("no_comparison_reviews")

    if similarity_adapter is None:
        return analyze_network_batch((target, *candidates))[0]

    adapter = similarity_adapter or NormalizedTextSimilarityAdapter()
    if not is_informative_text(target.content):
        return _unavailable_result("insufficient_text_information")
    similarities: list[float] = []
    for review in candidates:
        if not is_informative_text(review.content):
            continue
        similarity = adapter.calculate(target.content, review.content)
        if similarity is None:
            continue
        if not 0.0 <= similarity <= 1.0:
            raise ValueError("similarity adapter must return a value between 0 and 1")
        similarities.append(similarity)

    if not similarities:
        return _unavailable_result("similarity_unavailable")

    return _from_similarities(similarities)


def _from_similarities(similarities):
    if not similarities:
        return _unavailable_result("similarity_unavailable")
    similar_review_count = sum(similarity >= SIMILARITY_THRESHOLD for similarity in similarities)
    top3_mean_similarity = fmean(sorted(similarities, reverse=True)[:3])
    features = NetworkFeatures(
        similar_review_count=similar_review_count,
        similarity_max=max(similarities),
        similarity_mean=fmean(similarities),
        top3_mean_similarity=top3_mean_similarity,
        compared_review_count=len(similarities),
    )

    if similar_review_count == 0 and features.similarity_max <= SIMILARITY_SCORE_FLOOR:
        return NetworkAnalysisResult(
            available=False,
            p_network=None,
            features=features,
            reasons=(),
            unavailable_reason="no_meaningful_similarity_evidence",
        )

    scale = lambda value: min(1.0, max(0.0, (value - SIMILARITY_SCORE_FLOOR) / 0.50))
    cluster_scale = 1.0 - exp(-similar_review_count / 2.0)
    score = (
        100.0
        - 55.0 * scale(features.similarity_max) ** 2
        - 30.0 * scale(top3_mean_similarity) ** 2
        - 15.0 * cluster_scale
    )
    reasons: list[NetworkReason] = []
    if similar_review_count >= CLUSTER_SIZE:
        reasons.append(
            NetworkReason(
                code="SIMILAR_REVIEW_CLUSTER",
                message="높은 본문 유사도의 리뷰 군집 탐지",
            )
        )
    elif similar_review_count >= 1:
        reasons.append(
            NetworkReason(
                code="SIMILAR_REVIEW_PATTERN",
                message="높은 본문 유사도의 리뷰 탐지",
            )
        )

    return NetworkAnalysisResult(
        available=True,
        p_network=round(min(100.0, max(score, 0.0)), 1),
        features=features,
        reasons=tuple(reasons),
        unavailable_reason=None,
    )


def analyze_network_batch(reviews: Sequence[NetworkReview]) -> tuple[NetworkAnalysisResult, ...]:
    """Fit once, reuse sparse vectors, and avoid retaining a dense pair matrix.

    Identity is review_id only; author is deliberately not part of this API.
    """
    ids = set()
    for review in reviews:
        _validate_review(review, field_name="review")
        if review.review_id in ids:
            raise ValueError("duplicate review_id")
        ids.add(review.review_id)
    if len({review.product_id for review in reviews}) > 1:
        raise ValueError("all reviews must have the same product_id")
    rows = hybrid_similarity_rows([review.content for review in reviews])
    return tuple(_from_similarities(similarities) for similarities in rows)


def _validate_review(review: NetworkReview, *, field_name: str) -> None:
    if not review.review_id.strip():
        raise ValueError(f"{field_name}.review_id must not be blank")
    if not review.product_id.strip():
        raise ValueError(f"{field_name}.product_id must not be blank")
    if not isinstance(review.content, str):
        raise TypeError(f"{field_name}.content must be a string")


def _unavailable_result(reason: str) -> NetworkAnalysisResult:
    return NetworkAnalysisResult(
        available=False,
        p_network=None,
        features=NetworkFeatures(
            similar_review_count=None,
            similarity_max=None,
            similarity_mean=None,
            top3_mean_similarity=None,
            compared_review_count=0,
        ),
        reasons=(),
        unavailable_reason=reason,
    )
