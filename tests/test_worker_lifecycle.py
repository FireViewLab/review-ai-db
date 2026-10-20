"""공식 SSE 종료 대기와 재시작 뒤 잔류 RUNNING의 보존을 검증한다."""

import asyncio
import threading
from unittest.mock import Mock

from fastapi.testclient import TestClient

from app.contracts.data_ai_v05 import DataAnalyzeRequestV05
from app.core.analysis_versions import analysis_versions
from app.factory import create_app
from app.repositories.analysis_jobs import SQLiteJobStore
from app.services import data_analysis_stream as service


def configure_local(monkeypatch):
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setenv("ENABLE_EXPERIMENTAL_COLLECTION", "0")
    monkeypatch.setenv("REQUIRE_INTERNAL_TOKEN", "0")
    monkeypatch.delenv("INTERNAL_TOKEN", raising=False)


def sample_payload():
    return DataAnalyzeRequestV05(
        platform="mall", product_id="worker-test",
        reviews=[{"review_id": "r1", "content": "작업 종료 검증용 리뷰입니다."}],
    )


def test_shutdown_waits_for_disconnected_sse_analysis_and_commit(tmp_path, monkeypatch):
    configure_local(monkeypatch)
    store = SQLiteJobStore(str(tmp_path / "shutdown.db"))
    app = create_app(job_store=store)
    entered, release = threading.Event(), threading.Event()
    payload = sample_payload()
    result = dict(platform=payload.platform, product_id=payload.product_id, review_count=1,
                  results=[dict(review_id="r1", rti=-1, level=None, text_score=-1,
                                behavior_score=-1, network_score=-1, reasons=[])])

    def blocked_analysis(db, job_id, _payload):
        entered.set()
        assert release.wait(5), "Test must release the running analysis"
        db.complete(job_id, result)

    monkeypatch.setattr(service, "evaluate_data_and_store", blocked_analysis)

    async def scenario():
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        claim = store.claim_idempotent("shutdown-key", payload.model_dump(mode="json"), analysis_versions())
        task = service.start_analysis(app.state.analysis_tasks, store, claim.job_id, payload)
        stream = service.analysis_events(request_id="shutdown-request", claim=claim, payload=payload, task=task)
        shutdown = None
        try:
            await anext(stream)  # meta
            await anext(stream)  # progress
            assert await asyncio.to_thread(entered.wait, 3)
            await stream.aclose()
            assert not task.cancelled() and task in app.state.analysis_tasks
            shutdown = asyncio.create_task(lifespan.__aexit__(None, None, None))
            await asyncio.sleep(0)
            assert not shutdown.done()
            assert store.get(claim.job_id)["status"] == "RUNNING"
            release.set()
            await asyncio.wait_for(shutdown, timeout=3)
            assert task.done() and not task.cancelled()
            assert store.get(claim.job_id)["status"] == "DONE"
            assert store.get(claim.job_id)["result"] == result
            replay = store.claim_idempotent("shutdown-key", payload.model_dump(mode="json"), {})
            assert not replay.is_new and replay.job_id == claim.job_id
        finally:
            release.set()
            await asyncio.wait_for(task, timeout=3)
            if shutdown is None:
                await lifespan.__aexit__(None, None, None)
            elif not shutdown.done():
                await shutdown

    asyncio.run(scenario())


def test_restart_keeps_running_job_and_rejects_automatic_reexecution(tmp_path, monkeypatch):
    configure_local(monkeypatch)
    path = str(tmp_path / "restart.db")
    original = SQLiteJobStore(path)
    payload = sample_payload()
    with TestClient(create_app(job_store=original)):
        claim = original.claim_idempotent("interrupted-key", payload.model_dump(mode="json"), analysis_versions())
        saved_before = original.get(claim.job_id)

    forbidden = Mock(side_effect=AssertionError("Restart must not reclaim a RUNNING job"))
    monkeypatch.setattr(service, "evaluate_data_and_store", forbidden)
    restarted = SQLiteJobStore(path)
    with TestClient(create_app(job_store=restarted)) as client:
        response = client.post(
            "/api/v1/data/analyze/stream", json=payload.model_dump(mode="json"),
            headers={"X-Request-ID": "retry-after-restart", "Idempotency-Key": "interrupted-key"},
        )
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "IDEMPOTENCY_IN_PROGRESS"
        assert restarted.get(claim.job_id) == saved_before
        assert not client.app.state.analysis_tasks
    forbidden.assert_not_called()
