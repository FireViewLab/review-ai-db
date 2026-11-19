"""Write a Python-only paragraph comparison for the original synthetic fixture; no credentials or HTTP."""

import argparse
import json
from pathlib import Path
from statistics import median
import sys
from timeit import repeat

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.experimental.policy_summary import SUMMARY_VERSION, compose_policy_summary, summary_preview
from app.experimental.summary_candidate import approved_summary_options, select_approved_summary
from scripts.compare_ai_v3_groq_reasons import LIVE_REVIEW_IDS, LOCAL_OUTPUT, load_cases


def compare_paragraphs(cases) -> list[dict]:
    return [
        {"review_id": case.review_id, "policy_index_v3": case.policy_index_v3, "evidence_level_v3": case.evidence_level_v3,
         "evidence_reasons_v3": list(case.codes), **summary_preview(case.codes, case.templates),
         "fixed_selector_options_ko": list(approved_summary_options(case.codes))}
        for case in cases
    ]


def local_benchmark(cases, *, batches: int = 2000, repeats: int = 5) -> dict:
    by_id = {case.review_id: case for case in cases}
    profiles = tuple(by_id[review_id].codes for review_id in LIVE_REVIEW_IDS)

    def direct() -> tuple:
        return tuple(compose_policy_summary(codes) for codes in profiles)

    def selector_local_only() -> tuple:
        return tuple(select_approved_summary(codes, approved_summary_options(codes)[0]) for codes in profiles)

    def us_per_batch(function) -> float:
        return round(median(repeat(function, number=batches, repeat=repeats)) / batches * 1_000_000, 3)

    return {"scope": "4-review Python functions only; no HTTP, serialization, or inference",
            "repeats": repeats, "batches_per_repeat": batches, "direct_us_per_batch": us_per_batch(direct),
            "selector_local_us_per_batch": us_per_batch(selector_local_only)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--comparison", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.parent != LOCAL_OUTPUT or output.suffix != ".json" or output.name.endswith("_sample.json") or output.exists():
        parser.error("Use a new ignored JSON file directly in output/")
    cases = load_cases(args.fixture, args.comparison)
    rows = compare_paragraphs(cases)
    result = {"summary_version": SUMMARY_VERSION, "actual_api_requests": 0, "external_tokens": 0,
              "rows": rows, "local_benchmark": local_benchmark(cases)}
    with output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    supported = sum(row["analysis_summary_ko"] is not None for row in rows)
    print(f"Python previews: {supported}/{len(rows)}; {len(rows) - supported} rule fallbacks; 0 API requests.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
