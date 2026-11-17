"""Opt-in v2 POST SSE. Shared framing but separate DTO, model, and job table scope."""

from functools import partial

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from app.contracts.data_ai_v2 import DataAnalyzeRequestV2, DataAnalyzeResponseV2, v2_versions
from app.core.internal_auth import require_internal_token
from app.repositories.idempotency import IdempotencyConflict
from app.services.data_analysis_stream import analysis_events, saved_result, start_analysis
from app.services.data_analysis_v2 import evaluate_and_read_v2, v2_error_code

router = APIRouter(tags=["Data AI v2"], dependencies=[Depends(require_internal_token)])


@router.post("/api/v2/data/analyze/stream", response_class=StreamingResponse,
             responses={200: {"description": "v2 SSE: meta, progress, heartbeat, result, done or error. Results follow v2 DB commit.",
                              "content": {"text/event-stream": {"schema": {"type": "string"}}}},
                        409: {"description": "Idempotency conflict within v2"}, 503: {"description": "v2 storage unavailable"}})
async def analyze_v2_stream(payload: DataAnalyzeRequestV2, request: Request,
                            x_request_id: str = Header(...), idempotency_key: str = Header(...)):
    for name, value in (("X-Request-ID", x_request_id), ("Idempotency-Key", idempotency_key)):
        if not value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise HTTPException(422, f"{name} must be a non-empty valid HTTP header")
    store = request.app.state.v2_job_store
    try:
        claim = await run_in_threadpool(store.claim_idempotent, idempotency_key,
                                       payload.model_dump(mode="json"), v2_versions())
        replay = None if claim.is_new else await run_in_threadpool(
            saved_result, store, claim.job_id, payload, result_type=DataAnalyzeResponseV2)
    except IdempotencyConflict as exc:
        raise HTTPException(409, detail={"code": exc.code}) from None
    except Exception:
        raise HTTPException(503, "V2 analysis storage unavailable") from None
    runner = partial(evaluate_and_read_v2, groq=request.app.state.v2_groq)
    task = start_analysis(request.app.state.analysis_tasks, store, claim.job_id, payload, runner=runner) if claim.is_new else None
    return StreamingResponse(
        analysis_events(request_id=x_request_id, claim=claim, payload=payload, task=task, replay=replay,
                        error_code=v2_error_code),
        media_type="text/event-stream", headers={"X-Request-ID": x_request_id, "X-Analysis-Job-ID": claim.job_id,
                                                  "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
