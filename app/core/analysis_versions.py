"""저장 및 SSE replay에 사용할 분석 계약/모델/정책 버전을 한 곳에서 관리한다."""

CONTRACT_VERSION = "v0.5"
MODEL_VERSION = "ptext-koelectra-v1-2epoch-20260929"
POLICY_VERSION = "rti-v0.1"


def analysis_versions() -> dict[str, str]:
    return dict(contract_version=CONTRACT_VERSION, model_version=MODEL_VERSION,
                policy_version=POLICY_VERSION)
