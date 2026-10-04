"""공통 analyze_reviews 진입점으로 분석하고 v0.5 결과를 저장한 뒤 반환한다."""
import logging

from app.contracts.data_ai_v05 import DataAnalyzeRequestV05, DataAnalyzeResponseV05
from app.integrations.groq_reason_naturalizer import naturalize_reasons_batch
from app.repositories.analysis_jobs import JobStore
from app.services.analysis import analyze_reviews

LOGGER = logging.getLogger(__name__)


def evaluate_data_and_store(store: JobStore, job_id: str,
                            payload: DataAnalyzeRequestV05) -> DataAnalyzeResponseV05:
    try:
        # 운영 입력 계약은 그대로 유지한다. rating/written_at만으로 행동 근거를 만들지 않는다.
        # 실제로 제공되는 행동 evidence 확장은 Data 팀과 입력 계약을 합의한 뒤 연결한다.
        evaluated = analyze_reviews(
            platform=payload.platform, product_id=payload.product_id,
            reviews=[{"review_id": review.review_id, "content": review.content}
                     for review in payload.reviews],
        )
        response = DataAnalyzeResponseV05.model_validate(evaluated)
        # 선택 기능의 실패는 점수·응답·저장 성공에 영향을 주지 않는다.
        # 새 문구도 동일한 계약으로 검증하고 reasons 외의 필드는 복사만 한다.
        try:
            content_by_id = {review.review_id: review.content for review in payload.reviews}
            if set(content_by_id) != {result.review_id for result in response.results}:
                raise ValueError("Reason input identity mismatch")
            phrases = naturalize_reasons_batch(
                contents=[content_by_id[result.review_id] for result in response.results],
                reasons=[result.reasons for result in response.results],
            )
            if len(phrases) != len(response.results):
                raise ValueError("Reason result count mismatch")
            enriched = response.model_dump(mode="json")
            for result, replacement in zip(enriched["results"], phrases, strict=True):
                result["reasons"] = replacement
            response = DataAnalyzeResponseV05.model_validate(enriched)
        except Exception:
            pass
        store.complete(job_id, response.model_dump(mode="json"))
        return response
    except Exception:
        try:
            store.fail(job_id, "ANALYSIS_OR_STORAGE_FAILED")
        except Exception:
            LOGGER.exception("Failed to record Data analysis failure for %s", job_id)
        raise
