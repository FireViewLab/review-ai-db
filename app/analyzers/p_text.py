"""Local, lazy KoELECTRA inference. No downloads or training at runtime."""

from functools import lru_cache
import json
import math
import logging
import os
from threading import RLock
from pathlib import Path

DEFAULT_MODEL_PATH = str(Path(__file__).resolve().parents[2] / "models/ptext-koelectra-v1-2epoch-20260929")
LOGGER = logging.getLogger(__name__)
_PREDICTION_LOCK = RLock()
TEXT_SCORE_TEMPERATURE = 2.0
DEFAULT_BATCH_SIZE = 8
MAX_BATCH_SIZE = 32


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


def _temperature_scaled_probability(probability: float) -> float:
    """Soften score saturation without changing the raw classification probability."""
    if probability == 0.0 or probability == 1.0:
        return probability
    logit = math.log(probability / (1.0 - probability))
    return 1.0 / (1.0 + math.exp(-logit / TEXT_SCORE_TEMPERATURE))


def probability_result(probability: float, threshold: float = .5) -> dict:
    if isinstance(probability, bool) or isinstance(threshold, bool) or not math.isfinite(probability) or not 0 <= probability <= 1 or not 0 <= threshold <= 1:
        raise ValueError("Probability and threshold must be finite values in [0, 1]")
    score_probability = _temperature_scaled_probability(probability)
    return {"text_score": round((1 - score_probability) * 100), "suspicious_probability": probability,
            "predicted_label": "SUSPICIOUS" if probability >= threshold else "NORMAL"}


def unavailable_result():
    return {"text_score": -1, "suspicious_probability": None, "predicted_label": None}


class TextScorePredictor:
    def __init__(self, model_path=DEFAULT_MODEL_PATH, *, device=None):
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer
        self.torch = torch
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        path = Path(model_path)
        self.settings = json.loads((path / "inference_config.json").read_text(encoding="utf-8"))
        probability_result(0., self.settings["threshold"])
        if type(self.settings["max_length"]) is not int or not 1 <= self.settings["max_length"] <= 512:
            raise ValueError("Invalid inference max_length")
        self.tokenizer = AutoTokenizer.from_pretrained(path, local_files_only=True)
        self.model = AutoModelForSequenceClassification.from_pretrained(path, local_files_only=True).to(self.device)
        if self.model.config.id2label != {0: "NORMAL", 1: "SUSPICIOUS"}:
            raise ValueError("Unexpected model label mapping")
        self.model.eval()

    def predict_text_score(self, content):
        if not isinstance(content, str) or not content.strip():
            return unavailable_result()
        encoded = self.tokenizer(content, return_tensors="pt", truncation=True, max_length=self.settings["max_length"])
        encoded = {key: value.to(self.device) for key, value in encoded.items()}
        with self.torch.inference_mode():
            probability = self.torch.softmax(self.model(**encoded).logits, dim=-1)[0, 1].item()
        return probability_result(probability, self.settings["threshold"])

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
                predictions = [probability_result(p, self.settings["threshold"]) for p in probabilities]
                for index, prediction in zip(indices, predictions, strict=True):
                    results[index] = prediction
            except (OSError, RuntimeError, ValueError, ImportError, KeyError, TypeError):
                # No second inference attempt: preserve successful chunks and unavailable semantics.
                LOGGER.exception("P_text batch inference unavailable")
        return results


@lru_cache(maxsize=1)
def _predictor(model_path):
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
