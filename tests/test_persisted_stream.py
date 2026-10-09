"""저장 선행, 수집 종료 계약, 연결 중단과 기존 평가 보존을 검증한다."""
import asyncio
import json
import time
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient
from app.factory import create_app
from app.repositories.analysis_jobs import SQLiteJobStore
from app.contracts.crawler import CollectStreamRequest
from app.contracts.stream import ReviewEvent, DoneEvent
from app.core.crawler_settings import CrawlerSettings
from app.integrations.crawler_stream import CrawlerStreamClient, iter_sse_frames
from app.services.collection_stream import collection_events
from tests.test_data_ai_v05 import payload

pytestmark = pytest.mark.usefixtures("model_free_prediction")


@pytest.fixture
def store(tmp_path):
    value = SQLiteJobStore(str(tmp_path / "results.db"))
    value.initialize()
    return value


def test_http_result_is_durable(store, monkeypatch):
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setenv("ENABLE_EXPERIMENTAL_COLLECTION", "0")
    with TestClient(create_app(job_store=store)) as client:
        r = client.post("/api/v1/data/analyze", json=payload())
        assert r.status_code == 200
        saved = SQLiteJobStore(store.path).get(r.headers["X-Analysis-Job-ID"])
        assert saved["status"] == "DONE"
        assert saved["result"] == r.json()
        assert saved["result"]["results"][0]["rti"] == 72
        assert client.post("/experimental/analysis/collect/stream").status_code == 404


def test_storage_failure_never_returns_success(store, monkeypatch):
    monkeypatch.setenv("ENABLE_EXPERIMENTAL_COLLECTION", "0")
    with TestClient(create_app(job_store=store)) as client:
        with patch.object(store, "complete", side_effect=OSError("disk full")):
            r = client.post("/api/v1/data/analyze", json=payload())
        assert r.status_code == 503


class FakeCrawler:
    def __init__(self, events):
        self.events = events
        self.closed = False

    async def stream_reviews(self, *args, **kwargs):
        try:
            for event in self.events:
                yield event
        finally:
            self.closed = True


def review(id="r"):
    return ReviewEvent(platform="mall", product_id="p", review_id=id,
                       content="배송 빠르고 제품도 좋아요")


def run_stream(store, events):
    async def run():
        job_id = store.create({})
        crawler = FakeCrawler(events)
        frames = [frame async for frame in collection_events(
            crawler, store, job_id, CollectStreamRequest(platform="mall", product_id="p"),
        )]
        decoded = list(iter_sse_frames(b"".join(frames).decode().splitlines()))
        assert crawler.closed
        return job_id, decoded
    return asyncio.run(run())


def test_stream_stores_before_result_and_deduplicates(store):
    job_id, frames = run_stream(store, [review(), review(), DoneEvent(job_id="up", collected=1)])
    assert frames[-1].event == "result"
    assert store.get(job_id)["status"] == "DONE"
    assert store.get(job_id)["result"] == json.loads(frames[-1].data)
    assert len(store.get(job_id)["result"]["results"]) == 1


@pytest.mark.parametrize("events", [
    [review()],
    [DoneEvent(job_id="up", collected=0)],
    [review(), DoneEvent(job_id="up", collected=2)],
    [review(), review().model_copy(update={"content": "different"})],
    [review().model_copy(update={"product_id": "other"})],
])
def test_invalid_collection_never_scores(store, events):
    with patch("app.services.data_analysis.analyze_reviews") as scorer:
        job_id, frames = run_stream(store, events)
    scorer.assert_not_called()
    assert frames[-1].event == "error"
    assert store.get(job_id)["status"] == "FAILED"


def test_v05_mapping_preserves_ids_and_missing_evidence(store):
    job_id, frames = run_stream(store, [review(), DoneEvent(job_id="up", collected=1)])
    result = json.loads(frames[-1].data)
    assert result["platform"] == "mall" and result["product_id"] == "p"
    assert result["results"][0]["review_id"] == "r"
    assert result["results"][0]["behavior_score"] == -1
    assert result["results"][0]["network_score"] == -1
    assert store.get(job_id)["result"] == result


def test_stream_storage_failure_has_no_result(store):
    with patch.object(store, "complete", side_effect=OSError("disk full")):
        job_id, frames = run_stream(store, [review(), DoneEvent(job_id="up", collected=1)])
    assert frames[-1].event == "error"
    assert not any(f.event == "result" for f in frames)
    assert store.get(job_id)["status"] == "FAILED"


def test_disconnect_during_collection_marks_failed(store):
    async def run():
        job_id = store.create({})
        crawler = FakeCrawler([review(), DoneEvent(job_id="up", collected=1)])
        stream = collection_events(crawler, store, job_id,
                                   CollectStreamRequest(platform="mall", product_id="p"))
        await anext(stream)
        await stream.aclose()
        assert crawler.closed
        assert store.get(job_id)["error_code"] == "CLIENT_DISCONNECTED"
    asyncio.run(run())


def test_heartbeat_during_analysis_and_disconnect_preserves_result(store):
    from app.services.data_analysis import evaluate_data_and_store
    def slow(*args):
        time.sleep(.08)
        return evaluate_data_and_store(*args)
    async def run():
        job_id = store.create({})
        stream = collection_events(
            FakeCrawler([review(), DoneEvent(job_id="up", collected=1)]), store, job_id,
            CollectStreamRequest(platform="mall", product_id="p"),
            heartbeat_seconds=.01)
        assert b"progress" in await anext(stream)
        assert b"heartbeat" in await anext(stream)
        await stream.aclose()
        for _ in range(100):
            if store.get(job_id)["status"] == "DONE":
                break
            await asyncio.sleep(.01)
        assert store.get(job_id)["status"] == "DONE"
    with patch("app.services.collection_stream.evaluate_data_and_store", side_effect=slow):
        asyncio.run(run())


def test_real_sse_client_with_mock_transport():
    async def run():
        body = 'event: review\nid: 1\ndata: {"platform":"mall","product_id":"p","review_id":"r","content":"ok"}\n\nevent: done\ndata: {"job_id":"up","collected":1}\n\n'
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)
        ), base_url="http://crawler.test") as http:
            crawler = CrawlerStreamClient(client=http, settings=CrawlerSettings(base_url="http://crawler.test"))
            events = [e async for e in crawler.stream_reviews("mall", "p", limit=2)]
            assert isinstance(events[0], ReviewEvent)
            assert isinstance(events[-1], DoneEvent)
    asyncio.run(run())


def test_experimental_endpoint_and_lookup(store, monkeypatch):
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setenv("ENABLE_EXPERIMENTAL_COLLECTION", "1")
    monkeypatch.setenv("CRAWLER_BASE_URL", "http://crawler.test")
    app = create_app(job_store=store)
    with TestClient(app) as client:
        app.state.crawler_stream = FakeCrawler([review(), DoneEvent(job_id="up", collected=1)])
        response = client.post("/experimental/analysis/collect/stream",
                               json={"platform": "mall", "product_id": "p"})
        assert response.status_code == 200
        frames = list(iter_sse_frames(response.text.splitlines()))
        assert frames[-1].event == "result"
        saved = client.get("/experimental/analysis/jobs/" + response.headers["X-Analysis-Job-ID"])
        assert saved.json()["result"] == json.loads(frames[-1].data)
        assert "request" not in saved.json()


def test_experimental_rejects_product_regrouping(store, monkeypatch):
    monkeypatch.setenv("ENABLE_EXPERIMENTAL_COLLECTION", "1")
    monkeypatch.setenv("CRAWLER_BASE_URL", "http://crawler.test")
    with TestClient(create_app(job_store=store)) as client:
        response = client.post("/experimental/analysis/collect/stream",
                               json={"platform": "mall", "product_id": "p", "product_key": "group"})
        assert response.status_code == 422


def test_analyzer_failure_is_persisted(store):
    with patch("app.services.data_analysis.analyze_reviews", side_effect=RuntimeError("model")):
        job_id, frames = run_stream(store, [review(), DoneEvent(job_id="up", collected=1)])
    assert frames[-1].event == "error"
    assert store.get(job_id)["status"] == "FAILED"


@pytest.mark.parametrize("body,content_type", [
    ('event: review\ndata: {}\n\n', "text/event-stream"),
    ('event: done\ndata: not-json\n\n', "text/event-stream"),
    ('event: heartbeat\ndata: {}\n\n', "text/event-stream"),
    ('{}', "application/json"),
])
def test_crawler_contract_errors(body, content_type):
    from app.integrations.crawler_client import CrawlerRequestError, CrawlerUnavailableError
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, headers={"content-type": content_type}, text=body)
        ), base_url="http://crawler.test") as http:
            crawler = CrawlerStreamClient(client=http, settings=CrawlerSettings(base_url="http://crawler.test"))
            with pytest.raises((CrawlerRequestError, CrawlerUnavailableError)):
                _ = [e async for e in crawler.stream_reviews("mall", "p", limit=2)]
    asyncio.run(run())
