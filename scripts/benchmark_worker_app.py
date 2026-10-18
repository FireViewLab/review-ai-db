"""Disposable-only app factory adding PID/timing observations outside production APIs."""

from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time
from uuid import uuid4

from fastapi import Depends, HTTPException, Request

LOG_PREFIX = "BENCHMARK_EVENT "
_events: list[dict] = []
_state_lock = threading.Lock()
_configured = False


def require_disposable_environment() -> None:
    if (os.getenv("BENCHMARK_DISPOSABLE") != "1"
            or os.getenv("DB_NAME") != "runtime_validation"
            or os.getenv("DB_HOST") != "ai-db"
            or os.getenv("ENABLE_GROQ_REASON_NATURALIZATION", "0") != "0"):
        raise RuntimeError("Benchmark requires the isolated validation database and Groq OFF")


def record(kind: str, **values) -> dict:
    event = dict(kind=kind, pid=os.getpid(), monotonic=time.perf_counter(), **values)
    with _state_lock:
        _events.append(event)
    # Each bounded event fits Linux PIPE_BUF; one syscall prevents Worker/thread
    # print buffering from joining or interleaving the benchmark JSON records.
    encoded = (LOG_PREFIX + json.dumps(event, separators=(",", ":")) + "\n").encode("utf-8")
    os.write(1, encoded)
    return event


def process_memory() -> dict:
    values = {}
    status = Path("/proc/self/status")
    if status.is_file():
        for line in status.read_text().splitlines():
            name, _, raw = line.partition(":")
            if name in {"VmRSS", "VmHWM"}:
                values[name + "_bytes"] = int(raw.strip().split()[0]) * 1024
    return values


def instrument_runtime() -> None:
    global _configured
    if _configured:
        return
    from app.analyzers import p_text
    from app.services import analysis, data_analysis

    original_init = p_text.TextScorePredictor.__init__

    def observed_init(self, *args, **kwargs):
        started = time.perf_counter()
        record("model_load_start")
        try:
            original_init(self, *args, **kwargs)
        except Exception:
            record("model_load_end", seconds=time.perf_counter() - started, succeeded=False)
            raise
        record("model_load_end", seconds=time.perf_counter() - started,
               succeeded=True, device=self.device)

    p_text.TextScorePredictor.__init__ = observed_init
    if os.getenv("BENCHMARK_USE_SINGLE") == "1":
        # True pre-batch baseline: unchanged public single inference, not batch size 1.
        analysis.predict_text_scores = lambda contents: [
            p_text.predict_text_score(content) for content in contents
        ]
    original_analyze = analysis.analyze_reviews

    def observed_analyze(**kwargs):
        product_id = kwargs.get("product_id", "")
        if (not isinstance(product_id, str) or not product_id.startswith("benchmark-")
                or len(product_id) > 160):
            raise ValueError("Benchmark app only accepts synthetic benchmark products")
        analysis_id = str(uuid4())
        started = time.perf_counter()
        record("analysis_start", analysis_id=analysis_id, product_id=product_id,
               review_count=len(kwargs.get("reviews", [])))
        try:
            result = original_analyze(**kwargs)
        except Exception:
            record("analysis_end", analysis_id=analysis_id, product_id=product_id,
                   seconds=time.perf_counter() - started, succeeded=False)
            raise
        record("analysis_end", analysis_id=analysis_id, product_id=product_id,
               seconds=time.perf_counter() - started, succeeded=True)
        return result

    analysis.analyze_reviews = observed_analyze
    data_analysis.analyze_reviews = observed_analyze
    _configured = True


def create_benchmark_app():
    """Run with uvicorn benchmark_worker_app:create_benchmark_app --factory."""
    require_disposable_environment()
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    from app.core.analysis_versions import analysis_versions
    from app.core.internal_auth import require_internal_token
    from app.factory import create_app

    instrument_runtime()
    app = create_app()

    @app.get("/__benchmark__/state", include_in_schema=False,
             dependencies=[Depends(require_internal_token)])
    def state():
        from app.analyzers.p_text import _predictor
        with _state_lock:
            events = list(_events)
        return dict(pid=os.getpid(), workers=int(os.getenv("UVICORN_WORKERS", "1")),
                    batch_size=int(os.getenv("PTEXT_BATCH_SIZE", "8")),
                    single_baseline=os.getenv("BENCHMARK_USE_SINGLE") == "1",
                    versions=analysis_versions(), memory=process_memory(),
                    model_cache=dict(_predictor.cache_info()._asdict()), events=events)

    @app.get("/__benchmark__/jobs/{job_id}", include_in_schema=False,
             dependencies=[Depends(require_internal_token)])
    def saved_job(job_id: str, request: Request):
        job = request.app.state.job_store.get(job_id)
        if job is None or not job["request"].get("product_id", "").startswith("benchmark-"):
            raise HTTPException(404, "Synthetic benchmark job unavailable")
        return {"job_id": job_id, "status": job["status"], "result": job["result"]}

    return app
