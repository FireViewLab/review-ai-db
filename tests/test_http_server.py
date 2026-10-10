"""실제 Uvicorn TCP 서버에서 health와 분석 HTTP 요청을 검증한다."""
import json
import socket
import threading
import time
from urllib.request import Request, urlopen

import uvicorn
import pytest
from app.factory import create_app
from app.repositories.analysis_jobs import SQLiteJobStore

pytestmark = pytest.mark.usefixtures("model_free_prediction")


def test_live_http(monkeypatch, tmp_path):
    monkeypatch.setenv("ENABLE_EXPERIMENTAL_COLLECTION", "0")
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    app = create_app(job_store=SQLiteJobStore(str(tmp_path / "results.db")))
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started
        base = f"http://127.0.0.1:{port}"
        with urlopen(base + "/health", timeout=5) as response:
            assert response.status == 200
            assert json.load(response) == {"status": "ok"}
        data_payload = {"platform": "mall", "product_id": "0007", "reviews": [{
            "review_id": "00:01", "content": "배송 빠르고 제품도 좋아요",
        }]}
        data_request = Request(base + "/api/v1/data/analyze",
                               data=json.dumps(data_payload).encode(),
                               headers={"Content-Type": "application/json"})
        with urlopen(data_request, timeout=5) as response:
            assert response.status == 200
            result = json.load(response)
            assert result["results"][0]["rti"] == 72.0
            assert result["results"][0]["behavior_score"] == -1
            assert app.state.job_store.get(response.headers["X-Analysis-Job-ID"])["result"] == result
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        assert not thread.is_alive()
