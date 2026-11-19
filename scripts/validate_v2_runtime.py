"""Pinned synthetic v2 verification: real CPU models, standalone file-backed SSE, or one live Groq batch."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
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
    from app.v2_server import create_v2_app

    with TemporaryDirectory(prefix="v2-files-", dir=LOCAL_OUTPUT) as directory:
        os.environ.update(V2_JOB_DIR=directory, INTERNAL_TOKEN="local-synthetic-only",
                          ENABLE_V2_GROQ_SUMMARY="0")
        expected = evaluate_v2(DataAnalyzeRequestV2.model_validate(payload)).model_dump(mode="json")
        key = uuid4().hex
        headers = {"X-Request-ID": "local-fixture", "Idempotency-Key": key,
                   "X-Internal-Token": os.environ["INTERNAL_TOKEN"]}
        with TestClient(create_v2_app()) as client:
            fresh = read_stream(client.post(V2_PATH, headers=headers, json=payload), payload)
            assert fresh["results"] == expected["results"]
        with TestClient(create_v2_app()) as client:
            replay = read_stream(client.post(V2_PATH, headers=headers, json=payload), payload, replay=True)
            assert replay == fresh
        return {"request_rows": len(payload["reviews"]), "weights_sha256": sha256(
                Path(os.environ["PTEXT_V31B_MODEL_PATH"]) / "model.safetensors"), **fresh}


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


V2_PATH = "/api/v2/data/analyze/stream"


def frame_data(response):
    return [(lines[0].removeprefix("event: "), json.loads(lines[1].removeprefix("data: ")))
            for block in response.text.strip().split("\n\n") if (lines := block.splitlines())]


def read_stream(response, payload, *, replay=False):
    assert response.status_code == 200, response.status_code
    assert response.headers["content-type"].startswith("text/event-stream")
    frames = frame_data(response)
    names = [name for name, _ in frames]
    meta, done = frames[0][1], frames[-1][1]
    count = len(payload["reviews"])
    expected = ["meta", *(["result"] * count), "done"]
    assert [name for name in names if name != "heartbeat"] == (
        expected if replay else ["meta", "progress", *expected[1:]]), names
    if replay:
        assert "heartbeat" not in names
    from app.contracts.data_ai_v2 import v2_versions
    assert all(meta[name] == value for name, value in v2_versions().items())
    assert done["result_count"] == meta["review_count"] == count
    job_id = response.headers["X-Analysis-Job-ID"]
    assert job_id == meta["ai_job_id"] == done["ai_job_id"]
    assert all(row["request_id"] == response.headers["X-Request-ID"] for _, row in frames)
    results = [{k: value for k, value in row.items() if k != "request_id"} for name, row in frames if name == "result"]
    assert [row["review_id"] for row in results] == [row["review_id"] for row in payload["reviews"]]
    assert all(row["text_score"] >= 0 and "rti" not in row for row in results)
    return {"job_id": job_id, "results": results, "versions": v2_versions()}


def expanded(payload):
    return {**payload, "reviews": [
        {**row, "review_id": f"{row['review_id']}-{i}"}
        for i in range(8) for row in payload["reviews"]
    ]}


def container_smoke(payload, port, *, prior=None):
    import httpx

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
        raise RuntimeError("Isolated v2 container health did not become ready")
    ready = perf_counter() - cold

    def headers(key, trace, *, auth=True):
        base = {"X-Request-ID": trace, "Idempotency-Key": key}
        return {**base, "X-Internal-Token": os.environ["V2_INTERNAL_TOKEN"]} if auth else base

    def post(body, key, trace):
        return httpx.post(origin + V2_PATH, timeout=90, headers=headers(key, trace), json=body)

    def replay_all(jobs):
        for job in jobs:
            body = expanded(payload) if job["expanded"] else payload
            actual = read_stream(post(body, job["key"], "container-replay"), body, replay=True)
            assert actual == job["verified"]
        return len(jobs)

    if prior is not None:
        return {"health_poll_seconds": ready, "replayed_after_recreate": replay_all(prior["jobs"])}

    key = uuid4().hex
    assert httpx.post(origin + V2_PATH, json=payload, headers=headers(key, "no-auth", auth=False)).status_code == 401
    paths = httpx.get(origin + "/openapi.json").json()["paths"]
    assert V2_PATH in paths and "/api/v1/data/analyze/stream" not in paths

    # Keep the first stream open; its cold/large inference gives time for a duplicate at HTTP level.
    large = expanded(payload)
    with httpx.stream("POST", origin + V2_PATH, timeout=90, headers=headers(key, "overlap"), json=large) as first:
        assert first.status_code == 200 and next(first.iter_lines()) == "event: meta"
        duplicate = post(large, key, "duplicate")
        assert duplicate.status_code == 409 and duplicate.json()["detail"]["code"] == "IDEMPOTENCY_IN_PROGRESS"
        changed = post(payload, key, "changed")
        assert changed.status_code == 409 and changed.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REUSED"
        # Intentionally disconnect before reading result/done, then replay the durable result.
        original_job = first.headers["X-Analysis-Job-ID"]
    for _ in range(180):
        replay = post(large, key, "after-disconnect")
        if replay.status_code != 409:
            break
        assert replay.json()["detail"]["code"] == "IDEMPOTENCY_IN_PROGRESS"
        sleep(.25)
    else:
        raise RuntimeError("Interrupted stream did not complete")
    recovered = read_stream(replay, large, replay=True)
    assert recovered["job_id"] == original_job

    def run(key):
        response = post(payload, key, "container-fresh")
        return {"key": key, "expanded": False, "verified": read_stream(response, payload)}

    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = list(pool.map(run, (uuid4().hex, uuid4().hex)))
    assert jobs[0]["verified"]["results"] == jobs[1]["verified"]["results"]
    jobs.append({"key": key, "expanded": True, "verified": recovered})
    return {"health_poll_seconds": ready, "concurrent_fresh": 2, "overlap_reviews": len(large["reviews"]),
            "disconnected_and_replayed": True, "replay_count": replay_all(jobs), "jobs": jobs}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("model-v1", "model-v2", "streams", "live-four", "live-twelve", "container-smoke", "container-replay"), required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--prior", type=Path)
    parser.add_argument("--port", type=int, default=8001)
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
    if not args.mode.startswith("container-"):
        import torch

        torch.set_num_threads(1)
        torch.set_num_interop_threads(1)
    if args.mode == "container-smoke":
        report = container_smoke(payload, args.port)
    elif args.mode == "container-replay":
        if not args.prior:
            parser.error("--prior must name the container-smoke JSON")
        report = container_smoke(payload, args.port, prior=json.loads(args.prior.read_text(encoding="utf-8")))
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
