"""팀원 분석기 회귀 테스트 원본을 통합 경로에 맞춰 실행한다."""
"""P_network의 상품 계약, 비교 제외 및 유사도 집계 테스트."""

import pytest

from app.analyzers.network import NetworkReview, analyze_network
from app.integrations.similarity import NormalizedTextSimilarityAdapter


TARGET = NetworkReview(
    review_id="review-1",
    product_id="product-1",
    content="정말 좋은 상품이라 꾸준하게 사용하고 있습니다!",
)


class UnavailableSimilarityAdapter:
    """유사도 결과를 제공하지 못하는 테스트 adapter."""

    def calculate(self, left: str, right: str) -> float | None:
        return None


def review(review_id: str, content: str, *, product_id: str = "product-1") -> NetworkReview:
    return NetworkReview(review_id=review_id, product_id=product_id, content=content)


def test_no_comparison_reviews_is_unavailable() -> None:
    result = analyze_network(TARGET, ())

    assert result.available is False
    assert result.p_network is None
    assert result.unavailable_reason == "no_comparison_reviews"
    assert result.features.compared_review_count == 0
    assert result.features.similar_review_count is None


def test_target_itself_is_excluded() -> None:
    result = analyze_network(
        TARGET,
        (
            TARGET,
            review("review-2", "전혀 다른 실제 후기입니다."),
        ),
    )

    assert result.available is False
    assert result.features.compared_review_count == 1
    assert result.features.similar_review_count == 0


def test_normalized_duplicate_increases_similar_review_count() -> None:
    result = analyze_network(
        TARGET,
        (review("review-2", "  정말   좋은 상품이라 꾸준하게 사용하고 있습니다!  "),),
    )

    assert result.features.similar_review_count == 1
    assert result.features.similarity_max == 1.0
    assert result.p_network == 9.1


def test_no_duplicate_has_zero_count() -> None:
    result = analyze_network(
        TARGET,
        (review("review-2", "배송이 빠르고 포장이 꼼꼼합니다."),),
    )

    assert result.available is False
    assert result.features.similar_review_count == 0
    assert result.features.similarity_max < 0.5
    assert result.p_network is None
    assert result.unavailable_reason == "no_meaningful_similarity_evidence"


def test_compared_review_count_counts_only_similarity_results() -> None:
    result = analyze_network(
        TARGET,
        (
            review("review-2", TARGET.content),
            review("review-3", ""),
            review("review-4", "충분한 길이를 가진 다른 내용입니다."),
        ),
        similarity_adapter=NormalizedTextSimilarityAdapter(),
    )

    assert result.features.compared_review_count == 2
    assert result.features.similar_review_count == 1
    assert result.features.similarity_mean == 0.5


def test_mixed_product_ids_raise_contract_error() -> None:
    with pytest.raises(ValueError, match="target product_id"):
        analyze_network(
            TARGET,
            (review("review-2", "다른 상품 리뷰", product_id="product-2"),),
        )


def test_empty_target_content_is_unavailable() -> None:
    empty_target = review("review-empty", "")
    result = analyze_network(empty_target, (review("review-2", "내용 있음"),))

    assert result.available is False
    assert result.p_network is None
    assert result.unavailable_reason == "similarity_unavailable"
    assert result.features.similar_review_count is None
    assert result.features.similarity_max is None
    assert result.features.similarity_mean is None


def test_missing_similarity_is_not_replaced_with_a_number() -> None:
    result = analyze_network(
        TARGET,
        (review("review-2", "비교 가능한 충분히 긴 후기 본문입니다."),),
        similarity_adapter=UnavailableSimilarityAdapter(),
    )

    assert result.available is False
    assert result.p_network is None
    assert result.features.similar_review_count is None
    assert result.features.similarity_max is None
    assert result.features.similarity_mean is None
    assert result.features.compared_review_count == 0
