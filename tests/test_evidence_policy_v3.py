"""Verify offline policy branches and evidence provenance without an external model."""

from dataclasses import asdict

import pytest

from app.analyzers.network import NetworkReview, analyze_network, analyze_network_batch
from app.experimental.evidence_policy_v3 import (
    PolicyEvidence,
    evaluate_policy_v3,
    network_evidence_state,
)


DETAILED = "2주 동안 직접 사용했고 오후에는 조금 건조했지만 발림성과 지속력은 괜찮았습니다."
PROMOTIONAL = "혁신적인 기술력으로 완벽한 효과를 보장합니다. 누구에게나 반드시 추천합니다."


def evidence(content=DETAILED, *, rating=None, images=None, **overrides):
    """Verifiable synthetic facts only; unknown is distinct from actual zero."""
    metadata = {}
    if rating is not None:
        metadata.update(rating_scale_max=5, rating_source="synthetic", rating_source_verified=True)
    if images is not None:
        metadata.update(image_count_source="synthetic", image_count_source_verified=True)
    return PolicyEvidence(
        content=content, original_rti=overrides.pop("original_rti", 55),
        rating=rating, image_count=images, **(metadata | overrides),
    )


@pytest.mark.parametrize(
    "content,rating,images,expected,decision",
    [
        (DETAILED, None, None, "WARN", "PHOTO_EVIDENCE_UNKNOWN"),
        (DETAILED, 4, 0, "SAFE", "NO_PHOTO_DETAILED_EXPERIENCE_SAFE"),
        (DETAILED, None, 0, "WARN", "SAFE_REQUIREMENTS_INCOMPLETE"),
        (DETAILED, 4, None, "WARN", "PHOTO_EVIDENCE_UNKNOWN"),
        ("좋아요", 5, 0, "DANGER", "VERIFIED_NO_PHOTO_ULTRASHORT_FIVE_STAR"),
        ("좋아요", 5, 1, "SAFE", "VERIFIED_PHOTO_AND_NO_PROMOTIONAL_STYLE"),
        ("좋아요", 5, None, "WARN", "PHOTO_EVIDENCE_UNKNOWN"),
        ("좋아요", None, 0, "WARN", "SAFE_REQUIREMENTS_INCOMPLETE"),
        ("좋아요", 3, 0, "WARN", "SAFE_REQUIREMENTS_INCOMPLETE"),
        (PROMOTIONAL, None, None, "DANGER", "PROMOTIONAL_STYLE_DANGER_CANDIDATE"),
        (PROMOTIONAL, 4, 1, "DANGER", "PROMOTIONAL_STYLE_DANGER_CANDIDATE"),
        ("혁신적인 기술력", 4, 1, "WARN", "PHOTO_SAFE_BLOCKED_BY_PROMOTIONAL_UNCERTAINTY"),
        ("무조건 추천합니다", 4, None, "WARN", "PROMOTIONAL_UNCERTAINTY_FORCES_WARN"),
    ],
)
def test_candidate_branches_preserve_original_score_and_grade(content, rating, images, expected, decision):
    inputs = evidence(content, rating=rating, images=images)
    result = evaluate_policy_v3(inputs)
    assert result.original_rti == inputs.original_rti == 55
    assert result.original_level == "WARN"
    assert result.evidence_level_v3 == expected
    assert result.grade_conflict == (expected != "WARN")
    assert result.evidence_reasons_v3[-1] == decision
    assert result.policy_index_type == "ORDINAL_V3_POLICY_INDEX"
    assert result.policy_index_interpretation == "ORDINAL_V3_POLICY_INDEX_NOT_PROBABILITY"
    low, high = {"DANGER": (0, 39), "WARN": (40, 69), "SAFE": (70, 100)}[expected]
    assert low <= result.policy_index_v3 <= high
    assert "content" not in result.to_dict()
    assert "rating_source" not in result.to_dict()


def test_original_danger_takes_priority_and_risk_order_is_stable():
    result = evaluate_policy_v3(evidence(
        PROMOTIONAL, original_rti=39.9, images=1, rating=5,
        text_predicted_label="SUSPICIOUS", network_evidence_state="STRONG_CLUSTER",
    ))
    assert result.original_rti == 39.9
    assert result.original_level == result.evidence_level_v3 == "DANGER"
    assert result.evidence_reasons_v3[-3:] == (
        "TEXT_PREDICTION_SUSPICIOUS", "STRONG_NETWORK_SIMILARITY", "ORIGINAL_RTI_DANGER_PRESERVED",
    )
    assert not result.grade_conflict


@pytest.mark.parametrize(
    "risk",
    [
        {"text_predicted_label": "SUSPICIOUS"},
        {"network_evidence_state": "STRONG_PAIR"},
        {"network_evidence_state": "STRONG_CLUSTER"},
    ],
)
def test_strong_risk_blocks_safe_but_does_not_override_explicit_danger_rules(risk):
    photo = evaluate_policy_v3(evidence(images=1, **risk))
    detailed = evaluate_policy_v3(evidence(images=0, rating=4, **risk))
    promotional = evaluate_policy_v3(evidence(PROMOTIONAL, images=1, **risk))
    no_photo_short = evaluate_policy_v3(evidence("좋아요", images=0, rating=5, **risk))
    assert photo.evidence_reasons_v3[-1] == "PHOTO_SAFE_BLOCKED_BY_STRONG_RISK"
    assert detailed.evidence_reasons_v3[-1] == "DETAILED_SAFE_BLOCKED_BY_STRONG_RISK"
    assert photo.evidence_level_v3 == detailed.evidence_level_v3 == "WARN"
    assert promotional.evidence_level_v3 == no_photo_short.evidence_level_v3 == "DANGER"


def test_unknown_fields_and_unavailable_signals_not_filled_in():
    result = evaluate_policy_v3(evidence(network_evidence_state="COMPARISON_SUFFICIENT_NO_PATTERN"))
    assert result.missing_information == ("rating", "image_count", "behavior_evidence", "network_evidence")
    assert result.network_evidence_state == "COMPARISON_SUFFICIENT_NO_PATTERN"
    assert result.image_evidence_state == "UNKNOWN"
    assert "RATING_UNKNOWN" in result.evidence_reasons_v3
    assert "IMAGE_COUNT_UNKNOWN" in result.evidence_reasons_v3
    assert not result.verified_rating_five
    verified = evaluate_policy_v3(evidence(
        rating=0, images=0, behavior_available=True, network_available=True,
        network_evidence_state="WEAK_ABOVE_FLOOR",
    ))
    assert verified.missing_information == ()
    assert verified.image_evidence_state == "ABSENT"
    assert "RATING_BELOW_FIVE_VERIFIED" in verified.evidence_reasons_v3


@pytest.mark.parametrize(
    "kwargs,message",
    [
        ({"rating": 5, "rating_scale_max": 5}, "RATING_PROVENANCE_NOT_VERIFIED"),
        ({"rating": 5, "rating_scale_max": 10, "rating_source": "synthetic", "rating_source_verified": True},
         "RATING_SCALE_MUST_BE_VERIFIED_FIVE_POINT"),
        ({"rating": True}, "RATING_INVALID_OR_OUT_OF_RANGE"),
        ({"rating": float("nan")}, "RATING_INVALID_OR_OUT_OF_RANGE"),
        ({"rating": 5.01}, "RATING_INVALID_OR_OUT_OF_RANGE"),
        ({"rating_source_verified": True}, "RATING_VERIFIED_WITHOUT_VALUE"),
        ({"image_count": 0}, "IMAGE_COUNT_PROVENANCE_NOT_VERIFIED"),
        ({"image_count": 0, "image_count_source": " ", "image_count_source_verified": True},
         "IMAGE_COUNT_PROVENANCE_NOT_VERIFIED"),
        ({"image_count": -1}, "IMAGE_COUNT_INVALID_OR_NEGATIVE"),
        ({"image_count": False}, "IMAGE_COUNT_INVALID_OR_NEGATIVE"),
        ({"image_count_source_verified": True}, "IMAGE_COUNT_VERIFIED_WITHOUT_VALUE"),
        ({"network_evidence_state": "UNSUPPORTED"}, "NETWORK_EVIDENCE_STATE_INVALID"),
        ({"text_predicted_label": "UNAVAILABLE"}, "TEXT_PREDICTION_LABEL_INVALID"),
        ({"behavior_available": 0}, "EVIDENCE_FLAG_INVALID"),
    ],
)
def test_policy_provenance_validation_is_fail_closed(kwargs, message):
    with pytest.raises(ValueError, match=message):
        PolicyEvidence(content="synthetic", original_rti=55, **kwargs)


@pytest.mark.parametrize("invalid", [-1, None, True, float("nan"), float("inf"), 100.01])
def test_missing_or_invalid_original_rti_does_not_create_a_policy_tier(invalid):
    with pytest.raises(ValueError, match="ORIGINAL_RTI_UNAVAILABLE_OR_INVALID"):
        PolicyEvidence(content="synthetic", original_rti=invalid)


@pytest.mark.parametrize(
    "length,expected_band,expected_penalty",
    [(0, "STRONG_0_9", 6), (9, "STRONG_0_9", 4.2),
     (10, "GENERAL_10_29", 4), (29, "GENERAL_10_29", .2), (30, "NONE_30_PLUS", 0)],
)
def test_meaningful_length_penalty_and_balanced_index(length, expected_band, expected_penalty):
    result = evaluate_policy_v3(evidence("가" * length + " !⭐?!", images=1))
    assert result.meaningful_character_count == length
    assert result.short_text_penalty_band == expected_band
    assert result.short_text_penalty == expected_penalty
    assert result.policy_index_v3 == 86.0 - expected_penalty
    assert result.evidence_level_v3 == "SAFE"
    assert result.policy_index_method == "BALANCED_INTERIOR_V1"


def test_length_deduction_is_clamped_only_to_the_chosen_tier():
    result = evaluate_policy_v3(evidence("⭐!", original_rti=40, images=1))
    assert result.original_level == "WARN"
    assert result.evidence_level_v3 == "SAFE"
    assert result.policy_index_v3 == 77
    danger = evaluate_policy_v3(evidence("⭐!", original_rti=0))
    assert danger.evidence_level_v3 == "DANGER"
    assert danger.policy_index_v3 == 0
    assert danger.short_text_penalty == 6


def test_balanced_index_remains_deterministic_and_monotonic_within_tier():
    results = [evaluate_policy_v3(evidence("가" * 10, original_rti=rti, images=1))
               for rti in (40, 50, 60, 70, 100)]
    scores = [result.policy_index_v3 for result in results]
    assert scores == sorted(set(scores))
    assert asdict(results[0]) == asdict(evaluate_policy_v3(evidence("가" * 10, original_rti=40, images=1)))


@pytest.mark.parametrize(
    "similarity,peer_count,expected",
    [
        (.99, 0, "NO_PEERS"),
        (.5, 1, "COMPARISON_SUFFICIENT_NO_PATTERN"),
        (.5001, 1, "WEAK_ABOVE_FLOOR"),
        (.84999, 1, "WEAK_ABOVE_FLOOR"),
        (.85, 1, "STRONG_PAIR"),
        (.85, 4, "STRONG_PAIR"),
        (.85, 5, "STRONG_CLUSTER"),
    ],
)
def test_network_state_uses_existing_comparisons_and_threshold(similarity, peer_count, expected):
    class ControlledSimilarity:
        def calculate(self, left, right):
            return similarity

    target = NetworkReview("target", "sample-product", DETAILED)
    peers = [NetworkReview(str(i), "sample-product", DETAILED) for i in range(peer_count)]
    analyzed = analyze_network(target, peers, similarity_adapter=ControlledSimilarity())
    assert network_evidence_state(analyzed, peer_count=peer_count) == expected


def test_batch_network_distinguishes_no_peers_from_insufficient_text_and_detects_duplicates():
    first = NetworkReview("first", "sample-product", DETAILED)
    duplicate = NetworkReview("second", "sample-product", DETAILED)
    sparse = NetworkReview("short", "sample-product", "⭐")
    (alone,) = analyze_network_batch([first])
    assert network_evidence_state(alone, peer_count=0) == "NO_PEERS"
    insufficient = analyze_network_batch([first, sparse])[0]
    assert not insufficient.available
    assert network_evidence_state(insufficient, peer_count=1) == "INSUFFICIENT_TEXT"
    analyzed = analyze_network_batch([first, duplicate, sparse])
    assert [network_evidence_state(row, peer_count=2) for row in analyzed] == [
        "STRONG_PAIR", "STRONG_PAIR", "INSUFFICIENT_TEXT",
    ]
