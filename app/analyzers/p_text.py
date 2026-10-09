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
