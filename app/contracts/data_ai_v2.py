"""Separate v2 SSE wire models; never serialize Policy Index as v0.5 RTI."""

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from app.contracts.data_ai_result import ContractModel, Identifier
from app.contracts.data_ai_v05 import DataAnalyzeRequestV05, DataReviewV05

MODEL_VERSION_V2 = "ptext-koelectra-v3.1-b-seed20261115"
POLICY_VERSION_V2 = "evidence-v3-balanced-interior-v1"
REASON_VERSION_V2 = "policy-locked-connectives-v1"


def v2_versions() -> dict[str, str]:
    return {"contract_version": "v2", "model_version": MODEL_VERSION_V2, "policy_version": POLICY_VERSION_V2}


class DataReviewV2(DataReviewV05):
    rating: Annotated[float, Field(strict=True, ge=0, le=5)] | None = None
    image_count: Annotated[int, Field(strict=True, ge=0)] | None = None


class DataAnalyzeRequestV2(DataAnalyzeRequestV05):
    reviews: list[DataReviewV2] = Field(min_length=1, max_length=500)


class DataReviewResultV2(ContractModel):
    review_id: Identifier
    policy_index_v3: Annotated[float, Field(strict=True, ge=-1, le=100)]
    evidence_level_v3: Literal["SAFE", "WARN", "DANGER"] | None
    text_score: Annotated[float, Field(strict=True, ge=-1, le=100)]
    behavior_score: Annotated[float, Field(strict=True, ge=-1, le=100)]
    network_score: Annotated[float, Field(strict=True, ge=-1, le=100)]
    analysis_summary_ko: str | None
    rules_reasons_ko: list[str]
    evidence_reasons_v3: list[str]
    reason_source: Literal["groq", "python", "rules"]

    @model_validator(mode="after")
    def validate_signals_and_level(self) -> Self:
        scores = (self.text_score, self.behavior_score, self.network_score)
        if any(-1 < score < 0 for score in (*scores, self.policy_index_v3)):
            raise ValueError("Negative score must be the unavailable sentinel -1")
        score = self.policy_index_v3
        expected = None if score == -1 else "SAFE" if score >= 70 else "WARN" if score >= 40 else "DANGER"
        if self.evidence_level_v3 != expected or (score == -1) != all(value == -1 for value in scores):
            raise ValueError("Availability and Policy Index band must agree")
        if (self.reason_source == "rules") != (self.analysis_summary_ko is None):
            raise ValueError("Reason source does not match the paragraph")
        return self


class DataAnalyzeResponseV2(ContractModel):
    contract_version: Literal["v2"]
    platform: Identifier
    product_id: Identifier
    review_count: Annotated[int, Field(strict=True, ge=1, le=500)]
    results: list[DataReviewResultV2]

    @model_validator(mode="after")
    def validate_results(self) -> Self:
        if len(self.results) != self.review_count or len({row.review_id for row in self.results}) != self.review_count:
            raise ValueError("Invalid result count or duplicate result ID")
        return self
