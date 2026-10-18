"""기존 근거와 확정 점수를 보존하고 빈 reasons에만 중립적인 점수 안내를 만든다."""

from dataclasses import dataclass
from typing import Literal

from app.contracts.data_ai_v05 import DataReviewResultV05


# 설명 문구 정책은 모델/RTI 정책과 독립적이다. 외부 v0.5 필드를 추가하지 않는다.
EXPLANATION_POLICY_VERSION = "neutral-summary-v1"

SAFE_MESSAGE = (
    "현재 계산 가능한 신호를 기준으로 신뢰 점수가 높은 구간에 해당합니다. "
    "실제 구매 여부나 후기의 진위를 보장하는 것은 아닙니다."
)
WARN_MESSAGE = (
    "현재 분석 점수는 주의 구간에 해당합니다. "
    "구체적인 위험 근거가 별도로 제공되지 않아 추가적인 확인이 필요합니다."
)
DANGER_MESSAGE = (
    "현재 분석 점수는 낮은 구간에 해당합니다. "
    "다만 점수만으로 허위 후기나 광고성 리뷰라고 단정할 수는 없습니다."
)
UNAVAILABLE_MESSAGE = "현재 제공된 정보와 계산 가능한 신호만으로는 신뢰 점수를 산정하기 어렵습니다."
PARTIAL_SIGNALS_MESSAGE = (
    "계산 가능한 항목만으로 신뢰 점수를 산정했으며, "
    "계산할 수 없는 항목은 0점으로 반영하지 않았습니다."
)


@dataclass(frozen=True, slots=True)
class ExplanationSignal:
    """확정 점수와 가용성만 전달하며 새 위험 근거나 감점을 만들지 않는다."""

    name: Literal["text", "behavior", "network"]
    score: float
    available: bool


@dataclass(frozen=True, slots=True)
class ReviewExplanationContext:
    """향후 AI 담당자의 설명 규격으로 교체할 수 있는 내부 전용 입력이다.

    원본 근거를 불변 tuple로 복사한다. 외부 응답/DB schema에는 노출하지 않고,
    현재 Groq 요청에도 전달하지 않는다. 점수 기여도나 관측 사실은 추측하지 않는다.
    """

    raw_reason_codes: tuple[str, ...]
    rti: float
    level: Literal["safe", "warn", "danger"] | None
    signals: tuple[ExplanationSignal, ...]
    explanation_policy_version: str = EXPLANATION_POLICY_VERSION


def build_explanation_context(result: DataReviewResultV05) -> ReviewExplanationContext:
    """검증된 결과를 읽기만 하며 -1과 실제 0점의 의미를 분리한다."""
    return ReviewExplanationContext(
        raw_reason_codes=tuple(result.reasons),
        rti=result.rti,
        level=result.level,
        signals=tuple(
            ExplanationSignal(name, score, available=score != -1)
            for name, score in (
                ("text", result.text_score),
                ("behavior", result.behavior_score),
                ("network", result.network_score),
            )
        ),
    )


def neutral_reasons(context: ReviewExplanationContext) -> list[str]:
    """빈 근거에만 점수 구간/가용성 안내를 제공하는 결정적 설명 인터페이스다."""
    if context.raw_reason_codes:
        return list(context.raw_reason_codes)
    if not any(signal.available for signal in context.signals):
        return [UNAVAILABLE_MESSAGE]
    messages = {"safe": SAFE_MESSAGE, "warn": WARN_MESSAGE, "danger": DANGER_MESSAGE}
    result = [messages[context.level]]
    if any(not signal.available for signal in context.signals):
        result.append(PARTIAL_SIGNALS_MESSAGE)
    return result
