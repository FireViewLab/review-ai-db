"""Final Hybrid A P_network regression tests."""

import pytest

from app.analyzers.network import (
    NetworkReview,
    _from_similarities,
    analyze_network,
    analyze_network_batch,
)
from app.integrations.similarity import canonical_text, hybrid_similarity_rows


def review(review_id: str, content: str, *, product_id: str = "product-1") -> NetworkReview:
    return NetworkReview(review_id=review_id, product_id=product_id, content=content)


def batch(*texts: str):
    return analyze_network_batch(tuple(review(str(index), text) for index, text in enumerate(texts)))


class FixedSimilarityAdapter:
    def __init__(self, value: float | None):
        self.value = value

    def calculate(self, left: str, right: str) -> float | None:
        return self.value


def test_no_comparison_reviews_is_unavailable() -> None:
    target = review("target", "충분한 길이를 가진 실제 사용 후기 문장입니다.")
    result = analyze_network(target, ())
    assert result.network_score == -1
    assert result.unavailable_reason == "no_comparison_reviews"


@pytest.mark.parametrize("values", [[0.0], [0.2, 0.5]])
def test_low_similarity_with_valid_comparison_is_unavailable(values):
    result = _from_similarities(values)
    assert result.network_score == -1 and not result.available
    assert result.features.compared_review_count == len(values)
    assert result.features.similarity_max == max(values)
    assert result.features.similar_review_count == 0
    assert result.features.similarity_mean == pytest.approx(sum(values) / len(values))
    assert result.reasons == ()
    assert result.unavailable_reason == "no_meaningful_similarity_evidence"


def test_similarity_above_score_floor_keeps_continuous_score():
    result = _from_similarities([0.5001])
    assert result.available is True
    assert result.network_score == 100.0  # Existing rounding, not unavailable.
    assert result.reasons == ()


def test_canonical_normalization_does_not_mutate_source() -> None:
    source = " 배송  빠르고 제품도 좋아요!!! "
    assert canonical_text(source) == "배송빠르고제품도좋아요"
    assert source == " 배송  빠르고 제품도 좋아요!!! "


@pytest.mark.parametrize(("left", "right"), [
    ("2주 사용해보니 피부가 덜 건조해서 만족합니다.", "2주 사용 해보니 피부가 덜 건조해서 만족합니다!"),
    ("배송 빠르고 포장도 깔끔해요. 만족합니다.", "배송 빠르고 포장도 깔끔해요!!! 만족합니다"),
])
def test_spacing_and_punctuation_differences_are_strong(left: str, right: str) -> None:
    results = batch(left, right)
    assert results[0].features.similarity_max == 1.0
    assert results[0].features.similar_review_count == 1
    assert results[0].p_network == 9.1
    assert results[0].reasons[0].code == "SIMILAR_REVIEW_PATTERN"


def test_near_duplicate_just_below_raw_threshold_is_not_strong() -> None:
    results = batch("배송 빠르고 제품도 좋아요. 재구매할게요.", "배송도 빠르고 제품도 좋아요! 재구매 할게요.")
    assert 0.84 < results[0].features.similarity_max < 0.85
    assert results[0].p_network < 100
    assert results[0].reasons == ()


@pytest.mark.parametrize(("raw_similarity", "is_strong"), [
    (0.8499, False),
    (0.8500, True),
    (0.8501, True),
])
def test_strong_threshold_uses_unrounded_raw_similarity(
    raw_similarity: float,
    is_strong: bool,
) -> None:
    result = _from_similarities([raw_similarity])
    assert (result.features.similar_review_count == 1) is is_strong
    assert bool(result.reasons) is is_strong


@pytest.mark.parametrize(("left", "right"), [
    ("배송이 빨라서 좋았고 포장도 깔끔했습니다.", "한 달 사용해보니 피부가 조금 덜 건조해졌어요."),
    ("가격 대비 만족합니다. 배송도 빨라요.", "가격은 조금 비싸지만 사용감은 좋습니다."),
])
def test_different_reviews_do_not_create_false_positive(left: str, right: str) -> None:
    results = batch(left, right)
    assert results[0].features.similarity_max < 0.85
    assert results[0].p_network is None
    assert results[0].unavailable_reason == "no_meaningful_similarity_evidence"
    assert results[0].reasons == ()


def test_short_duplicates_are_not_cluster_evidence() -> None:
    results = batch(*(["좋아요"] * 5))
    assert all(result.network_score == -1 for result in results)
    assert all(result.reasons == () for result in results)


def test_short_review_does_not_make_long_reviews_unavailable() -> None:
    results = batch("좋아요", "배송 빠르고 제품도 좋아요. 재구매할게요.", "배송도 빠르고 제품도 좋아요! 재구매 할게요.")
    assert results[0].network_score == -1
    assert results[1].available is True
    assert results[1].features.compared_review_count == 1
    assert results[1].features.similar_review_count == 1


def test_six_long_duplicates_form_five_peer_cluster() -> None:
    text = "한 달 동안 사용했고 피부 당김이 줄어서 만족한 제품입니다."
    results = batch(*([text] * 6))
    assert all(result.features.similar_review_count == 5 for result in results)
    assert all(result.p_network == 1.2 for result in results)
    assert all(result.reasons[0].code == "SIMILAR_REVIEW_CLUSTER" for result in results)


def test_hybrid_similarity_changes_continuously() -> None:
    rows = hybrid_similarity_rows([
        "배송 빠르고 제품도 좋아요. 재구매할게요.",
        "배송도 빠르고 제품도 좋아요! 재구매 할게요.",
        "배송이 늦었지만 제품 색상은 화면과 비슷합니다.",
    ])
    assert 0 < rows[0][1] < rows[0][0] < 1


def test_custom_adapter_remains_supported_for_informative_text() -> None:
    target = review("target", "충분한 길이를 가진 실제 사용 후기 문장입니다.")
    peer = review("peer", "비교에 사용할 충분한 길이의 다른 후기 문장입니다.")
    result = analyze_network(target, (peer,), similarity_adapter=FixedSimilarityAdapter(0.9))
    assert result.available is True
    assert result.features.similar_review_count == 1


def test_mixed_product_ids_raise_contract_error() -> None:
    target = review("target", "충분한 길이를 가진 실제 사용 후기 문장입니다.")
    with pytest.raises(ValueError, match="target product_id"):
        analyze_network(target, (review("peer", "충분한 비교 후기입니다.", product_id="other"),))
