"""외부 모델과 전용 MySQL로 CPU·API·SSE·저장 통합 smoke를 실제 실행한다."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time


# `python scripts/...`에서도 저장소의 app을 가져온다.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def memory_snapshot() -> dict:
    """Linux 프로세스 실측값만 반환한다. 제공되지 않는 수치는 추정하지 않는다."""
    values = {}
    status = Path("/proc/self/status")
    if status.is_file():
        for line in status.read_text().splitlines():
            name, _, raw = line.partition(":")
            if name in {"VmRSS", "VmHWM", "VmSize"}:
                values[name + "_mib"] = round(int(raw.strip().split()[0]) / 1024, 2)
    try:
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        values["peak_rss_mib"] = round(peak / (1024 * 1024 if sys.platform == "darwin" else 1024), 2)
    except ImportError:
        pass
    return values


def timed(callable_):
    started = time.perf_counter()
    result = callable_()
    return result, round(time.perf_counter() - started, 4)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Optional measurement JSON path")
    parser.add_argument("--cpu-threads", type=int, default=2)
    parser.add_argument("--max-reviews", type=int, default=100, choices=(3, 10, 100))
    parser.add_argument("--include-max-length", action="store_true",
                        help="Also benchmark synthetic reviews truncated at the model's configured max_length")
    args = parser.parse_args()
    if os.getenv("RUN_MYSQL_TESTS") != "1":
        parser.error("RUN_MYSQL_TESTS=1 must explicitly identify a disposable validation database")
    model_path = os.environ.get("PTEXT_MODEL_PATH")
    if not model_path or not Path(model_path).is_dir():
        parser.error("PTEXT_MODEL_PATH must point to the mounted final model directory")
    if args.cpu_threads < 1:
        parser.error("--cpu-threads must be positive")

    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    os.environ["INTERNAL_TOKEN"] = "synthetic-local-integration-token"
    os.environ["DATA_INTERNAL_TOKEN"] = "synthetic-local-crawler-token"
    os.environ["REQUIRE_INTERNAL_TOKEN"] = "1"
    os.environ["ENABLE_EXPERIMENTAL_COLLECTION"] = "1"
    os.environ["DATA_SERVER_BASE_URL"] = "https://data.test"
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"

    report = {"environment": {"platform": sys.platform, "cpu_threads": args.cpu_threads,
                               "worker_count": 1, "external_network_used": False},
              "memory": {"before_runtime_import": memory_snapshot()}, "checks": {}}

    import torch
    import httpx
    from fastapi.testclient import TestClient
    from app.analyzers import p_text
    from app.core.crawler_settings import CrawlerSettings
    from app.factory import create_app
    from app.integrations.crawler_stream import CrawlerStreamClient, iter_sse_frames, stream_reviews_path
    from app.repositories.mysql_jobs import MySQLJobStore
    from app.services.analysis import analyze_reviews

    torch.set_num_threads(args.cpu_threads)
    torch.set_num_interop_threads(1)
    assert not torch.cuda.is_available(), "This validation is deliberately CPU-only"
    assert p_text._predictor.cache_info().currsize == 0, "Smoke must start before the lazy model loads"
    report["environment"]["torch_version"] = torch.__version__
    report["memory"]["before_model_load"] = memory_snapshot()
    store = MySQLJobStore.from_env()
    job_ids = []
    app = create_app(job_store=store)
    headers = {"X-Internal-Token": os.environ["INTERNAL_TOKEN"]}

    sample_reviews = [
        {"review_id": "00:01", "content": "배송이 빠르고 포장이 꼼꼼했어요. 제품도 설명대로 작동해서 만족합니다."},
        {"review_id": "00:02", "content": "색상은 사진보다 조금 진하지만 마감이 좋아요. 일주일 써보고 다시 후기 남깁니다."},
        {"review_id": "00:03", "content": "생각보다 크기가 작네요. 그래도 설치는 쉬웠고 가격 대비 성능은 괜찮습니다."},
    ]
    payload = {"platform": "mall", "product_id": "0007", "reviews": sample_reviews}

    try:
        with TestClient(app) as client:
            assert p_text._predictor.cache_info().currsize == 0
            health = client.get("/health")
            assert health.status_code == 200 and health.json() == {"status": "ok"}
            assert p_text._predictor.cache_info().currsize == 0
            report["checks"]["startup_and_health_do_not_load_model"] = True
            assert client.post("/api/v1/data/analyze", json=payload).status_code == 401
            assert p_text._predictor.cache_info().currsize == 0
            report["checks"]["authentication_precedes_inference"] = True
            assert client.post("/api/v1/analyze", json=payload).status_code == 404
            report["checks"]["retired_legacy_endpoint_404"] = True

            predictor, loading_seconds = timed(lambda: p_text._predictor(model_path))
            assert predictor.device == "cpu"
            report["model_loading"] = {"seconds": loading_seconds, "device": predictor.device,
                                        "threshold": predictor.settings["threshold"],
                                        "max_length": predictor.settings["max_length"]}
            report["memory"]["after_model_load"] = memory_snapshot()
            prediction, inference_seconds = timed(lambda: p_text.predict_text_score(sample_reviews[0]["content"]))
            assert 0 <= prediction["text_score"] <= 100 and prediction["suspicious_probability"] is not None
            report["single_inference"] = {"seconds": inference_seconds, "prediction": prediction}
            report["memory"]["after_single_inference"] = memory_snapshot()

            single = analyze_reviews(platform="mall", product_id="0007", reviews=sample_reviews[:1])["results"][0]
            assert single["text_score"] != -1
            assert single["behavior_score"] == single["network_score"] == -1
            assert single["rti"] == single["text_score"]
            report["checks"]["real_single_review_missing_behavior_network"] = True
            report["single_analysis"] = single

            expected, seconds = timed(lambda: analyze_reviews(**payload))
            assert expected["review_count"] == 3
            assert all(result["text_score"] != -1 and result["network_score"] == -1
                       and result["behavior_score"] == -1 for result in expected["results"])
            report["analysis_3"] = {"seconds": seconds, "result": expected}
            report["memory"]["after_3_reviews"] = memory_snapshot()

            for count in (10, 100):
                if count > args.max_reviews:
                    continue
                rows = [{"review_id": f"sample-{index:03d}",
                         "content": sample_reviews[index % 3]["content"] + f" 테스트 번호 {index}."}
                        for index in range(count)]
                result, seconds = timed(lambda: analyze_reviews(platform="mall", product_id="0007", reviews=rows))
                assert result["review_count"] == count and all(row["text_score"] != -1 for row in result["results"])
                report[f"analysis_{count}"] = {"seconds": seconds, "review_count": count}
                report["memory"][f"after_{count}_reviews"] = memory_snapshot()

            if args.include_max_length:
                max_length = predictor.settings["max_length"]
                long_content = (
                    "사용한지 일주일 되었고 배송 상태는 양호하지만 실제 색감은 사진보다 조금 어두웠습니다. "
                    "포장은 꼼꼼했고 설치는 간단했으며 며칠 더 사용해보고 내구성을 확인하려고 합니다. "
                ) * 40
                source_token_count = (len(predictor.tokenizer.tokenize(long_content)) +
                                      predictor.tokenizer.num_special_tokens_to_add(pair=False))
                assert source_token_count > max_length
                long_rows = [{"review_id": f"long-{index:03d}",
                              "content": long_content + f" 검증 번호 {index}."}
                             for index in range(args.max_reviews)]
                encoded = predictor.tokenizer(long_rows[0]["content"], truncation=True, max_length=max_length)
                truncated_token_count = len(encoded["input_ids"])
                assert truncated_token_count == max_length
                result, seconds = timed(lambda: analyze_reviews(
                    platform="mall", product_id="0007", reviews=long_rows,
                ))
                assert result["review_count"] == args.max_reviews
                assert all(row["text_score"] != -1 for row in result["results"])
                report["analysis_max_length"] = {
                    "seconds": seconds, "review_count": args.max_reviews,
                    "source_token_count_without_index_suffix": source_token_count,
                    "truncated_token_count": truncated_token_count, "max_length": max_length,
                }
                report["memory"]["after_max_length_reviews"] = memory_snapshot()

            normal, seconds = timed(lambda: client.post("/api/v1/data/analyze", json=payload, headers=headers))
            assert normal.status_code == 200
            job_ids.append(normal.headers["X-Analysis-Job-ID"])
            assert normal.json() == expected
            saved = store.get(job_ids[-1])
            assert saved["status"] == "DONE" and saved["result"] == normal.json()
            assert saved["request"]["product_id"] == "0007"
            assert [row["review_id"] for row in saved["request"]["reviews"]] == [row["review_id"] for row in sample_reviews]
            report["normal_api"] = {"seconds": seconds, "status_code": normal.status_code,
                                     "mysql_result_matches": True}

            from uuid import uuid4
            from unittest.mock import patch
            from app.core.analysis_versions import analysis_versions

            stream_headers = {**headers, "X-Request-ID": "policy-smoke",
                              "Idempotency-Key": "policy-smoke-" + str(uuid4())}
            official, seconds = timed(lambda: client.post(
                "/api/v1/data/analyze/stream", json=payload, headers=stream_headers))
            assert official.status_code == 200
            job_ids.append(official.headers["X-Analysis-Job-ID"])
            events = [(frame.event, json.loads(frame.data))
                      for frame in iter_sse_frames(official.text.splitlines())]
            assert {key: events[0][1][key] for key in analysis_versions()} == analysis_versions()
            wire_results = [{key: value for key, value in data.items() if key != "request_id"}
                            for name, data in events if name == "result"]
            assert wire_results == expected["results"]
            assert events[-1][0] == "done" and events[-1][1]["result_count"] == 3
            assert store.get(job_ids[-1])["result"] == expected
            with patch("app.services.data_analysis_stream.evaluate_data_and_store",
                       side_effect=AssertionError("Replay must not infer")) as forbidden:
                replay, replay_seconds = timed(lambda: client.post(
                    "/api/v1/data/analyze/stream", json=payload, headers=stream_headers))
                assert replay.status_code == 200
                replay_events = [(frame.event, json.loads(frame.data))
                                 for frame in iter_sse_frames(replay.text.splitlines())]
                assert replay_events == [event for event in events if event[0] != "progress"]
                forbidden.assert_not_called()
            report["official_sse"] = {"seconds": seconds, "replay_seconds": replay_seconds,
                                      "api_result_identical": True, "mysql_result_matches": True,
                                      "replay_without_analysis": True, **analysis_versions()}

            stream_body = "".join(
                "event: review\ndata: " + json.dumps({**row, "platform": "mall", "product_id": "0007"}, ensure_ascii=False) + "\n\n"
                for row in sample_reviews
            ) + 'event: done\ndata: {"job_id":"synthetic-crawler-job","collected":3}\n\n'
            crawler_calls = []

            def transport(request):
                assert request.url.scheme == "https" and request.url.host == "data.test"
                assert request.url.path == stream_reviews_path("mall", "0007")
                assert request.headers["X-Internal-Token"] == os.environ["DATA_INTERNAL_TOKEN"]
                crawler_calls.append(request.url.path)
                return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=stream_body)

            async_http = httpx.AsyncClient(transport=httpx.MockTransport(transport), base_url="https://data.test")
            app.state.crawler_stream = CrawlerStreamClient(client=async_http, settings=CrawlerSettings(
                base_url="https://data.test", internal_token=os.environ["DATA_INTERNAL_TOKEN"],
            ))
            try:
                stream, seconds = timed(lambda: client.post(
                    "/experimental/analysis/collect/stream",
                    json={"platform": "mall", "product_id": "0007", "limit": 3}, headers=headers,
                ))
                assert stream.status_code == 200
                job_ids.append(stream.headers["X-Analysis-Job-ID"])
                frames = list(iter_sse_frames(stream.text.splitlines()))
                assert frames[-1].event == "result" and json.loads(frames[-1].data) == normal.json()
                assert crawler_calls == [stream_reviews_path("mall", "0007")]
                saved = store.get(job_ids[-1])
                assert saved["status"] == "DONE" and saved["result"] == expected
                lookup_path = "/experimental/analysis/jobs/" + job_ids[-1]
                assert client.get(lookup_path).status_code == 401
                lookup = client.get(lookup_path, headers=headers)
                assert lookup.status_code == 200 and lookup.json()["result"] == expected
                report["sse"] = {"seconds": seconds, "status_code": stream.status_code,
                                  "events": [frame.event for frame in frames],
                                  "api_result_identical": True, "mysql_result_matches": True,
                                  "authenticated_lookup_matches": True}
            finally:
                client.portal.call(async_http.aclose)
            assert client.get("/health").status_code == 200
            cache = p_text._predictor.cache_info()
            assert cache.misses == 1 and cache.currsize == 1
            report["model_cache"] = dict(cache._asdict())
            report["memory"]["final"] = memory_snapshot()
            report["checks"]["single_cached_model_across_service_api_sse"] = True
            report["checks"]["all_passed"] = True
    finally:
        for job_id in job_ids:
            store._execute("DELETE FROM ai_analysis_idempotency WHERE job_id=%s", (job_id,))
            store._execute("DELETE FROM ai_analysis_jobs WHERE job_id=%s", (job_id,))
    report["cleanup"] = {"own_jobs_removed": len(job_ids)}
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)


if __name__ == "__main__":
    main()
