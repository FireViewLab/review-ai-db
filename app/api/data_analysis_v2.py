"""Opt-in v2 POST SSE. Shared framing, separate DTO, model and local replay cache."""

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.contracts.data_ai_v2 import DataAnalyzeRequestV2
from app.core.internal_auth import require_internal_token
from app.repositories.idempotency import IdempotencyConflict
from app.services.data_analysis_stream import analysis_events
from app.services.data_analysis_v2 import start_v2, v2_error_code

router = APIRouter(tags=["Data AI v2"], dependencies=[Depends(require_internal_token)])


@router.post("/api/v2/data/analyze/stream", response_class=StreamingResponse,
             responses={200: {"description": "v2 SSE: meta, progress, heartbeat, result, done or error. Results follow the local replay-cache commit.",
                              "content": {"text/event-stream": {"schema": {"type": "string"}}}},
                        409: {"description": "Idempotency conflict within v2"}, 503: {"description": "v2 storage unavailable"}})
async def analyze_v2_stream(payload: DataAnalyzeRequestV2, request: Request,
                            x_request_id: str = Header(...), idempotency_key: str = Header(...)):
    for name, value in (("X-Request-ID", x_request_id), ("Idempotency-Key", idempotency_key)):
        if not value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise HTTPException(422, f"{name} must be a non-empty valid HTTP header")
    state = request.app.state
    try:
        claim, replay, task = await start_v2(state.analysis_tasks, state.v2_job_store, idempotency_key,
                                             payload, groq=state.v2_groq)
    except IdempotencyConflict as exc:
        raise HTTPException(409, detail={"code": exc.code}) from None
    except Exception:
        raise HTTPException(503, "V2 analysis storage unavailable") from None
    return StreamingResponse(
        analysis_events(request_id=x_request_id, claim=claim, payload=payload, task=task, replay=replay,
                        error_code=v2_error_code),
        media_type="text/event-stream", headers={"X-Request-ID": x_request_id, "X-Analysis-Job-ID": claim.job_id,
                                                  "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# JSON API: reuse the same file-backed job, inference and replay path as SSE.
import asyncio
from fastapi import Response
from app.contracts.data_ai_v2 import DataAnalyzeResponseV2


@router.post(
    "/api/v2/data/analyze",
    response_model=DataAnalyzeResponseV2,
    responses={
        409: {"description": "Idempotency conflict"},
        503: {"description": "Analysis or storage unavailable"},
    },
)
async def analyze_v2_json(
    payload: DataAnalyzeRequestV2,
    request: Request,
    response: Response,
    x_request_id: str = Header(...),
    idempotency_key: str = Header(...),
):
    for name, value in (
        ("X-Request-ID", x_request_id),
        ("Idempotency-Key", idempotency_key),
    ):
        if not value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise HTTPException(422, f"{name} must be a non-empty valid HTTP header")

    state = request.app.state

    try:
        claim, replay, task = await start_v2(
            state.analysis_tasks,
            state.v2_job_store,
            idempotency_key,
            payload,
            groq=state.v2_groq,
        )
    except IdempotencyConflict as exc:
        raise HTTPException(409, detail={"code": exc.code}) from None
    except Exception:
        raise HTTPException(503, "V2 analysis storage unavailable") from None

    try:
        # A disconnected HTTP client must not cancel the persisted job.
        rows = await asyncio.shield(task) if task is not None else replay

        result = DataAnalyzeResponseV2(
            contract_version="v2",
            platform=payload.platform,
            product_id=payload.product_id,
            review_count=len(rows),
            results=rows,
        )
    except Exception:
        raise HTTPException(503, "V2 analysis or result storage failed") from None

    response.headers["X-Request-ID"] = x_request_id
    response.headers["X-Analysis-Job-ID"] = claim.job_id
    return result
