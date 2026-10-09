"""Model-free inference, scoring and Result Contract v0.5 regression tests."""
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from app.analyzers import p_text
from app.scoring.meta_scorer import calculate_rti, classify_rti
from app.schemas.analysis import ProductAnalysisResponse, ReviewAnalysisResponse, to_review_response
from app.services import analysis


@pytest.mark.parametrize('p,score', [(0, 100), (.002, 96), (.13, 72), (.5, 50), (.8, 33), (1, 0)])
def test_probability_score_boundaries(p, score):
    assert p_text.probability_result(p)['text_score'] == score


@pytest.mark.parametrize('p,threshold,label', [
    (.002, .5, 'NORMAL'), (.8, .7, 'SUSPICIOUS'),
    (.8, .8, 'SUSPICIOUS'), (.7999, .8, 'NORMAL'),
    (.13, .2, 'NORMAL'),
])
def test_temperature_scaling_preserves_raw_probability_and_label(p, threshold, label):
    result = p_text.probability_result(p, threshold)
    assert result['suspicious_probability'] == p
    assert result['predicted_label'] == label
    assert result['text_score'] == p_text.probability_result(p)['text_score']


@pytest.mark.parametrize('value', [-.01, 1.01, float('nan'), float('inf'), True])
def test_invalid_probability(value):
    with pytest.raises(ValueError):
        p_text.probability_result(value)


@pytest.mark.parametrize('content', ['', '  ', None, 42, {}])
def test_invalid_content_never_loads_model(monkeypatch, content):
    loader = Mock(side_effect=AssertionError('must not load'))
    monkeypatch.setattr(p_text, '_predictor', loader)
    assert p_text.predict_text_score(content) == p_text.unavailable_result()
    loader.assert_not_called()


@pytest.mark.parametrize('error', [FileNotFoundError('missing'), RuntimeError('backend failure'), ValueError('bad config')])
def test_unavailable_backend_returns_sentinel_and_logs(monkeypatch, caplog, error):
    monkeypatch.setattr(p_text, '_predictor', Mock(side_effect=error))
    assert p_text.predict_text_score('private review content')['text_score'] == -1
    assert 'P_text inference unavailable' in caplog.text
    assert 'private review content' not in caplog.text


def test_prediction_failure_returns_sentinel(monkeypatch):
    backend = Mock()
    backend.predict_text_score.side_effect = RuntimeError('forward failed')
    monkeypatch.setattr(p_text, '_predictor', Mock(return_value=backend))
    assert p_text.predict_text_score('text')['text_score'] == -1


def test_model_cache_serializes_first_load_and_uses_configured_path(monkeypatch):
    p_text._predictor.cache_clear()
    backend = Mock()
    backend.predict_text_score.return_value = p_text.probability_result(.13)
    factory = Mock(return_value=backend)
    monkeypatch.setattr(p_text, 'TextScorePredictor', factory)
    monkeypatch.setenv('PTEXT_MODEL_PATH', 'custom-model')
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(p_text.predict_text_score, ['text'] * 8))
        factory.assert_called_once_with('custom-model')
        assert all(r['text_score'] == 72 for r in results)
    finally:
        p_text._predictor.cache_clear()


@pytest.mark.parametrize('scores,expected', [
    ((85, -1, 76), 82.4),
    ((85, 60, -1), 75.6),
    ((85, -1, -1), 85),
    ((-1, 60, 80), 68),
    ((-1, -1, -1), -1),
    ((0, -1, -1), 0),
    ((100, 100, 100), 100),
])
def test_rti_missing_signals_and_renormalization(scores, expected):
    assert calculate_rti(*scores) == expected


@pytest.mark.parametrize('value', [None, True, -.5, -2, 101, float('nan')])
def test_rti_rejects_invalid_score(value):
    with pytest.raises((TypeError, ValueError)):
        calculate_rti(value)


@pytest.mark.parametrize('score,level', [(0, 'danger'), (39.9, 'danger'), (40, 'warn'), (69.9, 'warn'), (70, 'safe'), (100, 'safe')])
def test_level(score, level):
    assert classify_rti(score).value == level


def response_row(**changes):
    return dict(review_id='1', rti=87, level='safe', text_score=87,
                behavior_score=-1, network_score=-1, reasons=[], **changes)


@pytest.mark.parametrize('field', ['text_score', 'behavior_score', 'network_score', 'rti'])
@pytest.mark.parametrize('value', [None, -.5, 101, True, float('nan')])
def test_contract_rejects_null_or_invalid_scores(field, value):
    row = response_row()
    row[field] = value
    with pytest.raises(ValidationError):
        ReviewAnalysisResponse(**row)


def test_contract_forbids_nested_or_availability_fields():
    for name in ('signals', 'available', 'rti_available', 'unavailable_reasons'):
        with pytest.raises(ValidationError):
            ReviewAnalysisResponse(**response_row(), **{name: {}})


def test_count_must_match_results():
    with pytest.raises(ValidationError, match='review_count'):
        ProductAnalysisResponse(platform='kurly', product_id='p', review_count=2,
                                results=[ReviewAnalysisResponse(**response_row())])


@pytest.mark.parametrize('score', [-1, 0, 87])
def test_service_to_contract_missing_and_zero(monkeypatch, score):
    predictor = Mock(return_value={'text_score': score})
    monkeypatch.setattr(analysis, 'predict_text_score', predictor)
    content = 'A sufficiently long neutral review describing my personal experience.'
    result = analysis.analyze_product_reviews('p', [analysis.ReviewAnalysisInput('1', 'p', content)])[0]
    row = to_review_response(result, platform='kurly', review_id='1', product_id='p')
    assert row.rti == score
    assert row.level == (None if score == -1 else 'danger' if score == 0 else 'safe')
    assert row.reasons == []  # Probability is not evidence of a specific rule.
    assert row.behavior_score == row.network_score == -1
    predictor.assert_called_once_with(content)
    assert set(row.model_dump()) == {'review_id', 'rti', 'level', 'text_score', 'behavior_score', 'network_score', 'reasons'}


def test_empty_text_does_not_create_network_score(monkeypatch):
    monkeypatch.setattr(analysis, 'predict_text_score', lambda content: p_text.unavailable_result())
    rows = analysis.analyze_product_reviews('p', [analysis.ReviewAnalysisInput('1', 'p', ''), analysis.ReviewAnalysisInput('2', 'p', '  ')])
    assert all(row.rti is None and row.level is None for row in rows)


def test_team_service_import_remains_compatible():
    from app.services import team_analysis
    assert team_analysis.analyze_product_reviews is analysis.analyze_product_reviews
    assert team_analysis.analyze_reviews is analysis.analyze_reviews
