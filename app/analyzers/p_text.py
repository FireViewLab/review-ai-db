"""Local, lazy KoELECTRA inference. No downloads or training at runtime."""

from functools import lru_cache
from hashlib import file_digest
import json
import math
import logging
import os
from threading import RLock
from pathlib import Path

DEFAULT_MODEL_PATH = str(Path(__file__).resolve().parents[2] / "models/ptext-koelectra-v1-2epoch-20260929")
LOGGER = logging.getLogger(__name__)
_PREDICTION_LOCK = RLock()
# Transformer lazy imports are process-global. Serialize cold construction only;
# v1 and v3 keep distinct cached instances and do not share an inference lock.
_MODEL_LOAD_LOCK = RLock()
TEXT_SCORE_TEMPERATURE = 2.0
DEFAULT_BATCH_SIZE = 8
MAX_BATCH_SIZE = 32
V31B_MODEL_SHA256 = "012b65174cdbc2e1fb36d75519438043bebd4317a69ea9997325b66bb436474b"
_V31B_DATASET_SHA256 = "921225e36cdc3b0675cce337794e89e65a367fdfb4b28fc8172dc45bb52730f1"


def configured_batch_size(batch_size=None) -> int:
    """Bound tensor memory per process; a missing env retains the batch-8 default."""
    if batch_size is None:
        raw = os.environ.get("PTEXT_BATCH_SIZE", str(DEFAULT_BATCH_SIZE))
        try:
            batch_size = int(raw)
        except (TypeError, ValueError):
            raise ValueError("PTEXT_BATCH_SIZE must be an integer between 1 and 32") from None
    if type(batch_size) is not int or not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError("batch_size must be an integer between 1 and 32")
    return batch_size


def _temperature_scaled_probability(probability: float, temperature: float) -> float:
    """Soften score saturation without changing the raw classification probability."""
    if probability == 0.0 or probability == 1.0:
        return probability
    logit = math.log(probability / (1.0 - probability))
    return 1.0 / (1.0 + math.exp(-logit / temperature))


def probability_result(probability: float, threshold: float = .5) -> dict:
    return _probability_result(probability, threshold, TEXT_SCORE_TEMPERATURE)


def _probability_result(probability: float, threshold: float, score_temperature: float) -> dict:
    if isinstance(probability, bool) or isinstance(threshold, bool) or not math.isfinite(probability) or not 0 <= probability <= 1 or not 0 <= threshold <= 1:
        raise ValueError("Probability and threshold must be finite values in [0, 1]")
    score_probability = _temperature_scaled_probability(probability, score_temperature)
    return {"text_score": round((1 - score_probability) * 100), "suspicious_probability": probability,
            "predicted_label": "SUSPICIOUS" if probability >= threshold else "NORMAL"}


def unavailable_result():
    return {"text_score": -1, "suspicious_probability": None, "predicted_label": None}


def _read_inference_settings(path):
    settings = json.loads((path / "inference_config.json").read_text(encoding="utf-8"))
    if not isinstance(settings, dict):
        raise ValueError("inference_config.json must contain an object")
    return settings


class TextScorePredictor:
    _strict_batch_errors = False
    _safetensors_only = False

    def __init__(self, model_path=DEFAULT_MODEL_PATH, *, device=None):
        path = Path(model_path)
        self.settings = self._load_settings(path)
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer
        self.torch = torch
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        options = {"local_files_only": True}
        if self._safetensors_only:
            options["trust_remote_code"] = False
        self.tokenizer = AutoTokenizer.from_pretrained(path, **options)
        if self._safetensors_only:
            options["use_safetensors"] = True
        self.model = AutoModelForSequenceClassification.from_pretrained(
            path, **options,
        ).to(self.device)
        if self.model.config.id2label != {0: "NORMAL", 1: "SUSPICIOUS"}:
            raise ValueError("Unexpected model label mapping")
        self.model.eval()

    @staticmethod
    def _load_settings(path):
        settings = _read_inference_settings(path)
        if ("runtime_temperature_2_is_display_only" in settings
                or settings.get("dataset_sha256") == _V31B_DATASET_SHA256):
            raise ValueError("The v3.1 B candidate requires the offline loader; do not use PTEXT_MODEL_PATH")
        probability_result(0., settings["threshold"])
        if type(settings["max_length"]) is not int or not 1 <= settings["max_length"] <= 512:
            raise ValueError("Invalid inference max_length")
        return settings

    def _prediction_result(self, probability):
        return probability_result(probability, self.settings["threshold"])

    def predict_text_score(self, content):
        if not isinstance(content, str) or not content.strip():
            return unavailable_result()
        encoded = self.tokenizer(content, return_tensors="pt", truncation=True, max_length=self.settings["max_length"])
        encoded = {key: value.to(self.device) for key, value in encoded.items()}
        with self.torch.inference_mode():
            probability = self.torch.softmax(self.model(**encoded).logits, dim=-1)[0, 1].item()
        return self._prediction_result(probability)

    def predict_text_scores(self, contents, batch_size=DEFAULT_BATCH_SIZE):
        """Run actual batched tensors, restoring invalid inputs at their original indices."""
        batch_size = configured_batch_size(batch_size)
        contents = list(contents)
        results = [unavailable_result() for _ in contents]
        valid = [index for index, content in enumerate(contents)
                 if isinstance(content, str) and content.strip()]
        for start in range(0, len(valid), batch_size):
            indices = valid[start:start + batch_size]
            try:
                encoded = self.tokenizer(
                    [contents[index] for index in indices], return_tensors="pt",
                    padding=True, truncation=True, max_length=self.settings["max_length"],
                )
                encoded = {key: value.to(self.device) for key, value in encoded.items()}
                with self.torch.inference_mode():
                    probabilities = self.torch.softmax(self.model(**encoded).logits, dim=-1)[:, 1].tolist()
                if len(probabilities) != len(indices):
                    raise ValueError("Batch prediction count mismatch")
                predictions = [self._prediction_result(p) for p in probabilities]
                for index, prediction in zip(indices, predictions, strict=True):
                    results[index] = prediction
            except (OSError, RuntimeError, ValueError, ImportError, KeyError, TypeError):
                if self._strict_batch_errors:
                    raise RuntimeError("v3.1 B tensor batch inference failed") from None
                # No second inference attempt: preserve successful chunks and unavailable semantics.
                LOGGER.exception("P_text batch inference unavailable")
        return results


class _CandidateTextScorePredictor(TextScorePredictor):
    """Known v3.1 B checkpoint; no v1 metadata or fallback."""

    _strict_batch_errors = True
    _safetensors_only = True

    @staticmethod
    def _load_settings(path):
        settings = _read_inference_settings(path)
        if (settings.get("threshold_space") != "raw_probability"
                or settings.get("raw_probability_threshold") != 0.5
                or settings.get("threshold") != 0.5
                or settings.get("score_temperature") != 2.0
                or settings.get("runtime_temperature_2_is_display_only") is not True
                or type(settings.get("max_length")) is not int
                or settings["max_length"] != 256
                or settings.get("dataset_sha256") != _V31B_DATASET_SHA256):
            raise ValueError("Unexpected v3.1 B inference configuration")
        with (path / "model.safetensors").open("rb") as weights:
            if file_digest(weights, "sha256").hexdigest() != V31B_MODEL_SHA256:
                raise ValueError("v3.1 B model.safetensors SHA-256 mismatch")
        return settings

    def _prediction_result(self, probability):
        # The validation calibration temperature is not used at runtime.
        return _probability_result(probability, self.settings["raw_probability_threshold"],
                                   self.settings["score_temperature"])


@lru_cache(maxsize=1)
def _candidate_predictor(model_path, device):
    return _CandidateTextScorePredictor(model_path, device=device)


def load_candidate_predictor(model_path=None, *, device=None):
    """Explicit v3.1 B entry point. Configuration/load errors propagate."""
    path = model_path or os.environ.get("PTEXT_V31B_MODEL_PATH")
    if not path or not str(path).strip():
        raise ValueError("Set PTEXT_V31B_MODEL_PATH or pass a v3.1 B model path explicitly")
    with _MODEL_LOAD_LOCK:
        return _candidate_predictor(str(Path(path).resolve()), device)


@lru_cache(maxsize=1)
def _predictor(model_path):
    with _MODEL_LOAD_LOCK:
        return TextScorePredictor(model_path)


def predict_text_score(content, model_path=None):
    if not isinstance(content, str) or not content.strip():
        return unavailable_result()
    path = str(model_path or os.environ.get("PTEXT_MODEL_PATH") or DEFAULT_MODEL_PATH)
    # Serialize first load and inference to avoid duplicate model loads and CPU/RAM spikes.
    with _PREDICTION_LOCK:
        try:
            return _predictor(path).predict_text_score(content)
        except (OSError, RuntimeError, ValueError, ImportError, KeyError, TypeError):
            LOGGER.exception("P_text inference unavailable (model=%s)", path)
            return unavailable_result()


def predict_text_scores(contents, batch_size=None, model_path=None):
    """Return ordered predictions using the same process-local cache and inference lock."""
    batch_size = configured_batch_size(batch_size)
    contents = list(contents)
    if not any(isinstance(content, str) and content.strip() for content in contents):
        return [unavailable_result() for _ in contents]
    path = str(model_path or os.environ.get("PTEXT_MODEL_PATH") or DEFAULT_MODEL_PATH)
    with _PREDICTION_LOCK:
        try:
            return _predictor(path).predict_text_scores(contents, batch_size=batch_size)
        except (OSError, RuntimeError, ValueError, ImportError, KeyError, TypeError):
            LOGGER.exception("P_text batch inference unavailable (model=%s)", path)
            return [unavailable_result() for _ in contents]


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Saved P_text model inference smoke test (no training)")
    parser.add_argument("--content", action="append", required=True)
    parser.add_argument("--model-path", default=None)
    args = parser.parse_args()
    # Bound CPU parallelism for this standalone smoke command only.
    import torch
    torch.set_num_threads(1)
    print(json.dumps([
        {"content": content, **predict_text_score(content, model_path=args.model_path)}
        for content in args.content
    ], ensure_ascii=False, indent=2))
