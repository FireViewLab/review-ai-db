import json
from collections import Counter

from app.analyzers import p_text
from scripts import find_ptext_threshold_edge_cases as edge_finder
from scripts import generate_ptext_batch_parity_golden as generator


def test_parity_fixture_has_required_structure_and_cases():
    cases = generator.load_cases(generator.DEFAULT_CASES)

    assert 16 <= len(cases) <= 18
    assert len({case["case_id"] for case in cases}) == len(cases)
    assert any(case["content"] == "" for case in cases)
    assert any(isinstance(case["content"], str) and case["content"] and not case["content"].strip() for case in cases)
    assert any(case["content"] is None for case in cases)
    duplicates = Counter(
        case["content"] for case in cases
        if isinstance(case["content"], str) and case["content"].strip()
    )
    assert any(count >= 2 for count in duplicates.values())


def test_checked_in_single_golden_matches_fixture_structure():
    cases = generator.load_cases(generator.DEFAULT_CASES)
    golden = json.loads(generator.DEFAULT_OUTPUT.read_text(encoding="utf-8"))

    assert [row["case_id"] for row in golden] == [case["case_id"] for case in cases]
    assert all(
        set(row) == {"case_id", "text_score", "suspicious_probability", "predicted_label"}
        for row in golden
    )
    by_id = {row["case_id"]: row for row in golden}
    assert by_id["DUPLICATE_01_A"] | {"case_id": "DUPLICATE_01_B"} == by_id["DUPLICATE_01_B"]
    assert all(
        by_id[case_id]["predicted_label"] == "SUSPICIOUS"
        for case_id in ("MODEL_SUSPICIOUS_01", "MODEL_SUSPICIOUS_02")
    )
    for case_id in ("EMPTY_01", "WHITESPACE_01", "NULL_01"):
        assert by_id[case_id] == {"case_id": case_id, **p_text.unavailable_result()}


def test_generator_calls_current_single_inference_in_fixture_order(monkeypatch, tmp_path):
    calls = []

    def fake_predict(content, model_path=None):
        calls.append((content, model_path))
        if not isinstance(content, str) or not content.strip():
            return p_text.unavailable_result()
        return p_text.probability_result(0.25)

    monkeypatch.setattr(generator.p_text, "predict_text_score", fake_predict)
    output = tmp_path / "golden.json"
    cases = generator.load_cases(generator.DEFAULT_CASES)

    results = generator.generate_golden(generator.DEFAULT_CASES, output, model_path="test-model")

    assert calls == [(case["content"], "test-model") for case in cases]
    assert [row["case_id"] for row in results] == [case["case_id"] for case in cases]
    assert json.loads(output.read_text(encoding="utf-8")) == results
    assert all(set(row) == {"case_id", "text_score", "suspicious_probability", "predicted_label"} for row in results)


def test_generator_does_not_write_golden_when_inference_is_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(generator.p_text, "predict_text_score", lambda content, model_path=None: p_text.unavailable_result())
    output = tmp_path / "golden.json"

    try:
        generator.generate_golden(generator.DEFAULT_CASES, output)
    except RuntimeError as error:
        assert "golden was not written" in str(error)
    else:
        raise AssertionError("expected unavailable inference to stop golden generation")
    assert not output.exists()


def test_edge_finder_uses_configured_threshold_and_respects_limit(monkeypatch, tmp_path):
    csv_path = tmp_path / "reviews.csv"
    csv_path.write_text(
        "content,ignored\nlow,x\nnear below,x\nnear above,x\ncloser but beyond limit,x\n",
        encoding="utf-8",
    )
    probabilities = {"low": 0.1, "near below": 0.49, "near above": 0.51,
                     "closer but beyond limit": 0.499}

    def fake_predict(content, model_path=None):
        return p_text.probability_result(probabilities[content], threshold=0.5)

    monkeypatch.setattr(edge_finder.p_text, "predict_text_score", fake_predict)
    # The production model is externally mounted, not checked into this repository.
    config_path = tmp_path / "inference_config.json"
    config_path.write_text(json.dumps({"threshold": 0.5}), encoding="utf-8")
    threshold = edge_finder.configured_threshold(config_path)
    result = edge_finder.find_candidates(
        csv_path,
        limit=3,
        content_column="content",
        encoding="utf-8",
        threshold=threshold,
    )

    assert threshold == 0.5
    assert result["rows_read"] == result["rows_inferred"] == 3
    assert result["closest_below"]["content"] == "near below"
    assert result["closest_at_or_above"]["content"] == "near above"
