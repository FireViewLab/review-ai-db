"""Measure an already-started disposable Worker/batch configuration over local HTTP."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import statistics
import subprocess
import threading
import time
from urllib.parse import urlsplit
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener
from uuid import uuid4

TOKEN = "synthetic-local-validation-token"
EXPECTED_CONTAINER = "review-ai-runtime-check-api"
EXPECTED_PROJECT = "review-ai-runtime-check"
LOG_PREFIX = "BENCHMARK_EVENT "
LOCAL_HTTP = build_opener(ProxyHandler({}))
PROCESS_SAMPLE = """
import json, os
from pathlib import Path
rows=[]
for path in Path('/proc').iterdir():
    if not path.name.isdigit() or int(path.name)==os.getpid(): continue
    try:
        command=(path/'cmdline').read_bytes().replace(b'\\0',b' ').decode(errors='replace')
        if 'uvicorn' not in command and 'spawn_main' not in command: continue
        row={'pid':int(path.name)}
        for line in (path/'status').read_text().splitlines():
            name,_,raw=line.partition(':')
            if name in ('VmRSS','VmHWM'): row[name+'_bytes']=int(raw.strip().split()[0])*1024
        stat=(path/'stat').read_text().rsplit(')',1)[1].split()
        row['cpu_seconds']=(int(stat[11])+int(stat[12]))/os.sysconf('SC_CLK_TCK')
        rows.append(row)
    except (OSError,ValueError,IndexError): pass
print(json.dumps(rows))
"""


def docker(*args, timeout=15) -> str:
    result = subprocess.run(["docker", *args], capture_output=True, text=True,
                            encoding="utf-8", check=True, timeout=timeout)
    return result.stdout.strip()


def verify_target(args) -> None:
    url = urlsplit(args.base_url)
    if (not args.allow_disposable or url.scheme != "http"
            or url.hostname not in {"127.0.0.1", "localhost", "::1"}
            or url.username or url.password or url.query or url.fragment
            or url.path not in {"", "/"} or args.container_name != EXPECTED_CONTAINER):
        raise ValueError("Explicit disposable acknowledgement and local validation container are required")
    project = docker("inspect", "--format",
                     '{{ index .Config.Labels "com.docker.compose.project" }}', args.container_name)
    if project != EXPECTED_PROJECT:
        raise ValueError("Container must belong to the disposable validation Compose project")
    # Inspect only non-secret fields inside the container; never print its full environment.
    guard = ("import os; assert os.getenv('BENCHMARK_DISPOSABLE')=='1'; "
             "assert os.getenv('DB_NAME')=='runtime_validation'; "
             "assert os.getenv('DB_HOST')=='ai-db'; "
             "assert os.getenv('ENABLE_GROQ_REASON_NATURALIZATION','0')=='0'; print('ok')")
    if docker("exec", args.container_name, "python", "-c", guard) != "ok":
        raise ValueError("Disposable runtime guard failed")


def json_get(base_url: str, path: str) -> dict:
    request = Request(base_url.rstrip("/") + path,
                      headers={"X-Internal-Token": TOKEN, "Connection": "close"})
    with LOCAL_HTTP.open(request, timeout=15) as response:
        return json.load(response)


def wait_for_health(base_url: str, timeout: float = 120) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            if json_get(base_url, "/health") == {"status": "ok"}:
                return
        except Exception:
            pass
        if time.monotonic() >= deadline:
            raise TimeoutError("Disposable API did not become healthy")
        time.sleep(.25)


def synthetic_reviews(count: int) -> list[dict]:
    contents = [
        "배송이 빠르고 포장이 꼼꼼했습니다. 제품을 사용해 보니 설명대로 작동했습니다.",
        "색상은 사진보다 조금 진하지만 마감이 좋습니다. 일주일 사용 후 후기를 남깁니다.",
        "생각보다 크기가 작습니다. 설치는 쉬웠고 가격 대비 성능은 괜찮았습니다.",
        "상자가 찌그러져 도착했지만 제품에는 문제가 없었습니다. 조금 더 사용해 보겠습니다.",
    ]
    return [{"review_id": f"benchmark-review-{index:04d}",
             "content": contents[index % len(contents)] + f" 검증용 리뷰 번호 {index}."}
            for index in range(count)]


def memory_bytes(usage: str) -> int:
    raw = usage.split("/", 1)[0].strip()
    match = re.fullmatch(r"([0-9.]+)\s*([KMGT]?i?B)", raw, re.IGNORECASE)
    if not match:
        raise ValueError("Unexpected Docker memory usage format")
    units = {"b": 1, "kb": 1000, "mb": 1000 ** 2, "gb": 1000 ** 3,
             "tb": 1000 ** 4, "kib": 1024, "mib": 1024 ** 2,
             "gib": 1024 ** 3, "tib": 1024 ** 4}
    return int(float(match[1]) * units[match[2].lower()])


def check_authentication(base_url: str) -> None:
    payload = dict(platform="mall", product_id="benchmark-auth", reviews=synthetic_reviews(1))
    request = Request(base_url.rstrip("/") + "/api/v1/data/analyze/stream",
                      data=json.dumps(payload).encode(), method="POST",
                      headers={"Content-Type": "application/json", "X-Request-ID": "benchmark-auth",
                               "Idempotency-Key": "benchmark-auth", "Connection": "close"})
    try:
        with LOCAL_HTTP.open(request, timeout=15):
            raise AssertionError("Unauthenticated benchmark request unexpectedly succeeded")
    except HTTPError as exc:
        assert exc.code == 401


def post_sse(base_url: str, payload: dict, key: str, request_id: str, barrier=None) -> dict:
    if barrier is not None:
        barrier.wait(timeout=10)
    request = Request(base_url.rstrip("/") + "/api/v1/data/analyze/stream",
                      data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                      headers={"Content-Type": "application/json", "X-Internal-Token": TOKEN,
                               "X-Request-ID": request_id, "Idempotency-Key": key,
                               "Connection": "close"}, method="POST")
    started = time.perf_counter()
    events = []
    with LOCAL_HTTP.open(request, timeout=300) as response:
        assert response.status == 200
        assert response.headers.get_content_type() == "text/event-stream"
        assert response.headers["X-Request-ID"] == request_id
        job_id = response.headers["X-Analysis-Job-ID"]
        event_name, data_lines = "", []
        for raw_line in response:
            line = raw_line.decode("utf-8").rstrip("\r\n")
            if not line:
                if event_name:
                    events.append((event_name, json.loads("\n".join(data_lines))))
                event_name, data_lines = "", []
            elif line.startswith("event:"):
                event_name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
    seconds = time.perf_counter() - started
    assert events and events[0][0] == "meta" and events[-1][0] == "done"
    assert not any(name == "error" for name, _ in events)
    assert all(data.get("request_id") == request_id for _, data in events)
    meta = events[0][1]
    assert meta["ai_job_id"] == job_id
    assert (meta["platform"], meta["product_id"], meta["review_count"]) == (
        payload["platform"], payload["product_id"], len(payload["reviews"]))
    results = [{key: value for key, value in data.items() if key != "request_id"}
               for name, data in events if name == "result"]
    assert [row["review_id"] for row in results] == [row["review_id"] for row in payload["reviews"]]
    assert events[-1][1]["result_count"] == len(results) == len(payload["reviews"])
    assert events[-1][1]["ai_job_id"] == job_id
    assert all(isinstance(row["reasons"], list) and all(isinstance(reason, str) for reason in row["reasons"])
               for row in results)
    stored = json_get(base_url, "/__benchmark__/jobs/" + job_id)
    expected = dict(platform=payload["platform"], product_id=payload["product_id"],
                    review_count=len(results), results=results)
    assert stored == dict(job_id=job_id, status="DONE", result=expected)
    return dict(seconds=seconds, job_id=job_id, events=[name for name, _ in events],
                versions={name: meta[name] for name in ("contract_version", "model_version", "policy_version")},
                results=results, payload=payload, key=key)


class Sampler:
    def __init__(self, container):
        self.container = container
        self.phase = "cold"
        self.samples = []
        self.errors = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._collect, daemon=True)

    def _collect(self):
        while not self._stop.is_set():
            try:
                raw = json.loads(docker("stats", "--no-stream", "--format", "{{json .}}", self.container))
                processes = json.loads(docker("exec", self.container, "python", "-c", PROCESS_SAMPLE))
                self.samples.append(dict(phase=self.phase, monotonic=time.perf_counter(),
                                         cpu_percent=float(raw["CPUPerc"].rstrip("%")),
                                         memory_usage=raw["MemUsage"],
                                         container_memory_bytes=memory_bytes(raw["MemUsage"]),
                                         processes=processes))
            except Exception as exc:
                self.errors.append(type(exc).__name__)
            self._stop.wait(.25)

    def start(self):
        self._thread.start()

    def close(self):
        self._stop.set()
        self._thread.join(timeout=35)


def parse_observed_events(raw: str) -> list[dict]:
    decoder = json.JSONDecoder()
    return [decoder.raw_decode(part.lstrip())[0] for part in raw.split(LOG_PREFIX)[1:]]


def observed_events(container: str) -> list[dict]:
    return parse_observed_events(docker("logs", container))


def overlap_pairs(events: list[dict]) -> list[dict]:
    starts = {row["analysis_id"]: row for row in events if row["kind"] == "analysis_start"}
    intervals = [(starts[row["analysis_id"]], row) for row in events
                 if row["kind"] == "analysis_end" and row["analysis_id"] in starts and row["succeeded"]]
    pairs = []
    for index, (start, end) in enumerate(intervals):
        for other_start, other_end in intervals[index + 1:]:
            overlap = min(end["monotonic"], other_end["monotonic"]) - max(
                start["monotonic"], other_start["monotonic"])
            if start["pid"] != other_start["pid"] and overlap > 0:
                pairs.append(dict(pids=[start["pid"], other_start["pid"]], seconds=overlap,
                                  products=[start["product_id"], other_start["product_id"]]))
    return pairs


def percentile95(values: list[float]) -> float:
    ordered = sorted(values)
    position = .95 * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def check_concurrent_idempotency(base_url: str, container: str, run_id: str,
                                 reviews: list[dict], expected_results: list[dict]) -> dict:
    payload = dict(platform="mall", product_id=f"benchmark-{run_id}-idempotency", reviews=reviews)
    key = f"benchmark-{run_id}-shared-key"
    barrier = threading.Barrier(2)

    def attempt(index):
        try:
            result = post_sse(base_url, payload, key, f"benchmark-concurrent-idempotency-{index}", barrier)
            return dict(status=200, result=result)
        except HTTPError as exc:
            if exc.code != 409:
                raise
            code = json.load(exc)["detail"]["code"]
            assert code == "IDEMPOTENCY_IN_PROGRESS"
            return dict(status=409, code=code)

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, range(2)))
    successful = [row["result"] for row in outcomes if row["status"] == 200]
    assert successful and len({row["job_id"] for row in successful}) == 1
    assert all(row["results"] == expected_results for row in successful)
    starts = [row for row in observed_events(container)
              if row["kind"] == "analysis_start" and row["product_id"] == payload["product_id"]]
    assert len(starts) == 1, "Concurrent identical keys must start exactly one analysis"
    return dict(seconds=time.perf_counter() - started, statuses=[row["status"] for row in outcomes],
                analysis_started_count=len(starts), job_id=successful[0]["job_id"],
                successful_sse_count=len(successful),
                expected_in_progress_count=sum(row["status"] == 409 for row in outcomes))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-disposable", action="store_true", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--container-name", default=EXPECTED_CONTAINER)
    parser.add_argument("--reviews", type=int, default=32, choices=range(1, 501), metavar="1..500")
    parser.add_argument("--rounds", type=int, default=2, choices=range(1, 101), metavar="1..100")
    parser.add_argument("--concurrency", type=int, default=2, choices=(2, 3, 4))
    parser.add_argument("--workers", type=int, required=True, choices=(1, 2))
    parser.add_argument("--batch-size", type=int, required=True, choices=(1, 8, 16, 32))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    verify_target(args)
    wait_for_health(args.base_url)
    check_authentication(args.base_url)
    state = json_get(args.base_url, "/__benchmark__/state")
    assert state["workers"] == args.workers and state["batch_size"] == args.batch_size
    assert state["single_baseline"] == (args.batch_size == 1)
    assert state["model_cache"]["currsize"] == 0 and not state["events"], "Recreate the API container before each cold phase"
    run_id = str(uuid4())
    reviews = synthetic_reviews(args.reviews)
    sampler = Sampler(args.container_name)
    report = dict(run_id=run_id, workers=args.workers, batch_size=args.batch_size,
                  single_baseline=state["single_baseline"], reviews_per_request=args.reviews,
                  concurrency=args.concurrency, warm_rounds=args.rounds, versions=state["versions"],
                  external_network_used=False, sample_limit="Small synthetic sample; p95 is descriptive, not a production capacity estimate.")

    def round_requests(phase: str, number: int):
        barrier = threading.Barrier(args.concurrency)
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            pending = []
            for index in range(args.concurrency):
                product_id = f"benchmark-{run_id}-{phase}-{number}-{index}"
                payload = dict(platform="mall", product_id=product_id, reviews=reviews)
                pending.append(pool.submit(post_sse, args.base_url, payload,
                                           f"benchmark-{run_id}-{phase}-{number}-{index}",
                                           f"benchmark-request-{phase}-{number}-{index}", barrier))
            return [future.result() for future in pending]

    sampler.start()
    try:
        all_started = time.perf_counter()
        cold = round_requests("cold", 0)
        warmups = []
        # The shared Uvicorn listener does not guarantee one first request per Worker.
        # Keep cold model loading out of warm timings even if routing initially chooses one PID.
        for number in range(4):
            initial_models = [row for row in observed_events(args.container_name)
                              if row["kind"] == "model_load_end" and row["succeeded"]]
            if len({row["pid"] for row in initial_models}) == args.workers:
                break
            warmups.extend(round_requests("warmup", number))
        assert len({row["pid"] for row in observed_events(args.container_name)
                    if row["kind"] == "model_load_end" and row["succeeded"]}) == args.workers, (
            "Not all configured Workers received a request; retry with greater concurrency")
        sampler.phase = "warm"
        warm_started = time.perf_counter()
        warm = [row for number in range(args.rounds) for row in round_requests("warm", number)]
        warm_seconds = time.perf_counter() - warm_started
        sampler.phase = "idempotency"
        idempotency = check_concurrent_idempotency(
            args.base_url, args.container_name, run_id, reviews, cold[0]["results"])
        events_before = observed_events(args.container_name)
        sampler.phase = "replay"
        original = warm[0]
        replay = post_sse(args.base_url, original["payload"], original["key"], "benchmark-replay")
        events = observed_events(args.container_name)
        assert replay["job_id"] == original["job_id"] and replay["results"] == original["results"]
        assert replay["events"] == ["meta"] + ["result"] * args.reviews + ["done"]
        assert [row for row in events if row["kind"] == "analysis_start"] == [
            row for row in events_before if row["kind"] == "analysis_start"]
        assert all(row["results"] == cold[0]["results"] for row in [*cold, *warmups, *warm])
        assert all(row["versions"] == state["versions"] for row in [*cold, *warmups, *warm, replay])
        # Several short independent connections observe worker-local state without model inference.
        states = {}
        for _ in range(16):
            observation = json_get(args.base_url, "/__benchmark__/state")
            states[str(observation["pid"])] = observation
            if len(states) == args.workers:
                break
        models = [row for row in events if row["kind"] == "model_load_end"]
        assert models and all(row["succeeded"] and row["device"] == "cpu" for row in models)
        loaded_pids = sorted({row["pid"] for row in models})
        assert len(models) == len(loaded_pids), "Each observed Worker must load its model only once"
        latencies = [row["seconds"] for row in warm]
        pairs = overlap_pairs(events)
        report.update(
            total_seconds=time.perf_counter() - all_started,
            cold_request_seconds=[row["seconds"] for row in cold],
            additional_model_warmup_seconds=[row["seconds"] for row in warmups],
            warm_total_seconds=warm_seconds, warm_request_seconds=latencies,
            warm_mean_seconds=statistics.mean(latencies), warm_p95_seconds=percentile95(latencies),
            warm_reviews_per_second=len(warm) * args.reviews / warm_seconds,
            model_loading=models, loaded_worker_pids=loaded_pids,
            cross_process_overlap_observed=bool(pairs), overlap_pairs=pairs,
            sse_success_count=len(cold) + len(warmups) + len(warm) + 1 + idempotency["successful_sse_count"],
            sse_failure_count=0, concurrent_idempotency=idempotency,
            replay_seconds=replay["seconds"], replay_without_analysis=True,
            stored_results_match=True, scores_identical_across_requests=True,
            sample_results=cold[0]["results"], states=states, analysis_events=events,
        )
    finally:
        sampler.close()
    report["resource_samples"] = sampler.samples
    report["resource_sampling_errors"] = sampler.errors
    report["peak_cpu_percent"] = max((row["cpu_percent"] for row in sampler.samples), default=None)
    report["peak_sampled_container_memory_bytes"] = max(
        (row["container_memory_bytes"] for row in sampler.samples), default=None)
    report["peak_sampled_process_rss_bytes"] = max(
        (sum(proc.get("VmRSS_bytes", 0) for proc in row["processes"]) for row in sampler.samples), default=None)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("workers", "batch_size", "warm_reviews_per_second",
          "warm_mean_seconds", "warm_p95_seconds", "cross_process_overlap_observed", "sse_success_count")},
          ensure_ascii=False))


if __name__ == "__main__":
    main()
