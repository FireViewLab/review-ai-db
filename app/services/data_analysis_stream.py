"""분석을 연결 수명과 분리하고 저장 완료된 v0.5 결과만 SSE로 전달한다."""

import asyncio
import json

from starlette.concurrency import run_in_threadpool

from app.contracts.data_ai_v05 import DataAnalyzeResponseV05
from app.services.data_analysis import evaluate_data_and_store

HEARTBEAT_SECONDS = 15.0


def saved_result(store, job_id, payload):
    job = store.get(job_id)
    if job is None or job["status"] != "DONE":
        raise ValueError("Stored analysis unavailable")
    result = DataAnalyzeResponseV05.model_validate(job["result"])
    ids = [review.review_id for review in payload.reviews]
    by_id = {review.review_id: review for review in result.results}
    if (result.platform != payload.platform or result.product_id != payload.product_id
            or set(ids) != set(by_id) or len(ids) != result.review_count):
        raise ValueError("Stored analysis does not match request")
    return [by_id[review_id].model_dump(mode="json") for review_id in ids]


def analyze_and_read(store, job_id, payload):
    evaluate_data_and_store(store, job_id, payload)
    return saved_result(store, job_id, payload)


def start_analysis(tasks, store, job_id, payload):
    task = asyncio.create_task(run_in_threadpool(analyze_and_read, store, job_id, payload))
    tasks.add(task)

    def finished(completed):
        tasks.discard(completed)
        if not completed.cancelled():
            completed.exception()  # Consume failures even after client disconnect.

    task.add_done_callback(finished)
    return task


def frame(name, data):
    return (f"event: {name}\ndata: " + json.dumps(data, ensure_ascii=False,
             allow_nan=False, separators=(",", ":")) + "\n\n").encode("utf-8")


async def analysis_events(*, request_id, claim, payload, task=None, replay=None):
    context = dict(request_id=request_id, ai_job_id=claim.job_id)
    yield frame("meta", {**context, "platform": payload.platform,
                         "product_id": payload.product_id,
                         "review_count": len(payload.reviews), **claim.versions})
    try:
        if task is not None:
            yield frame("progress", dict(request_id=request_id, stage="analyzing",
                                         processed=0, total=len(payload.reviews)))
            while not task.done():
                ready, _ = await asyncio.wait({task}, timeout=HEARTBEAT_SECONDS)
                if not ready:
                    yield frame("heartbeat", dict(request_id=request_id))
            results = task.result()
        else:
            results = replay
        count = 0
        for result in results:
            yield frame("result", {**result, "request_id": request_id})
            count += 1
        yield frame("done", {**context, "result_count": count})
    except Exception:
        yield frame("error", {**context, "code": "ANALYSIS_FAILED",
                              "message": "분석 결과를 제공하지 못했습니다. 작업 상태 확인이 필요합니다.",
                              "retryable": False})
    # No cancellation of task: persistence must continue after stream disconnect.
