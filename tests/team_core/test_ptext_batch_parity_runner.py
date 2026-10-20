"""Parity report validation remains runnable without torch or model weights."""

from app.analyzers import p_text
from scripts.validate_ptext_batch_parity import compare_predictions, duplicate_parity


def test_probability_delta_is_measured_without_permitting_label_or_score_drift():
    expected = p_text.probability_result(.13)
    actual = {**expected, "suspicious_probability": .130001}
    report = compare_predictions([expected], [actual], ["case"])
    assert report["passed"]
    assert report["max_probability_absolute_delta"] > 0
    for field, replacement in (("text_score", 71), ("predicted_label", "SUSPICIOUS")):
        changed = compare_predictions([expected], [{**actual, field: replacement}], ["case"])
        assert not changed["passed"]
        assert changed["mismatches"][0]["field"] == field


def test_order_and_length_changes_cannot_be_hidden_by_zip():
    expected = [p_text.probability_result(.13), p_text.probability_result(.8)]
    assert not compare_predictions(expected, expected[:1], ["first", "second"])["passed"]
    assert not compare_predictions(expected, list(reversed(expected)), ["first", "second"])["passed"]


def test_unavailable_and_invalid_probability_are_reported_as_failures():
    missing = p_text.unavailable_result()
    assert compare_predictions([missing], [missing], ["empty"])["passed"]
    assert not compare_predictions([missing], [{**missing, "suspicious_probability": 0}], ["empty"])["passed"]
    available = p_text.probability_result(.13)
    for probability in (float("nan"), float("inf"), True, "0.13"):
        assert not compare_predictions([available], [{**available, "suspicious_probability": probability}], ["case"])["passed"]


def test_duplicate_comparison_matches_by_original_content_including_nonadjacent_rows():
    cases = [{"case_id": "a", "content": "동일"}, {"case_id": "empty", "content": None},
             {"case_id": "b", "content": "동일"}]
    score = p_text.probability_result(.13)
    predictions = [score, p_text.unavailable_result(), score]
    assert duplicate_parity(cases, predictions)["passed"]
    assert not duplicate_parity(cases, [score, predictions[1], p_text.probability_result(.8)])["passed"]
