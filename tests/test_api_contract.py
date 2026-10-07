"""운영 API 목록과 폐기한 legacy 라우트의 비노출·404를 회귀 검증한다."""
import pytest
from fastapi.testclient import TestClient
from app.factory import create_app
from app.repositories.analysis_jobs import SQLiteJobStore


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setenv("ENABLE_EXPERIMENTAL_COLLECTION", "0")
    monkeypatch.setenv("REQUIRE_INTERNAL_TOKEN", "0")
    monkeypatch.delenv("INTERNAL_TOKEN", raising=False)
    with TestClient(create_app(job_store=SQLiteJobStore(str(tmp_path / "results.db")))) as http:
        yield http


def test_production_openapi_contains_only_health_and_data_analysis(client):
    schema = client.get("/openapi.json").json()
    assert set(schema["paths"]) == {"/health", "/api/v1/data/analyze", "/api/v1/data/analyze/stream"}
    assert set(schema["paths"]["/health"]) == {"get"}
    assert set(schema["paths"]["/api/v1/data/analyze"]) == {"post"}
    assert "Legacy AI Analysis" not in str(schema)


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_retired_legacy_endpoint_is_404(client):
    assert client.post("/api/v1/analyze", json={}).status_code == 404
    assert all(getattr(route, "path", None) != "/api/v1/analyze" for route in client.app.routes)


def test_experimental_routes_remain_off_in_production(client):
    assert client.post("/experimental/analysis/collect/stream", json={}).status_code == 404
    assert client.get("/experimental/analysis/jobs/job").status_code == 404


def test_old_internal_lookup_apis_stay_retired(client):
    for path in ("products/product-list", "reviews/product-detail", "products/rti-trend",
                 "reviews/report", "products/risk-report"):
        assert client.post("/api/internal/ai/" + path, json={}).status_code == 404
