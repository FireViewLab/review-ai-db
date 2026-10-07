"""Data 요청을 받아 DB idempotency와 리뷰별 SSE 결과 전달을 제공한다."""

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from app.contracts.data_ai_v05 import DataAnalyzeRequestV05
from app.core.analysis_versions import analysis_versions
from app.core.internal_auth import require_internal_token
from app.repositories.idempotency import IdempotencyConflict
from app.services.data_analysis_stream import analysis_events, saved_result, start_analysis

router = APIRouter(tags=["Data AI v0.5"], dependencies=[Depends(require_internal_token)])


@router.post("/api/v1/data/analyze/stream", response_class=StreamingResponse,
             responses={200: {"description": "SSE: meta, progress, heartbeat, result (per review), done or error. Results follow DB commit.",
                              "content": {"text/event-stream": {"schema": {"type": "string"}}}},
                        409: {"description": "Idempotency key reused, in progress or failed"},
                        503: {"description": "Storage unavailable"}})
async def analyze_data_stream(payload: DataAnalyzeRequestV05, request: Request,
                              x_request_id: str = Header(...),
                              idempotency_key: str = Header(...)):
    for name, value in (("X-Request-ID", x_request_id), ("Idempotency-Key", idempotency_key)):
        if not value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise HTTPException(422, f"{name} must be a non-empty valid HTTP header")
    store = request.app.state.job_store
    try:
        claim = await run_in_threadpool(store.claim_idempotent, idempotency_key,
                                       payload.model_dump(mode="json"), analysis_versions())
        replay = None if claim.is_new else await run_in_threadpool(saved_result, store, claim.job_id, payload)
    except IdempotencyConflict as exc:
        raise HTTPException(409, detail={"code": exc.code}) from None
    except Exception:
        raise HTTPException(503, "Analysis storage unavailable") from None
    task = start_analysis(request.app.state.analysis_tasks, store, claim.job_id, payload) if claim.is_new else None
    return StreamingResponse(
        analysis_events(request_id=x_request_id, claim=claim, payload=payload, task=task, replay=replay),
        media_type="text/event-stream", headers={"X-Request-ID": x_request_id,
            "X-Analysis-Job-ID": claim.job_id, "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
