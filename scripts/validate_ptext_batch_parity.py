"""Read-only CPU parity check against the immutable single-inference fixtures."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import sys
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.analyzers import p_text  # noqa: E402
from scripts.generate_ptext_batch_parity_golden import (  # noqa: E402
    DEFAULT_CASES, DEFAULT_OUTPUT, RESULT_KEYS, load_cases,
)


def compare_predictions(reference, actual, case_ids):
    """Require exact discrete outputs and measure probability deltas, without tolerance."""
    mismatches = []
    rows = []
    if len(reference) != len(case_ids) or len(actual) != len(case_ids):
        return {"passed": False, "mismatches": [{"field": "count", "expected": len(case_ids),
                 "reference": len(reference), "actual": len(actual)}],
                "max_probability_absolute_delta": None, "rows": []}
    for case_id, expected, observed in zip(case_ids, reference, actual, strict=True):
        if set(observed) != set(RESULT_KEYS):
            mismatches.append({"case_id": case_id, "field": "result_keys"})
            continue
        for field in ("text_score", "predicted_label"):
            if observed[field] != expected[field]:
                mismatches.append({"case_id": case_id, "field": field,
                                   "expected": expected[field], "actual": observed[field]})
        left, right = expected["suspicious_probability"], observed["suspicious_probability"]
        delta = None
        if (left is None) != (right is None):
            mismatches.append({"case_id": case_id, "field": "probability_availability"})
        elif left is not None:
            if (isinstance(right, bool) or not isinstance(right, (int, float))
                    or not math.isfinite(right) or not 0 <= right <= 1):
                mismatches.append({"case_id": case_id, "field": "invalid_probability"})
            else:
                delta = abs(left - right)
        if observed["text_score"] == -1 and observed != p_text.unavailable_result():
            mismatches.append({"case_id": case_id, "field": "unavailable_contract"})
        rows.append({"case_id": case_id, "reference": expected, "actual": observed,
                     "probability_absolute_delta": delta})
    deltas = [row["probability_absolute_delta"] for row in rows
              if row["probability_absolute_delta"] is not None]
    return {"passed": not mismatches, "mismatches": mismatches,
            "max_probability_absolute_delta": max(deltas, default=0.0), "rows": rows}


def duplicate_parity(cases, predictions):
    """Compare repeated content independently of its position and padding companions."""
    seen = {}
    comparisons = []
    for case, prediction in zip(cases, predictions, strict=True):
        content = case["content"]
        if not isinstance(content, str) or not content.strip():
            continue
        if content in seen:
            previous_id, previous = seen[content]
            comparisons.append({"first_case_id": previous_id, "case_id": case["case_id"],
                                **compare_predictions([previous], [prediction], [case["case_id"]])})
        else:
            seen[content] = (case["case_id"], prediction)
    return {"passed": bool(comparisons) and all(item["passed"] for item in comparisons),
            "comparisons": comparisons}


def tokenization_parity(predictor, cases):
    """Prove truncation and unmasked tokens match, including the long fixture."""
    valid = [case for case in cases if isinstance(case["content"], str) and case["content"].strip()]
    maximum = predictor.settings["max_length"]
    batch = predictor.tokenizer([case["content"] for case in valid], return_tensors="pt",
                                padding=True, truncation=True, max_length=maximum)
    rows = []
    for index, case in enumerate(valid):
        single = predictor.tokenizer(case["content"], return_tensors="pt",
                                     truncation=True, max_length=maximum)
        original = predictor.tokenizer(case["content"], truncation=False)["input_ids"]
        single_mask = single["attention_mask"][0].bool()
        batch_mask = batch["attention_mask"][index].bool()
        single_ids = single["input_ids"][0][single_mask].tolist()
        batch_ids = batch["input_ids"][index][batch_mask].tolist()
        is_long = case["category"] == "long_truncation"
        passed = (single_ids == batch_ids and len(single_ids) <= maximum
                  and (not is_long or len(original) > maximum == len(single_ids)))
        rows.append({"case_id": case["case_id"], "original_token_count": len(original),
                     "effective_token_count": len(single_ids), "passed": passed,
                     "was_truncated": len(original) > len(single_ids)})
    return {"passed": all(row["passed"] for row in rows), "max_length": maximum,
            "dynamic_padding": True, "attention_mask_checked": True, "rows": rows}


def service_parity(cases, *, model_path, batch_sizes):
    """Compare complete flat v0.5 rows against the previous sequential Text path."""
    from app.services import analysis

    payload = {"platform": "parity-fixture", "product_id": "ptext-parity",
               "reviews": [{"review_id": case["case_id"], "content": case["content"]}
                           for case in cases]}

    def single_text(contents, **kwargs):
        return [p_text.predict_text_score(content, model_path=model_path) for content in contents]

    with patch.object(analysis, "predict_text_scores", side_effect=single_text):
        reference = analysis.analyze_reviews(**payload)
    rows = []
    for size in batch_sizes:
        def batch_text(contents, *, _size=size, **kwargs):
            return p_text.predict_text_scores(contents, batch_size=_size, model_path=model_path)

        with patch.object(analysis, "predict_text_scores", side_effect=batch_text):
            actual = analysis.analyze_reviews(**payload)
        changed = [old["review_id"] for old, new in zip(reference["results"], actual["results"], strict=True)
                   if old != new]
        expected_order = [case["case_id"] for case in cases]
        rows.append({"batch_size": size, "passed": actual == reference,
                     "changed_review_ids": changed,
                     "input_order_preserved": [row["review_id"] for row in actual["results"]] == expected_order,
                     "result": actual})
    return {"passed": all(row["passed"] and row["input_order_preserved"] for row in rows),
            "sequential_reference": reference, "comparisons": rows}


def run_parity(*, model_path, cases_path=DEFAULT_CASES, golden_path=DEFAULT_OUTPUT,
               batch_sizes=(1, 8, 16, 32), repeats=2, cpu_threads=1):
    import torch
    import transformers

    if repeats < 2:
        raise ValueError("At least two repetitions are required")
    torch.set_num_threads(cpu_threads)
    cases = load_cases(Path(cases_path))
    case_ids = [case["case_id"] for case in cases]
    golden = json.loads(Path(golden_path).read_text(encoding="utf-8"))
    if [row["case_id"] for row in golden] != case_ids:
        raise ValueError("Golden case IDs must match the immutable fixture order")
    expected = [{key: row[key] for key in RESULT_KEYS} for row in golden]
    asset_hashes = {str(path): hashlib.sha256(Path(path).read_bytes()).hexdigest()
                    for path in (cases_path, golden_path)}
    started = perf_counter()
    predictor = p_text.TextScorePredictor(model_path, device="cpu")
    load_seconds = perf_counter() - started
    report = {
        "environment": {"python": sys.version, "platform": platform.platform(),
                        "torch": torch.__version__, "transformers": transformers.__version__,
                        "device": predictor.device, "torch_cpu_threads": torch.get_num_threads(),
                        "torch_interop_threads": torch.get_num_interop_threads(),
                        "model_path": str(model_path), "inference_settings": predictor.settings},
        "model_load_seconds": load_seconds, "case_count": len(cases), "repeats": repeats,
        "fixture_sha256": asset_hashes,
        "probability_tolerance_applied": None,
        "exact_fields": ["text_score", "predicted_label", "unavailable", "order", "rti", "level"],
        "golden_file_modified": False, "single_runs": [], "batch_runs": [],
    }
    contents = [case["content"] for case in cases]
    with patch.object(p_text, "_predictor", return_value=predictor):
        reference = None
        for repeat in range(repeats):
            started = perf_counter()
            results = [p_text.predict_text_score(content, model_path=model_path) for content in contents]
            elapsed = perf_counter() - started
            if reference is None:
                reference = results
            report["single_runs"].append({
                "repeat": repeat + 1, "seconds": elapsed,
                "against_golden": compare_predictions(expected, results, case_ids),
                "against_first_single": compare_predictions(reference, results, case_ids),
                "duplicates": duplicate_parity(cases, results),
            })
        for size in batch_sizes:
            for repeat in range(repeats):
                started = perf_counter()
                results = p_text.predict_text_scores(contents, batch_size=size, model_path=model_path)
                elapsed = perf_counter() - started
                report["batch_runs"].append({
                    "batch_size": size, "repeat": repeat + 1, "seconds": elapsed,
                    "against_single": compare_predictions(reference, results, case_ids),
                    "against_golden": compare_predictions(expected, results, case_ids),
                    "duplicates": duplicate_parity(cases, results),
                })
        report["tokenization"] = tokenization_parity(predictor, cases)
        report["service"] = service_parity(cases, model_path=model_path, batch_sizes=batch_sizes)
    checks = [report["tokenization"]["passed"], report["service"]["passed"]]
    for run in report["single_runs"]:
        checks.extend(run[name]["passed"] for name in ("against_golden", "against_first_single", "duplicates"))
    for run in report["batch_runs"]:
        checks.extend(run[name]["passed"] for name in ("against_single", "against_golden", "duplicates"))
    report["passed"] = all(checks)
    report["max_single_batch_probability_absolute_delta"] = max(
        (run["against_single"]["max_probability_absolute_delta"] or 0.0
         for run in report["batch_runs"]), default=0.0)
    report["probability_tolerance_proposal"] = {
        "absolute_tolerance": report["max_single_batch_probability_absolute_delta"] if report["passed"] else None,
        "scope": "Observed maximum for these repeated CPU runs only; remeasure for another environment.",
        "does_not_apply_to": ["text_score", "predicted_label", "unavailable", "rti", "level"],
    }
    report["probability_tolerance_note"] = (
        "No tolerance was used to excuse score or label differences. The measured maximum is "
        "an observation for this CPU/software environment, not a cross-environment guarantee."
    )
    if any(hashlib.sha256(Path(path).read_bytes()).hexdigest() != checksum
           for path, checksum in asset_hashes.items()):
        raise RuntimeError("Parity fixture or golden changed during validation")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-path", default=os.getenv("PTEXT_MODEL_PATH") or p_text.DEFAULT_MODEL_PATH)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--golden", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--batch-sizes", type=int, nargs="+", default=[1, 8, 16, 32])
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--cpu-threads", type=int, default=1)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.output and (args.output.resolve() in {args.cases.resolve(), args.golden.resolve()}
                        or args.output.resolve().is_relative_to(DEFAULT_CASES.parent.resolve())):
        parser.error("The report must not overwrite fixtures or the golden file")
    report = run_parity(model_path=args.model_path, cases_path=args.cases, golden_path=args.golden,
                        batch_sizes=args.batch_sizes, repeats=args.repeats, cpu_threads=args.cpu_threads)
    rendered = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
        print(json.dumps({"passed": report["passed"], "output": str(args.output),
                          "max_probability_absolute_delta": report["max_single_batch_probability_absolute_delta"]}))
    else:
        print(rendered)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
