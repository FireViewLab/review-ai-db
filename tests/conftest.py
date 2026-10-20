"""운영 연동 테스트에서만 사용하는 모델 없는 P_text 의존성 fixture다."""
import pytest


@pytest.fixture
def model_free_prediction(monkeypatch):
    """실제 모델은 별도 smoke에서 검증하고 pytest는 저장·계약을 결정적으로 검증한다."""
    from app.analyzers.p_text import probability_result

    def prediction(content):
        return probability_result(.13)

    monkeypatch.setattr("app.services.analysis.predict_text_scores",
                        lambda contents: [prediction(content) for content in contents])
    return prediction
