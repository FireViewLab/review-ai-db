"""Explicit synthetic fixtures; no trained model is loaded by these tests."""
from datetime import datetime, timedelta

import pytest

from app.analyzers.behavior import BehaviorInput, analyze_behavior
from app.analyzers.network import NetworkReview, analyze_network_batch
from app.scoring.meta_scorer import calculate_rti
from app.services.analysis import analyze_reviews


def test_behavior_weights_missing_and_observed_reasons():
    day = datetime(2026, 9, 30, 12)
    result = analyze_behavior(BehaviorInput(
        verified_purchase=False, review_date=day, user_id="stable-id",
        user_review_dates=(day, day-timedelta(hours=1), day-timedelta(hours=2)),
    ))
    assert result.p_behavior == pytest.approx((70*.5+85*.3)/.8)
    assert {r.code for r in result.reasons} == {"PURCHASE_NOT_VERIFIED", "MULTIPLE_REVIEWS_SAME_DAY"}
    assert analyze_behavior(BehaviorInput()).p_behavior is None
    young = analyze_behavior(BehaviorInput(review_date=day, account_created_at=day))
    assert young.p_behavior == 80
    assert [r.code for r in young.reasons] == ["NEW_ACCOUNT"]
    invalid = analyze_behavior(BehaviorInput(review_date=day, account_created_at=day+timedelta(days=1)))
    assert invalid.p_behavior is None and invalid.reasons == ()


def network(texts):
    return analyze_network_batch([NetworkReview(str(i), "p", text) for i, text in enumerate(texts)])


def test_network_near_duplicate_and_unrelated_korean():
    texts = ["이 크림을 한 달 사용하니 세안 후 당김이 줄고 촉촉해서 만족합니다.",
             "이 크림을 한 달 사용하니 세안 후 당김이 줄고 촉촉해서 만족합니다!",
             "배송 상자가 찌그러졌고 손잡이 나사가 빠져 반품했습니다."]
    results = network(texts)
    assert results[0].features.similarity_max >= .85
    assert results[0].features.similar_review_count == 1
    assert results[0].p_network == 39.1
    assert results[2].p_network is None and results[2].reasons == ()


@pytest.mark.parametrize("texts", [["리뷰 한 건"], ["", "  "], ["좋", "!"]])
def test_network_unavailable(texts):
    assert all(r.p_network is None and not r.reasons for r in network(texts))


def test_network_duplicates_monotonic_bounded_and_invalid_peer():
    scores = [network(["충분히 긴 동일한 본문입니다"] * n)[0].p_network for n in (2, 3, 6, 10)]
    assert scores == sorted(scores, reverse=True)
    assert all(0 <= score <= 100 for score in scores)
    results = network(["실제로 사용한 후기입니다", "", "전혀 다른 배송 이야기"])
    assert results[0].features.compared_review_count == 0
    assert results[0].p_network is None
    assert results[1].p_network is None


@pytest.mark.parametrize("author", ["신**", "김**", "같은닉네임"])
def test_display_author_never_links_accounts(author):
    result = analyze_reviews(platform="kurly", product_id="p", reviews=[
        {"review_id": str(i), "content": "후기", "author": author,
         "written_at": datetime(2026, 9, 30)} for i in range(3)
    ])
    assert all(row["behavior_score"] == -1 for row in result["results"])
    assert not any(reason.startswith("BEHAVIOR_")
                   for row in result["results"] for reason in row["reasons"])


@pytest.mark.parametrize("scores", [(80,-1,-1),(80,70,-1),(80,-1,70),(-1,80,70),(80,70,60),(-1,-1,-1)])
def test_rti_all_combinations(scores):
    weights = (.5, .3, .2)
    expected = sum(s*w for s,w in zip(scores,weights) if s != -1)
    total = sum(w for s,w in zip(scores,weights) if s != -1)
    assert calculate_rti(*scores) == pytest.approx(round(expected/total, 1) if total else -1)


def test_python_entry_point_contract_and_missing(monkeypatch):
    monkeypatch.setattr("app.services.analysis.predict_text_score", lambda text: {"text_score":80 if text else -1})
    result = analyze_reviews(platform="kurly", product_id="p", reviews=[
        {"review_id":"1", "content":"한 달 사용한 크림 후기입니다", "verified_purchase":False},
        {"review_id":"2", "content":"배송이 느리고 포장이 찢어져 왔습니다"},
    ])
    assert result['review_count'] == len(result['results']) == 2
    assert result['results'][0]['behavior_score'] == 70
    assert result['results'][1]['behavior_score'] == -1
    assert result['results'][0]['rti'] == 76.2  # text + behavior, renormalized
    assert result['results'][1]['rti'] == 80.0  # text only
    expected_keys = {'review_id','rti','level','text_score','behavior_score','network_score','reasons'}
    for row in result['results']:
        assert set(row) == expected_keys
        assert all(row[k] is not None for k in ('rti','text_score','behavior_score','network_score'))
    missing = analyze_reviews(platform="kurly",product_id="p",reviews=[{'review_id':'1','content':''}])['results'][0]
    assert missing['rti'] == -1 and missing['level'] is None
    assert missing['network_score'] == -1
    text_only = analyze_reviews(
        platform="kurly", product_id="p",
        reviews=[{"review_id": "text-only", "content": "단독 리뷰입니다"}],
    )["results"][0]
    assert (text_only["text_score"], text_only["behavior_score"],
            text_only["network_score"], text_only["rti"]) == (80.0, -1.0, -1.0, 80.0)
    text_behavior = analyze_reviews(
        platform="kurly", product_id="p",
        reviews=[{"review_id": "text-behavior", "content": "단독 구매 후기입니다",
                  "verified_purchase": False}],
    )["results"][0]
    assert (text_behavior["text_score"], text_behavior["behavior_score"],
            text_behavior["network_score"], text_behavior["rti"]) == (80.0, 70.0, -1.0, 76.2)


def test_python_entry_point_combines_text_behavior_and_network(monkeypatch):
    """Synthetic integration fixture; these are not production reviews."""
    monkeypatch.setattr(
        "app.services.analysis.predict_text_score",
        lambda content: {"text_score": 80.0},
    )
    review_date = datetime(2026, 9, 30, 12)
    reviews = [
        {
            "review_id": "similar-1",
            "content": "이 크림을 한 달 사용하니 세안 후 당김이 줄고 촉촉해서 만족합니다.",
            "verified_purchase": False,
            "account_created_at": review_date - timedelta(days=2),
            "review_date": review_date,
            "user_id": "stable-user-id",
            "user_review_dates": (
                review_date,
                review_date - timedelta(hours=1),
                review_date - timedelta(hours=2),
            ),
        },
        {
            "review_id": "similar-2",
            "content": "이 크림을 한 달 사용하니 세안 후 당김이 줄고 촉촉해서 만족합니다!",
        },
        {
            "review_id": "unrelated",
            "content": "배송 상자가 찌그러졌고 손잡이 나사가 빠져 반품했습니다.",
        },
    ]

    result = analyze_reviews(platform="kurly", product_id="fixture-product", reviews=reviews)
    similar, _, unrelated = result["results"]

    expected_behavior = 70 * .5 + 80 * .2 + 85 * .3
    expected_rti = round(80 * .5 + expected_behavior * .3 + 39.1 * .2, 1)
    assert similar["text_score"] == 80.0
    assert similar["behavior_score"] == expected_behavior == 76.5
    assert similar["network_score"] == 39.1
    assert similar["rti"] == expected_rti == 70.8
    assert {
        "BEHAVIOR_PURCHASE_NOT_VERIFIED",
        "BEHAVIOR_NEW_ACCOUNT",
        "BEHAVIOR_MULTIPLE_REVIEWS_SAME_DAY",
        "NETWORK_SIMILAR_REVIEW_PATTERN",
    }.issubset(similar["reasons"])
    assert unrelated["behavior_score"] == -1.0
    assert unrelated["network_score"] == -1.0
    assert not any(reason.startswith("BEHAVIOR_") or reason.startswith("NETWORK_")
                   for reason in unrelated["reasons"])
    assert result["review_count"] == len(result["results"]) == 3


def test_python_entry_point_rejects_mixed_origins():
    with pytest.raises(ValueError, match="platform"):
        analyze_reviews(platform="kurly", product_id="p", reviews=[{'platform':'ohouse','review_id':'1'}])
