"""Model-free checks for the optional and strict local-only v3.1 B loader."""

from contextlib import nullcontext
from hashlib import sha256
import json
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.analyzers import p_text


@pytest.fixture(autouse=True)
def clear_candidate_cache():
    p_text._candidate_predictor.cache_clear()
    yield
    p_text._candidate_predictor.cache_clear()


@pytest.fixture
def package(tmp_path, monkeypatch):
    settings = {
        "threshold": 0.5,
        "threshold_space": "raw_probability",
        "raw_probability_threshold": 0.5,
        "score_temperature": 2.0,
        "max_length": 256,
        "runtime_temperature_2_is_display_only": True,
        "dataset_sha256": p_text._V31B_DATASET_SHA256,
        "calibration_temperature": 0.09259259259259259,
    }
    (tmp_path / "inference_config.json").write_text(json.dumps(settings), encoding="utf-8")
    fake_weights = b"synthetic checkpoint used only by this test"
    (tmp_path / "model.safetensors").write_bytes(fake_weights)
    monkeypatch.setattr(p_text, "V31B_MODEL_SHA256", sha256(fake_weights).hexdigest())
    return tmp_path, settings


class Tensor:
    def __init__(self, values):
        self.values = values

    def __getitem__(self, index):
        if index == (slice(None), 1):
            return self
        assert index == (0, 1)
        return Tensor(self.values[0])

    def to(self, device):
        assert device == "cpu"
        return self

    def item(self):
        return self.values

    def tolist(self):
        return self.values


@pytest.fixture
def backend(monkeypatch):
    probabilities = {"first": .4, "boundary": .5, "third": .8}

    def tokenize(text, **options):
        assert options == dict(return_tensors="pt", truncation=True, max_length=256,
                               **({"padding": True} if isinstance(text, list) else {}))
        return {"input_ids": Tensor(text if isinstance(text, list) else [text])}

    def forward(input_ids):
        if "broken" in input_ids.values:
            raise RuntimeError("Synthetic tensor backend failure")
        return SimpleNamespace(logits=Tensor([probabilities[text] for text in input_ids.values]))

    tokenizer = Mock(side_effect=tokenize)
    model = Mock(side_effect=forward)
    model.to.return_value = model
    model.config.id2label = {0: "NORMAL", 1: "SUSPICIOUS"}
    load_model = Mock(return_value=model)
    load_tokenizer = Mock(return_value=tokenizer)
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False),
                        inference_mode=nullcontext, softmax=lambda logits, dim: logits))
    monkeypatch.setitem(sys.modules, "transformers", SimpleNamespace(
        AutoModelForSequenceClassification=SimpleNamespace(from_pretrained=load_model),
        AutoTokenizer=SimpleNamespace(from_pretrained=load_tokenizer),
    ))
    return load_model, load_tokenizer, model, tokenizer


def test_candidate_path_is_explicit_and_does_not_fall_back_to_v1(monkeypatch):
    monkeypatch.setenv("PTEXT_MODEL_PATH", "must-not-use-v1-as-candidate")
    monkeypatch.delenv("PTEXT_V31B_MODEL_PATH", raising=False)
    with pytest.raises(ValueError, match="PTEXT_V31B_MODEL_PATH"):
        p_text.load_candidate_predictor()


def test_candidate_env_does_not_switch_production_default(monkeypatch):
    monkeypatch.delenv("PTEXT_MODEL_PATH", raising=False)
    monkeypatch.setenv("PTEXT_V31B_MODEL_PATH", "must-not-use-candidate-for-v1")
    predictor = Mock()
    loader = Mock(return_value=predictor)
    monkeypatch.setattr(p_text, "_predictor", loader)
    p_text.predict_text_scores(["synthetic review"])
    loader.assert_called_once_with(p_text.DEFAULT_MODEL_PATH)


def test_v1_rejects_candidate_bundle_even_through_legacy_env(package, backend, monkeypatch, caplog):
    path, _ = package
    load_model, load_tokenizer, _, _ = backend
    monkeypatch.setenv("PTEXT_MODEL_PATH", str(path))
    expected = p_text.unavailable_result()
    assert p_text.predict_text_score("first") == expected
    assert p_text.predict_text_scores(["first"]) == [expected]
    assert "requires the offline loader" in caplog.text
    load_model.assert_not_called()
    load_tokenizer.assert_not_called()


def test_candidate_uses_shared_real_batch_path_raw_threshold_and_display_temperature(package, backend, monkeypatch):
    path, _ = package
    load_model, load_tokenizer, model, tokenizer = backend
    monkeypatch.setenv("PTEXT_V31B_MODEL_PATH", str(path))
    predictor = p_text.load_candidate_predictor()
    assert p_text.load_candidate_predictor(path) is predictor  # resolved env and explicit path share the cache
    load_tokenizer.assert_called_once_with(path, local_files_only=True, trust_remote_code=False)
    load_model.assert_called_once_with(path, local_files_only=True, trust_remote_code=False, use_safetensors=True)
    model.to.assert_called_once_with("cpu")
    model.eval.assert_called_once_with()

    contents = ["first", "", "boundary", None, "third", "first"]
    results = predictor.predict_text_scores(contents, batch_size=3)
    assert [call.args[0] for call in tokenizer.call_args_list] == [["first", "boundary", "third"], ["first"]]
    assert model.call_count == 2  # one forward per tensor batch, not one per review
    assert results == [predictor.predict_text_score(text) for text in contents]
    assert results[0] == dict(text_score=55, suspicious_probability=.4, predicted_label="NORMAL")
    assert results[2] == dict(text_score=50, suspicious_probability=.5, predicted_label="SUSPICIOUS")
    assert results[1] == results[3] == p_text.unavailable_result()


def test_candidate_does_not_treat_failed_batch_as_available_or_silently_return_sentinels(package, backend):
    path, _ = package
    predictor = p_text.load_candidate_predictor(path, device="cpu")
    with pytest.raises(RuntimeError, match="v3.1 B tensor batch inference failed"):
        predictor.predict_text_scores(["first", "broken"])


@pytest.mark.parametrize("key,value", [
    ("threshold", .7), ("threshold_space", "calibrated_probability"),
    ("raw_probability_threshold", .4), ("score_temperature", 0.09259259259259259),
    ("max_length", 512), ("runtime_temperature_2_is_display_only", False), ("dataset_sha256", "different"),
])
def test_candidate_rejects_unsupported_inference_settings_before_model_load(package, backend, key, value):
    path, settings = package
    settings[key] = value
    (path / "inference_config.json").write_text(json.dumps(settings), encoding="utf-8")
    with pytest.raises(ValueError, match="Unexpected v3.1 B inference configuration"):
        p_text.load_candidate_predictor(path)
    backend[0].assert_not_called()


def test_candidate_rejects_incorrect_checkpoint_sha_before_model_load(package, backend):
    path, _ = package
    (path / "model.safetensors").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        p_text.load_candidate_predictor(path)
    backend[0].assert_not_called()


def test_candidate_refuses_reversed_class_index_mapping(package, backend):
    path, _ = package
    backend[2].config.id2label = {0: "SUSPICIOUS", 1: "NORMAL"}
    with pytest.raises(ValueError, match="Unexpected model label mapping"):
        p_text.load_candidate_predictor(path)
