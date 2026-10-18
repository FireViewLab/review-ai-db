"""Generate the P_text single-inference golden file without reimplementing inference."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.analyzers import p_text  # noqa: E402

DEFAULT_CASES = REPO_ROOT / "tests/fixtures/ptext_batch_parity_cases.json"
DEFAULT_OUTPUT = REPO_ROOT / "tests/fixtures/ptext_single_golden.json"
RESULT_KEYS = ("text_score", "suspicious_probability", "predicted_label")


def load_cases(path: Path) -> list[dict[str, Any]]:
    cases = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(cases, list):
        raise ValueError("fixture root must be a JSON array")
    seen: set[str] = set()
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            raise ValueError(f"case {index} must be an object")
        if set(case) != {"case_id", "category", "content", "purpose"}:
            raise ValueError(f"case {index} has an unexpected fixture structure")
        case_id = case["case_id"]
        if not isinstance(case_id, str) or not case_id or case_id in seen:
            raise ValueError(f"case {index} has an invalid or duplicate case_id")
        if not isinstance(case["category"], str) or not isinstance(case["purpose"], str):
            raise ValueError(f"case {case_id} has invalid metadata")
        if case["content"] is not None and not isinstance(case["content"], str):
            raise ValueError(f"case {case_id} content must be a string or null")
        seen.add(case_id)
    return cases


def generate_golden(cases_path: Path, output_path: Path, *, model_path: str | None = None) -> list[dict[str, Any]]:
    """Run the production single-item entry point once per case, in order."""
    results: list[dict[str, Any]] = []
    for case in load_cases(cases_path):
        prediction = p_text.predict_text_score(case["content"], model_path=model_path)
        if set(prediction) != set(RESULT_KEYS):
            raise RuntimeError(f"unexpected inference result for {case['case_id']}")
        is_analyzable = isinstance(case["content"], str) and bool(case["content"].strip())
        if is_analyzable and prediction["text_score"] == -1:
            raise RuntimeError(
                f"inference unavailable for analyzable case {case['case_id']}; golden was not written"
            )
        results.append({"case_id": case["case_id"], **prediction})

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(results, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate P_text golden results using sequential single inference")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--model-path", default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    results = generate_golden(args.cases, args.output, model_path=args.model_path)
    print(f"Wrote {len(results)} single-inference results to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
