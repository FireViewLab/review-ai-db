"""AI core regression tests use synthetic predictions without loading model weights."""
import pytest

from app.analyzers.p_text import probability_result, unavailable_result


@pytest.fixture(autouse=True)
def model_free_core_prediction(monkeypatch):
    def prediction(content):
        if not isinstance(content, str) or not content.strip():
            return unavailable_result()
        return probability_result(0.0)

    monkeypatch.setattr("app.services.analysis.predict_text_scores",
                        lambda contents: [prediction(content) for content in contents])
    return prediction
