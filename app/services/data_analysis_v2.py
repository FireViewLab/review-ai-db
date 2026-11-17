"""V2 orchestration: v3.1-only inference, deterministic policy, optional reasons, then commit."""

import logging
from threading import RLock

from app.analyzers.p_text import configured_batch_size, load_candidate_predictor
from app.contracts.data_ai_v2 import DataAnalyzeResponseV2, REASON_VERSION_V2
from app.experimental.candidate_analysis import CandidateBatch, CandidateReview, _REASON_MESSAGES, analyze_policy_batch
from app.experimental.policy_summary import compose_policy_summary
from app.services.data_analysis_stream import saved_result

LOGGER = logging.getLogger(__name__)
_V2_INFERENCE_LOCK = RLock()


class ModelUnavailable(RuntimeError):
    pass


class InferenceFailed(RuntimeError):
    pass


def predict_v31b(contents):
    try:
        predictor = load_candidate_predictor()
        batch_size = configured_batch_size()
    except Exception as exc:
        LOGGER.warning("V2 model initialization unavailable (%s)", type(exc).__name__)
        raise ModelUnavailable("V2 model unavailable") from None
    try:
        with _V2_INFERENCE_LOCK:
            return predictor.predict_text_scores(contents, batch_size=batch_size)
    except Exception as exc:
        LOGGER.warning("V2 tensor inference failed (%s)", type(exc).__name__)
        raise InferenceFailed("V2 inference failed") from None


def v2_error_code(exc):
    if isinstance(exc, ModelUnavailable):
        return "V2_MODEL_UNAVAILABLE"
    if isinstance(exc, InferenceFailed):
        return "V2_INFERENCE_FAILED"
    return "ANALYSIS_FAILED"


def evaluate_v2(payload, *, predict=None, groq=None) -> DataAnalyzeResponseV2:
    # Contract: Data sends non-null metadata only when verified at its source.
    verified_source = "data-v2-verified"
    batch = CandidateBatch(reviews=tuple(CandidateReview(
        platform=payload.platform, product_id=payload.product_id, review_id=review.review_id, content=review.content,
        rating=review.rating, rating_scale_max=5 if review.rating is not None else None,
        rating_source=verified_source if review.rating is not None else None, rating_source_verified=review.rating is not None,
        image_count=review.image_count, image_count_source=verified_source if review.image_count is not None else None,
        image_count_source_verified=review.image_count is not None,
        # No stable user ID or posting history in v2; timestamps alone do not create behavior evidence.
    ) for review in payload.reviews))
    evaluated = analyze_policy_batch(batch, text_predictor=predict or predict_v31b)
    results = []
    for row in evaluated["results"]:
        policy = row["policy_v3"]
        scores = row["original_v05"]
        codes = tuple(policy["evidence_reasons_v3"]) if policy is not None else ()
        paragraph = compose_policy_summary(codes)
        reasons = [_REASON_MESSAGES[code] for code in codes] if policy is not None else ["계산할 수 있는 분석 신호가 없습니다."]
        results.append(dict(
            review_id=row["review_id"], policy_index_v3=policy["policy_index_v3"] if policy is not None else -1,
            evidence_level_v3=policy["evidence_level_v3"] if policy is not None else None,
            text_score=scores["text_score"], behavior_score=scores["behavior_score"], network_score=scores["network_score"],
            evidence_reasons_v3=list(codes), rules_reasons_ko=reasons, analysis_summary_ko=paragraph,
            reason_source="python" if paragraph else "rules",
        ))
    if groq is not None:
        from app.integrations.groq_v2_summary import MAX_BATCH, verified_paragraph

        eligible = [result for result in results if result["analysis_summary_ko"] is not None][:MAX_BATCH]
        try:
            outcome = groq.enrich([row["analysis_summary_ko"] for row in eligible])
            for index, row in enumerate(eligible):
                candidate = verified_paragraph(row["analysis_summary_ko"], outcome.paragraphs.get(index))
                if candidate is not None and candidate != row["analysis_summary_ko"]:
                    row.update(analysis_summary_ko=candidate, reason_source="groq")
        except Exception:
            # Optional enrichment is not part of scoring, job success, or persistence guarantees.
            pass
    return DataAnalyzeResponseV2(contract_version="v2", platform=payload.platform, product_id=payload.product_id,
                                 review_count=len(results), results=results)


def evaluate_and_read_v2(store, job_id, payload, *, groq):
    try:
        response = evaluate_v2(payload, groq=groq)
        store.complete(job_id, response.model_dump(mode="json"), explanation_policy_version=REASON_VERSION_V2)
        return saved_result(store, job_id, payload, result_type=DataAnalyzeResponseV2)
    except Exception as exc:
        try:
            store.fail(job_id, v2_error_code(exc))
        except Exception:
            pass
        raise
