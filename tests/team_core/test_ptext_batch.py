"""Model-free tensor batching, memory bounds, ordering and service scope checks."""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock
from time import sleep
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.analyzers import p_text
from app.services import analysis


class Tensor:
    def __init__(self, values):
        self.values = values

    def to(self, device):
        assert device == "cpu"
        return self

    def __getitem__(self, index):
        assert index == (slice(None), 1)
        return self

    def tolist(self):
        return self.values


def backend():
    predictor = object.__new__(p_text.TextScorePredictor)
    predictor.settings = {"threshold": .5, "max_length": 256}
    predictor.device = "cpu"
    calls = []
    active = []

    @contextmanager
    def inference_mode():
        active.append(True)
        try:
            yield
        finally:
            active.pop()

    def tokenize(contents, **kwargs):
        assert isinstance(contents, list)
        assert kwargs == dict(return_tensors="pt", padding=True, truncation=True, max_length=256)
        calls.append(list(contents))
        return {"input_ids": Tensor([len(text) for text in contents]),
                "attention_mask": Tensor([1 for _ in contents])}

    def model(*, input_ids, attention_mask):
        assert active and len(input_ids.values) == len(attention_mask.values)
        return SimpleNamespace(logits=Tensor([.13 if value % 2 else .8 for value in input_ids.values]))

    predictor.tokenizer = tokenize
    predictor.model = model
    predictor.torch = SimpleNamespace(inference_mode=inference_mode,
                                     softmax=lambda logits, dim: logits)
    return predictor, calls


@pytest.mark.parametrize("batch_size", [1, 8, 16, 32])
@pytest.mark.parametrize("count", [7, 8, 9, 15, 16, 17])
def test_batched_tensors_restore_order_duplicates_and_invalid(batch_size, count):
    predictor, calls = backend()
    contents = ["abc", "", None, "  ", "abcd", "abc", "long" * 300] + ["x" * (i+1) for i in range(count)]
    result = predictor.predict_text_scores(contents, batch_size=batch_size)
    assert len(result) == len(contents)
    for content, row in zip(contents, result, strict=True):
        expected = p_text.probability_result(.13 if len(content) % 2 else .8) if content and content.strip() else p_text.unavailable_result()
        assert row == expected
    assert result[0] == result[5]
    assert all(1 <= len(call) <= batch_size for call in calls)
    assert [content for call in calls for content in call] == [text for text in contents if text and text.strip()]


@pytest.mark.parametrize("size", [0, -1, 33, True, 1.5, "8"])
def test_batch_size_is_bounded(size):
    with pytest.raises(ValueError):
        p_text.predict_text_scores(["valid"], batch_size=size)


def test_invalid_inputs_do_not_load_model(monkeypatch):
    loader = Mock(side_effect=AssertionError("No load for invalid input"))
    monkeypatch.setattr(p_text, "_predictor", loader)
    assert p_text.predict_text_scores(["", None, " ", 12]) == [p_text.unavailable_result()] * 4
    loader.assert_not_called()


def test_batch_cache_first_load_once_and_environment(monkeypatch):
    p_text._predictor.cache_clear()
    predictor, _ = backend()
    factory = Mock(return_value=predictor)
    monkeypatch.setattr(p_text, "TextScorePredictor", factory)
    monkeypatch.setenv("PTEXT_MODEL_PATH", "batch-model")
    monkeypatch.setenv("PTEXT_BATCH_SIZE", "16")
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            rows = list(pool.map(p_text.predict_text_scores, [["abc", "abcd"]] * 8))
        factory.assert_called_once_with("batch-model")
        assert all(result == rows[0] for result in rows)
        assert p_text.configured_batch_size() == 16
    finally:
        p_text._predictor.cache_clear()


def test_single_and_batch_calls_share_the_same_inference_lock(monkeypatch):
    barrier = Barrier(8)
    counter_lock = Lock()
    state = {"active": 0, "peak": 0, "single": 0, "batch": 0}

    def infer(kind, count):
        with counter_lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
            state[kind] += 1
        try:
            # Release the GIL so concurrent entry would be observable without the lock.
            sleep(.03)
            predictions = [p_text.probability_result(.13) for _ in range(count)]
            return predictions[0] if kind == "single" else predictions
        finally:
            with counter_lock:
                state["active"] -= 1

    predictor = SimpleNamespace(
        predict_text_score=lambda content: infer("single", 1),
        predict_text_scores=lambda contents, batch_size: infer("batch", len(contents)),
    )
    loader = Mock(return_value=predictor)
    monkeypatch.setattr(p_text, "_predictor", loader)

    def call(index):
        barrier.wait(timeout=5)
        if index % 2:
            return p_text.predict_text_score("single input", model_path="shared-model")
        return p_text.predict_text_scores(["batch input", "another input"], batch_size=8,
                                          model_path="shared-model")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(call, range(8)))
    assert state == {"active": 0, "peak": 1, "single": 4, "batch": 4}
    assert loader.call_count == 8
    assert all(call.args == ("shared-model",) for call in loader.call_args_list)
    for index, result in enumerate(results):
        assert result == (p_text.probability_result(.13) if index % 2 else
                          [p_text.probability_result(.13)] * 2)


@pytest.mark.parametrize("error", [FileNotFoundError("missing"), RuntimeError("backend unavailable"),
                                  ValueError("invalid model settings")])
@pytest.mark.parametrize("explicit_path", [None, "explicit-batch-model"])
def test_public_batch_loader_failure_preserves_unavailable_and_path(
    monkeypatch, caplog, error, explicit_path,
):
    monkeypatch.setenv("PTEXT_MODEL_PATH", "configured-batch-model")
    monkeypatch.setenv("PTEXT_BATCH_SIZE", "8")
    loader = Mock(side_effect=error)
    monkeypatch.setattr(p_text, "_predictor", loader)
    result = p_text.predict_text_scores(["private review content", "", None, "other input"],
                                       model_path=explicit_path)
    assert result == [p_text.unavailable_result()] * 4
    loader.assert_called_once_with(explicit_path or "configured-batch-model")
    assert "P_text batch inference unavailable" in caplog.text
    assert "private review content" not in caplog.text


@pytest.mark.parametrize("raw", ["", "0", "-1", "33", "8.0", "not-an-integer"])
def test_invalid_environment_batch_size_fails_before_model_load(monkeypatch, raw):
    monkeypatch.setenv("PTEXT_BATCH_SIZE", raw)
    loader = Mock(side_effect=AssertionError("Invalid configuration must not load the model"))
    monkeypatch.setattr(p_text, "_predictor", loader)
    with pytest.raises(ValueError, match="between 1 and 32"):
        p_text.predict_text_scores(["valid input"])
    loader.assert_not_called()


@pytest.mark.parametrize("raw,expected", [(None, 8), ("1", 1), ("32", 32)])
def test_environment_default_and_boundary_batch_sizes(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("PTEXT_BATCH_SIZE", raising=False)
    else:
        monkeypatch.setenv("PTEXT_BATCH_SIZE", raw)
    predictor = Mock()
    predictor.predict_text_scores.return_value = [p_text.probability_result(.13)]
    monkeypatch.setattr(p_text, "_predictor", Mock(return_value=predictor))
    assert p_text.predict_text_scores(["valid"]) == [p_text.probability_result(.13)]
    predictor.predict_text_scores.assert_called_once_with(["valid"], batch_size=expected)


def test_failed_chunk_keeps_completed_chunks_without_retry():
    predictor, calls = backend()
    original = predictor.tokenizer

    def tokenize(contents, **kwargs):
        if "fail" in contents:
            raise RuntimeError("synthetic backend failure")
        return original(contents, **kwargs)

    predictor.tokenizer = tokenize
    result = predictor.predict_text_scores(["abc", "fail", "xyz"], batch_size=1)
    assert result == [p_text.probability_result(.13), p_text.unavailable_result(), p_text.probability_result(.13)]
    assert calls == [["abc"], ["xyz"]]


@pytest.mark.parametrize("bad_probabilities", [[.13], [.13, .13, .13], [.13, float("nan")], [.13, 1.1]])
def test_malformed_chunk_does_not_discard_neighbors_or_publish_partial_results(bad_probabilities):
    predictor, calls = backend()
    original_model = predictor.model
    model_calls = []

    def model(**encoded):
        result = original_model(**encoded)
        model_calls.append(encoded["input_ids"].values)
        return SimpleNamespace(logits=Tensor(bad_probabilities)) if len(model_calls) == 2 else result

    predictor.model = model
    contents = ["abc", "abcd", "fault", "faulty", "xyz", "zzzz"]
    result = predictor.predict_text_scores(contents, batch_size=2)
    assert result == [p_text.probability_result(.13), p_text.probability_result(.8),
                      p_text.unavailable_result(), p_text.unavailable_result(),
                      p_text.probability_result(.13), p_text.probability_result(.8)]
    assert calls == [contents[:2], contents[2:4], contents[4:]]
    assert len(model_calls) == 3


def test_service_batches_text_once_but_keeps_whole_network_set(monkeypatch):
    rows = [analysis.ReviewAnalysisInput(str(i), "p", "충분한 길이의 리뷰 본문입니다 " + str(i)) for i in range(17)]
    text = Mock(return_value=[p_text.probability_result(.13) for _ in rows])
    network = Mock(wraps=analysis.analyze_network_batch)
    monkeypatch.setattr(analysis, "predict_text_scores", text)
    monkeypatch.setattr(analysis, "analyze_network_batch", network)
    assert len(analysis.analyze_product_reviews("p", rows)) == 17
    text.assert_called_once_with([row.content for row in rows])
    assert len(network.call_args.args[0]) == 17
    network.assert_called_once()


def test_service_rejects_prediction_count_mismatch(monkeypatch):
    monkeypatch.setattr(analysis, "predict_text_scores", lambda contents: [])
    with pytest.raises(ValueError):
        analysis.analyze_product_reviews("p", [analysis.ReviewAnalysisInput("r", "p", "리뷰 본문")])
