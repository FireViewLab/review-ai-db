"""P_text v4.0 텍스트 모델과 등급 규칙 A(텍스트 중심) 추론.

모델 디렉터리(PTEXT_V4_MODEL_PATH)에는 가중치·토크나이저와 함께 두 설정 파일이 있어야 한다.
- inference_config.json: calibration_temperature, max_length, model_family
- policy.json: version, warn_cut, danger_cut (검증 세트에서 정한 의심 확률 경계)

등급은 보정된 의심 확률만으로 정한다. 사진 유무는 쓰지 않는다.
- 확률 >= danger_cut → DANGER, >= warn_cut → WARN, 그 외 SAFE
- 정책 지수는 등급 구간(SAFE 70~100, WARN 40~69, DANGER 0~39) 안에서 확률 순서를 보존한다.
학습·평가 코드는 FireViewLab/review-model에 있고, 이 모듈은 그 serving 로직과 같은 결과를 낸다.
"""

import json
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

BANDS = {"SAFE": (70.0, 100.0), "WARN": (40.0, 69.0), "DANGER": (0.0, 39.0)}


@dataclass(frozen=True)
class V4Policy:
    version: str
    warn_cut: float
    danger_cut: float

    @classmethod
    def load(cls, path: Path) -> "V4Policy":
        data = json.loads(path.read_text(encoding="utf-8"))
        policy = cls(str(data["version"]), float(data["warn_cut"]), float(data["danger_cut"]))
        if not 0 < policy.warn_cut < policy.danger_cut <= 1:
            raise ValueError("policy.json의 경계값이 올바르지 않다")
        return policy


@dataclass(frozen=True)
class V4Result:
    review_id: str
    text_probability: float
    text_score: int
    policy_index: float
    level: str


def grade(probability: float, policy: V4Policy) -> str:
    if probability >= policy.danger_cut:
        return "DANGER"
    return "WARN" if probability >= policy.warn_cut else "SAFE"


def policy_index(probability: float, policy: V4Policy) -> float:
    """등급 구간 안에서 의심 확률이 낮을수록 높은 지수."""
    level = grade(probability, policy)
    lo, hi = {
        "SAFE": (0.0, policy.warn_cut),
        "WARN": (policy.warn_cut, policy.danger_cut),
        "DANGER": (policy.danger_cut, 1.0),
    }[level]
    low, high = BANDS[level]
    position = (probability - lo) / max(hi - lo, 1e-9)
    return round(high - min(max(position, 0.0), 1.0) * (high - low), 1)


class V4Predictor:
    def __init__(self, probability: Callable[[Sequence[str]], Sequence[float]], policy: V4Policy,
                 model_version: str):
        self._probability = probability
        self.policy = policy
        self.model_version = model_version

    def analyze(self, reviews: Sequence[dict]) -> list[V4Result]:
        if not reviews:
            return []
        probs = [min(max(float(p), 0.0), 1.0) for p in self._probability([r["content"] for r in reviews])]
        return [
            V4Result(
                review_id=str(r["review_id"]),
                text_probability=round(p, 5),
                text_score=round((1 - p) * 100),
                policy_index=policy_index(p, self.policy),
                level=grade(p, self.policy),
            )
            for r, p in zip(reviews, probs, strict=True)
        ]

    @classmethod
    def from_model_dir(cls, model_dir: str | Path, *, batch_size: int = 16) -> "V4Predictor":
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        path = Path(model_dir)
        config = json.loads((path / "inference_config.json").read_text(encoding="utf-8"))
        policy = V4Policy.load(path / "policy.json")
        device = "cuda" if torch.cuda.is_available() else "cpu"
        tokenizer = AutoTokenizer.from_pretrained(path, local_files_only=True)
        model = AutoModelForSequenceClassification.from_pretrained(path, local_files_only=True).to(device).eval()
        temperature = float(config.get("calibration_temperature", 1.0))
        max_length = int(config.get("max_length", 256))
        lock = Lock()

        def probability(texts: Sequence[str]) -> list[float]:
            # 길이가 비슷한 리뷰끼리 묶어 배치마다 가장 긴 리뷰에 맞춘 패딩 계산을 줄인다.
            # attention mask로 패딩은 결과에 영향이 없으므로 입력 순서로 되돌리면 값이 같다.
            ids = tokenizer(list(texts), truncation=True, max_length=max_length)["input_ids"]
            order = sorted(range(len(texts)), key=lambda i: len(ids[i]))
            out: list[float] = [0.0] * len(texts)
            with lock, torch.no_grad():
                for start in range(0, len(order), batch_size):
                    chunk = order[start:start + batch_size]
                    enc = tokenizer.pad({"input_ids": [ids[i] for i in chunk]}, return_tensors="pt").to(device)
                    logits = model(**enc).logits.float().cpu()
                    for i, diff in zip(chunk, (logits[:, 1] - logits[:, 0]).tolist(), strict=True):
                        out[i] = 1 / (1 + math.exp(-diff / temperature))
            return out

        return cls(probability, policy, str(config.get("model_family", path.name)))
