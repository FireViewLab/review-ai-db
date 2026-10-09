"""팀원 분석기 회귀 테스트 원본을 통합 경로에 맞춰 실행한다."""
"""상품 단위 analysis service orchestration 테스트."""

import pytest

from app.integrations.sentiment import SentimentResult, SentimentStatus
from app.services.team_analysis import ReviewAnalysisInput, analyze_product_reviews


def review(
    review_id: str,
    content: str,
    *,
    product_id: str = "product-1",
    verified_purchase: bool | None = None,
) -> ReviewAnalysisInput:
    return ReviewAnalysisInput(
        review_id=review_id,
        product_id=product_id,
        content=content,
        verified_purchase=verified_purchase,
    )


class TrackingSimilarityAdapter:
    """호출 방향을 기록하며 지정 유사도를 반환하는 테스트 adapter."""

    def __init__(self, similarity: float | None) -> None:
        self.similarity = similarity
        self.calls: list[tuple[str, str]] = []

    def calculate(self, left: str, right: str) -> float | None:
        self.calls.append((left, right))
        return self.similarity


class AvailableSentimentAdapter:
    """주입 여부를 확인할 수 있는 sentiment 테스트 adapter."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def analyze(self, content: str) -> SentimentResult:
        self.calls.append(content)
        return SentimentResult(
            status=SentimentStatus.AVAILABLE,
            score=0.0,
            magnitude=0.0,
        )


def test_single_review_connects_text_to_meta_scorer() -> None:
    result = analyze_product_reviews(
        "product-1",
        (review("review-1", "충분히 긴 일반 리뷰 본문입니다. 만족스럽게 사용했습니다."),),
    )[0]

    assert result.signals.text.available is True
    assert result.signals.behavior.available is False
    assert result.signals.network.available is False
    assert result.rti == result.signals.text.score
    assert result.used_signals == ("text",)
    assert result.used_signal_count == 1


def test_unavailable_behavior_is_not_scored_as_zero() -> None:
    results = analyze_product_reviews(
        "product-1",
        (
            review("review-1", "서로 다른 충분히 긴 첫 번째 리뷰 본문입니다."),
            review("review-2", "완전히 별개인 충분히 긴 두 번째 후기 내용입니다."),
        ),
    )

    assert all(result.signals.behavior.score is None for result in results)
    assert all(result.rti == 100.0 for result in results)
    assert all("behavior" not in result.applied_weights for result in results)


def test_two_reviews_are_each_others_network_comparison() -> None:
    adapter = TrackingSimilarityAdapter(0.0)
    inputs = (
        review("review-1", "충분한 길이를 가진 첫 번째 리뷰 본문입니다."),
        review("review-2", "비교 가능한 길이를 가진 두 번째 리뷰 본문입니다."),
    )

    results = analyze_product_reviews(
        "product-1",
        inputs,
        similarity_adapter=adapter,
    )

    assert len(results) == 2
    assert adapter.calls == [
        ("충분한 길이를 가진 첫 번째 리뷰 본문입니다.", "비교 가능한 길이를 가진 두 번째 리뷰 본문입니다."),
        ("비교 가능한 길이를 가진 두 번째 리뷰 본문입니다.", "충분한 길이를 가진 첫 번째 리뷰 본문입니다."),
    ]
    assert all(not result.signals.network.available for result in results)


def test_duplicate_content_network_signal_affects_final_result() -> None:
    content = "동일한 리뷰 본문이 반복됩니다. 내용도 충분히 깁니다."
    results = analyze_product_reviews(
        "product-1",
        (review("review-1", content), review("review-2", content)),
    )

    assert all(result.signals.network.score == 9.1 for result in results)
    assert all("network" in result.used_signals for result in results)
    assert all(result.rti == pytest.approx(74.02857142857142) for result in results)


def test_mixed_product_ids_raise_error() -> None:
    with pytest.raises(ValueError, match="product_id must match"):
        analyze_product_reviews(
            "product-1",
            (
                review("review-1", "첫 리뷰"),
                review("review-2", "다른 상품", product_id="product-2"),
            ),
        )


def test_input_order_is_preserved() -> None:
    inputs = tuple(
        review(review_id, f"{review_id}의 서로 다른 충분히 긴 리뷰 본문입니다.")
        for review_id in ("review-3", "review-1", "review-2")
    )

    results = analyze_product_reviews("product-1", inputs)

    assert tuple(result.review_id for result in results) == (
        "review-3",
        "review-1",
        "review-2",
    )


def test_analyzer_reasons_preserve_source_code_and_message() -> None:
    content = "정말 최고 최고입니다. 한 달 사용하고 만족했습니다!!!"
    results = analyze_product_reviews(
        "product-1",
        (
            review("review-1", content, verified_purchase=False),
            review("review-2", content),
        ),
    )
    reasons = results[0].reasons

    assert ("text", "REPETITIVE_KEYWORD") in {
        (reason.source, reason.code) for reason in reasons
    }
    assert ("behavior", "PURCHASE_NOT_VERIFIED") in {
        (reason.source, reason.code) for reason in reasons
    }
    assert ("network", "SIMILAR_REVIEW_PATTERN") in {
        (reason.source, reason.code) for reason in reasons
    }
    assert all(reason.message for reason in reasons)


def test_analysis_works_without_sentiment_adapter() -> None:
    result = analyze_product_reviews(
        "product-1",
        (review("review-1", "sentiment adapter 없이 분석하는 리뷰입니다."),),
    )[0]

    assert result.signals.text.available is True
    assert result.rti is not None


def test_sentiment_and_custom_similarity_adapters_can_be_injected() -> None:
    sentiment = AvailableSentimentAdapter()
    similarity = TrackingSimilarityAdapter(0.25)
    inputs = (
        review("review-1", "첫 번째 adapter 주입 테스트 본문입니다."),
        review("review-2", "두 번째 adapter 주입 테스트 본문입니다."),
    )

    results = analyze_product_reviews(
        "product-1",
        inputs,
        sentiment_analyzer=sentiment,
        similarity_adapter=similarity,
    )

    assert sentiment.calls == [review.content for review in inputs]
    assert len(similarity.calls) == 2
    assert all(not result.signals.network.available for result in results)
