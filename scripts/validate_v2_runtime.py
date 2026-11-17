"""Pinned synthetic v2 verification: real CPU models, isolated MySQL SSE, or one live Groq batch."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Event, Thread
from time import perf_counter, process_time, sleep, time
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.analyzers.p_text import TextScorePredictor, load_candidate_predictor
from app.contracts.data_ai_v2 import DataAnalyzeRequestV2
from app.integrations.groq_v2_summary import INTERVAL_SECONDS, MAX_COMPLETION_TOKENS, MODEL, V2GroqSummary
from app.services.data_analysis_v2 import evaluate_v2
from scripts.compare_ai_v3_candidate import FIXTURE_SHA256, peak_working_set_mib, sha256
from scripts.compare_ai_v3_groq_reasons import LIVE_REVIEW_IDS, LOCAL_OUTPUT


def fixture_payload(path, four=False):
    if sha256(path) != FIXTURE_SHA256:
        raise ValueError("Only the pinned synthetic fixture is allowed")
    source = json.loads(path.read_text(encoding="utf-8-sig"))
    if source["data_type"] != "SYNTHETIC_ONLY" or source["is_ground_truth"] is not False:
        raise ValueError("Synthetic fixture required")
    rows = source["reviews"]
    first = rows[0]
    return {"platform": first["platform"], "product_id": first["product_id"], "reviews": [
        {key: row.get(key) for key in ("content", "review_id", "rating", "image_count")}
        for row in rows if not four or row["review_id"] in LIVE_REVIEW_IDS]}


def model_profile(mode, payload):
    texts = [row["content"] for row in payload["reviews"]]
    path = Path(os.environ["PTEXT_MODEL_PATH" if mode == "model-v1" else "PTEXT_V31B_MODEL_PATH"])
    before = peak_working_set_mib()
    mark = perf_counter()
    predictor = TextScorePredictor(path, device="cpu") if mode == "model-v1" else load_candidate_predictor(path, device="cpu")
    load, loaded = perf_counter() - mark, peak_working_set_mib()
    mark = perf_counter()
    one = predictor.predict_text_scores(texts[:1], batch_size=8)
    one_time = perf_counter() - mark
    mark, cpu = perf_counter(), process_time()
    batch = predictor.predict_text_scores(texts, batch_size=8)
    wall = perf_counter() - mark
    assert all(row["text_score"] >= 0 for row in one + batch) and one[0]["text_score"] == batch[0]["text_score"]
    return {"path": str(path), "weights_bytes": (path / "model.safetensors").stat().st_size,
            "weights_sha256": sha256(path / "model.safetensors"), "files": sorted(p.name for p in path.iterdir() if p.is_file()),
            "peak_before_mib": before, "load_seconds": load, "peak_loaded_mib": loaded,
            "one_seconds": one_time, "batch_seconds": wall, "batch_cpu_seconds": process_time() - cpu,
            "peak_mib": peak_working_set_mib(), "rows": len(batch)}


def streams(payload):
    from fastapi.testclient import TestClient
    from app.factory import create_app
    from app.repositories.mysql_jobs import MySQLJobStore
    from app.repositories.v2_job_migration import migrate_v2_schema

    if (os.getenv("DB_HOST"), os.getenv("DB_PORT"), os.getenv("DB_NAME")) != ("127.0.0.1", "43317", "review_ai_v2_isolated"):
        raise ValueError("Use only the named local disposable MySQL profile")
    v1, v2 = MySQLJobStore.from_env(), MySQLJobStore.from_env(scope="v2")
    v1.initialize()
    migrate_v2_schema(v2)
    commit_seconds = {"v1": [], "v2": []}

    def measure_commit(store, version):
        original = store.complete

        def measured(*args, **kwargs):
            started = perf_counter()
            result = original(*args, **kwargs)
            commit_seconds[version].append(perf_counter() - started)
            return result

        store.complete = measured

    measure_commit(v1, "v1")
    measure_commit(v2, "v2")
    old_payload = {**payload, "reviews": [{k: v for k, v in row.items() if k != "image_count"} for row in payload["reviews"]]}
    paths = {"v1": "/api/v1/data/analyze/stream", "v2": "/api/v2/data/analyze/stream"}
    runs = []
    base = peak_working_set_mib()
    with TestClient(create_app(job_store=v1, v2_job_store=v2, v2_groq=V2GroqSummary())) as client:
        def run(version, key):
            start = perf_counter()
            response = client.post(paths[version], headers={"X-Request-ID": "local-fixture", "Idempotency-Key": key},
                                   json=old_payload if version == "v1" else payload)
            assert response.status_code == 200 and "event: done" in response.text and "event: error" not in response.text
            job_id = response.headers["X-Analysis-Job-ID"]
            store, other = (v1, v2) if version == "v1" else (v2, v1)
            stored = store.get(job_id)
            assert stored["status"] == "DONE" and other.get(job_id) is None
            results = [json.loads(text) for text in response.text.splitlines() if text.startswith("data: ")
                       and '"text_score":' in text for text in (text[6:],)]
            assert [{k: v for k, v in row.items() if k != "request_id"} for row in results] == stored["result"]["results"]
            assert all(row["text_score"] >= 0 for row in results)
            return {"version": version, "job_id": job_id, "wall_seconds": perf_counter() - start,
                    "scores": [row["text_score"] for row in results], "peak_mib": peak_working_set_mib()}

        for order in (("v1", "v2"), ("v2", "v1")):
            key = uuid4().hex
            runs.append([run(version, key) for version in order])
        key = uuid4().hex
        mark, cpu = perf_counter(), process_time()
        with ThreadPoolExecutor(max_workers=2) as pool:
            concurrent = list(pool.map(lambda version: run(version, key), ("v1", "v2")))
        parallel_wall, parallel_cpu = perf_counter() - mark, process_time() - cpu
        replay = [run(version, key) for version in ("v1", "v2")]
        for new, original in zip(replay, concurrent, strict=True):
            assert new["job_id"] == original["job_id"] and new["scores"] == original["scores"]
        assert concurrent[0]["scores"] != concurrent[1]["scores"]
        assert client.get("/health").status_code == 200
    return {"runs": runs, "parallel": concurrent, "replay": replay, "parallel_wall_seconds": parallel_wall,
            "parallel_cpu_seconds": parallel_cpu, "peak_before_mib": base, "peak_mib": peak_working_set_mib(),
            "mysql_complete_seconds": commit_seconds}


def live_groq(payload):
    from dotenv import load_dotenv

    load_dotenv(LOCAL_OUTPUT.parent / ".env", override=False)
    key = os.getenv("GROQ_API_KEY_PRIMARY", "").strip()
    if not key:
        raise ValueError("No configured Groq primary key; no request made")
    client = V2GroqSummary(key=key, timeout=float(os.getenv("GROQ_TIMEOUT_SECONDS", "5")))

    class Recorder:
        calls = []

        def enrich(self, paragraphs):
            outcome = client.enrich(paragraphs, audit=True)
            self.calls.append({**asdict(outcome), "inputs": len(paragraphs)})
            return outcome

    recorder = Recorder()
    mark = perf_counter()
    result = evaluate_v2(DataAnalyzeRequestV2.model_validate(payload), groq=recorder)
    return {"model": MODEL, "temperature": 0.3, "top_p": 1.0, "reasoning_effort": "low",
            "max_completion_tokens": MAX_COMPLETION_TOKENS, "calls": recorder.calls, "completed_at": time(),
            "total_wall_seconds": perf_counter() - mark, "results": result.model_dump(mode="json")["results"]}


def container_smoke(payload, workers):
    import httpx

    port = 18317 if workers == 1 else 28317
    name = f"review-ai-v2-smoke-w{workers}-261117"
    origin = f"http://127.0.0.1:{port}"
    cold = perf_counter()
    for _ in range(100):
        try:
            if httpx.get(origin + "/health", timeout=1).is_success:
                break
        except httpx.HTTPError:
            pass
        sleep(.25)
    else:
        raise RuntimeError("Isolated container health did not become ready")
    warm = perf_counter() - cold
    end, measurements = Event(), []

    def sample():
        while not end.is_set():
            result = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}|{{.CPUPerc}}", name],
                                    capture_output=True, text=True, timeout=8)
            if result.returncode == 0:
                measurements.append(result.stdout.strip())

    old_payload = {**payload, "reviews": [{k: v for k, v in row.items() if k != "image_count"} for row in payload["reviews"]]}

    def run(version, key):
        mark = perf_counter()
        response = httpx.post(f"{origin}/api/v{version}/data/analyze/stream", timeout=90,
                              headers={"X-Internal-Token": "v2_synthetic_only", "X-Request-ID": "container-fixture",
                                       "Idempotency-Key": key}, json=old_payload if version == 1 else payload)
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        if response.status_code != 200 or "event: done" not in response.text or "event: error" in response.text:
            raise RuntimeError(f"isolated v{version} returned {response.status_code}, "
                               f"SSE error codes: {[obj['code'] for obj in events if 'code' in obj]}")
        rows = [row for row in events if "text_score" in row]
        assert len(rows) == len(payload["reviews"]) and all(row["text_score"] >= 0 for row in rows)
        assert all(("policy_index_v3" in row) == (version == 2) for row in rows)
        return {"version": version, "seconds": perf_counter() - mark, "id": response.headers["X-Analysis-Job-ID"],
                "scores": [row["text_score"] for row in rows]}

    thread = Thread(target=sample, daemon=True)
    thread.start()
    try:
        key = uuid4().hex
        assert httpx.post(f"{origin}/api/v2/data/analyze/stream", json=payload,
                          headers={"X-Request-ID": "container-fixture", "Idempotency-Key": key}).status_code == 401
        jobs = [(version, job_key) for job_key in (key, uuid4().hex) for version in (1, 2)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            measured = list(pool.map(lambda job: run(*job), jobs))
        assert measured[0]["scores"] != measured[1]["scores"]
        assert all(run(i + 1, key)["id"] == measured[i]["id"] for i in range(2))
    finally:
        end.set()
        thread.join(timeout=9)
    return {"workers": workers, "caps": {"cpus": 2, "memory_gib": 4}, "health_poll_seconds": warm,
            "parallel_requests": measured, "stats_samples": measurements}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("model-v1", "model-v2", "streams", "live-four", "live-twelve", "container-smoke"), required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--prior", type=Path)
    parser.add_argument("--workers", type=int, choices=(1, 2), default=1)
    args = parser.parse_args()
    if args.output.resolve().parent != LOCAL_OUTPUT or args.output.suffix != ".json" or args.output.exists():
        parser.error("New ignored JSON only")
    if args.mode == "live-twelve":
        prior = json.loads(args.prior.read_text(encoding="utf-8")) if args.prior else {}
        calls = prior.get("calls", [])
        if (prior.get("mode") != "live-four" or len(calls) != 1 or calls[0]["http_status"] != 200
                or calls[0]["limits"].get("x-ratelimit-remaining-requests", 0) < 1
                or time() - prior["completed_at"] < INTERVAL_SECONDS):
            parser.error("Do not run the second batch before stage 1 success and a full rate window")
    payload = fixture_payload(args.fixture, four=args.mode == "live-four")
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", TOKENIZERS_PARALLELISM="false")
    if args.mode != "container-smoke":
        import torch

        torch.set_num_threads(1)
        torch.set_num_interop_threads(1)
    if args.mode == "container-smoke":
        report = container_smoke(payload, args.workers)
    elif args.mode == "streams":
        report = streams(payload)
    elif args.mode.startswith("model-"):
        report = model_profile(args.mode, payload)
    else:
        report = live_groq(payload)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump({"mode": args.mode, **report}, stream, ensure_ascii=False, indent=2, allow_nan=False)
    print(f"Saved local verification: {args.mode}")


if __name__ == "__main__":
    main()
