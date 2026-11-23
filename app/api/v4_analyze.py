"""P_text v4.0 분석 API. Data 서버가 v3.1(v2 SSE) 결과를 저장한 뒤 같은 리뷰로 호출한다."""

from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.core.internal_auth import require_internal_token

MAX_REVIEWS = 500


class ReviewIn(BaseModel):
    review_id: str = Field(min_length=1)
    content: str = Field(min_length=1)


class AnalyzeV4Request(BaseModel):
    platform: str = Field(min_length=1)
    product_id: str = Field(min_length=1)
    reviews: list[ReviewIn] = Field(max_length=MAX_REVIEWS)


class ReviewOut(BaseModel):
    review_id: str
    text_probability: float
    text_score: int
    policy_index: float
    level: str


class AnalyzeV4Response(BaseModel):
    platform: str
    product_id: str
    model_version: str
    policy_version: str
    results: list[ReviewOut]


router = APIRouter(tags=["Data AI v4"], dependencies=[Depends(require_internal_token)])


def _predictor(request: Request):
    predictor = getattr(request.app.state, "v4_predictor", None)
    if predictor is None:
        raise HTTPException(503, "v4 model is not configured")
    return predictor


@router.get("/v4/health")
def health_v4(request: Request) -> dict:
    predictor = _predictor(request)
    return {"status": "ok", "model_version": predictor.model_version, "policy_version": predictor.policy.version}


@router.post("/v4/analyze", response_model=AnalyzeV4Response)
def analyze_v4(body: AnalyzeV4Request, request: Request) -> AnalyzeV4Response:
    predictor = _predictor(request)
    ids = [r.review_id for r in body.reviews]
    if len(ids) != len(set(ids)):
        raise HTTPException(422, "duplicate review_id")
    results = predictor.analyze([r.model_dump() for r in body.reviews])
    return AnalyzeV4Response(
        platform=body.platform,
        product_id=body.product_id,
        model_version=predictor.model_version,
        policy_version=predictor.policy.version,
        results=[ReviewOut(**asdict(r)) for r in results],
    )
