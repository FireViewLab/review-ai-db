"""Compare the handoff synthetic fixture offline; no web, MySQL, Groq, or deployment."""

import argparse
from collections import Counter
import ctypes
from hashlib import file_digest
import json
import os
from pathlib import Path
import random
from statistics import fmean
import sys
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.analyzers.p_text import TextScorePredictor, load_candidate_predictor
from app.core.analysis_versions import MODEL_VERSION
from app.experimental.candidate_analysis import (
    CandidateBatch, analyze_candidate_batch, require_candidate_enabled,
)

V31B_MODEL_VERSION = "ptext-v3.1-b-experimental-seed20261115"
FIXTURE_SHA256 = "c9a708684e1603777c8ce6f5d7b343c0c8a5518b6c6c5c60d9841c9071ad42a4"
PROBABILITY_TOLERANCE = 1e-5  # Proposed CPU tolerance in the supplied fixture.
BATCH_SIZES = (1, 2, 4, 8, 12)


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest()


def peak_working_set_mib() -> float:
    """Process high-water mark (includes both CPU models and Python, not tensor-only)."""
    if sys.platform == "win32":
        from ctypes import wintypes

        class MemoryCounters(ctypes.Structure):
            _fields_ = [
                ("size", wintypes.DWORD), ("page_fault_count", wintypes.DWORD),
                *((name, ctypes.c_size_t) for name in (
                    "peak_working_set", "working_set", "quota_peak_paged_pool",
                    "quota_paged_pool", "quota_peak_nonpaged_pool", "quota_nonpaged_pool",
                    "pagefile_usage", "peak_pagefile_usage",
                )),
            ]

        current_process = ctypes.windll.kernel32.GetCurrentProcess
        current_process.restype = wintypes.HANDLE
        get_memory = ctypes.windll.psapi.GetProcessMemoryInfo
        get_memory.argtypes = [wintypes.HANDLE, ctypes.POINTER(MemoryCounters), wintypes.DWORD]
        get_memory.restype = wintypes.BOOL
        counters = MemoryCounters()
        counters.size = ctypes.sizeof(counters)
        if not get_memory(current_process(), ctypes.byref(counters), counters.size):
            raise OSError("Could not measure process memory")
        size = counters.peak_working_set
    else:
        import resource

        size = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if sys.platform != "darwin":
            size *= 1024
    return round(size / 2**20, 2)


def timed(fn):
    start = perf_counter()
    result = fn()
    return result, round(perf_counter() - start, 6), peak_working_set_mib()


def paired_predictions(reviews, predictions):
    paired = {row.review_id: prediction for row, prediction in zip(reviews, predictions, strict=True)}
    if len(paired) != len(reviews):
        raise ValueError("Duplicate review_id prevents prediction matching")
    for row in reviews:
        if row.content.strip() and paired[row.review_id]["text_score"] == -1:
            raise RuntimeError("Model failed to infer a non-empty review")
    return paired


def measure_text_parity(predictor, batch: CandidateBatch) -> dict:
    single, seconds, memory = timed(lambda: paired_predictions(
        batch.reviews, [predictor.predict_text_score(row.content) for row in batch.reviews],
    ))
    shuffled = list(batch.reviews)
    random.Random(20261116).shuffle(shuffled)
    runs = []
    for batch_size, order in [(size, batch.reviews) for size in BATCH_SIZES] + [(8, shuffled)]:
        observed, elapsed, peak = timed(lambda: paired_predictions(
            order, predictor.predict_text_scores([row.content for row in order], batch_size=batch_size),
        ))
        if single.keys() != observed.keys():
            raise ValueError("Single and batch review IDs do not match")
        grade_changes, score_changes, availability_changes, deltas = 0, 0, 0, []
        for key, expected in single.items():
            actual = observed[key]
            grade_changes += actual["predicted_label"] != expected["predicted_label"]
            score_changes += actual["text_score"] != expected["text_score"]
            a, b = actual["suspicious_probability"], expected["suspicious_probability"]
            availability_changes += (a is None) != (b is None)
            if a is not None and b is not None:
                deltas.append(abs(a - b))
        max_delta = max(deltas, default=0.0)
        runs.append(dict(batch_size=batch_size, shuffled=order is shuffled, seconds=elapsed,
                         cumulative_peak_working_set_mib=peak, label_mismatches=grade_changes,
                         score_mismatches=score_changes, availability_mismatches=availability_changes,
                         max_raw_probability_absolute_delta=max_delta,
                         passed=not (grade_changes or score_changes or availability_changes)
                         and max_delta <= PROBABILITY_TOLERANCE))
    return dict(single_seconds=seconds, cumulative_peak_after_single_mib=memory,
                probability_tolerance=PROBABILITY_TOLERANCE, runs=runs,
                passed=all(run["passed"] for run in runs))


def distribution(values) -> dict:
    counts = Counter(value.upper() if value is not None else "UNAVAILABLE" for value in values)
    return {key: counts[key] for key in ("SAFE", "WARN", "DANGER", "UNAVAILABLE")}


def signed_deltas(rows, field: str) -> dict:
    deltas = [row[f"v3_{field}"] - row[f"v1_{field}"] for row in rows
              if row[f"v3_{field}"] != -1 and row[f"v1_{field}"] != -1]
    return dict(count=len(deltas), mean=round(fmean(deltas), 4) if deltas else None,
                min=round(min(deltas), 4) if deltas else None, max=round(max(deltas), 4) if deltas else None)


def compare_rows(baseline: dict, candidate: dict) -> dict:
    rows = []
    for v1, v3 in zip(baseline["results"], candidate["results"], strict=True):
        if v1["review_id"] != v3["review_id"]:
            raise ValueError("Model comparison review order mismatch")
        # Preserve both baselines, without embedding input text, platform, or source values.
        base, new = v1["original_v05"], v3["original_v05"]
        if base["network_score"] != new["network_score"] or base["behavior_score"] != new["behavior_score"]:
            raise ValueError("Non-text signals changed for the same review context")
        policy = v3["policy_v3"]
        row = dict(review_id=v3["review_id"],
                   v1_text_score=base["text_score"], v3_text_score=new["text_score"],
                   v1_rti=base["rti"], v3_rti=new["rti"], v1_level=base["level"], v3_level=new["level"],
                   behavior_score=new["behavior_score"], network_score=new["network_score"],
                   original_rti=v3["original_rti"], original_level=v3["original_level"],
                   original_v05=new, policy_v3=policy, policy_unavailable_reason=v3["policy_unavailable_reason"])
        if "proposed_web_preview" in v3:
            row["proposed_web_preview"] = v3["proposed_web_preview"]
        rows.append(row)
    count = len(rows)
    grade_changes = sum(row["v1_level"] != row["v3_level"] for row in rows)
    conflicts = [row["review_id"] for row in rows if row["policy_v3"] and row["policy_v3"]["grade_conflict"]]
    return dict(review_count=count, v1_text_to_v3_text_delta=signed_deltas(rows, "text_score"),
                v1_rti_to_v3_rti_delta=signed_deltas(rows, "rti"),
                v1_distribution=distribution(row["v1_level"] for row in rows),
                v3_original_distribution=distribution(row["v3_level"] for row in rows),
                v3_evidence_distribution=distribution(row["policy_v3"]["evidence_level_v3"] if row["policy_v3"] else None
                                                       for row in rows),
                model_grade_changes=grade_changes, model_grade_change_rate=grade_changes / count,
                policy_conflict_review_ids=conflicts, rows=rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--v1-model-path", type=Path, required=True)
    parser.add_argument("--v3-model-path", type=Path, help="Or set PTEXT_V31B_MODEL_PATH")
    parser.add_argument("--output", type=Path, required=True, help="New report file; not checked into Git")
    args = parser.parse_args()
    require_candidate_enabled()
    if args.output.exists():
        parser.error("Use a new report path; existing files are never replaced")
    if sha256(args.fixture) != FIXTURE_SHA256:
        parser.error("Use the original handoff synthetic fixture (SHA-256 mismatch)")
    payload = json.loads(args.fixture.read_text(encoding="utf-8-sig"))
    if payload.get("data_type") != "SYNTHETIC_ONLY" or payload.get("is_ground_truth") is not False:
        parser.error("This runner accepts only the handoff's synthetic fixture, not production reviews")
    batch = CandidateBatch.model_validate({"reviews": payload["reviews"]})

    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    import torch
    import transformers

    torch.set_num_threads(1)
    torch.set_num_interop_threads(1)
    before = peak_working_set_mib()
    v1_hash = sha256(args.v1_model_path / "model.safetensors")
    v3_path = args.v3_model_path or os.environ.get("PTEXT_V31B_MODEL_PATH")
    if not v3_path:
        parser.error("Set PTEXT_V31B_MODEL_PATH or pass --v3-model-path")
    v3_hash = sha256(Path(v3_path) / "model.safetensors")
    if v3_hash != payload["model"]["model_safetensors_sha256"]:
        raise ValueError("Fixture/model digest mismatch")
    v1, v1_load, v1_peak = timed(lambda: TextScorePredictor(args.v1_model_path, device="cpu"))
    parity_v1 = measure_text_parity(v1, batch)
    baseline, v1_batch, _ = timed(lambda: analyze_candidate_batch(
        batch, text_predictor=lambda texts: v1.predict_text_scores(texts, batch_size=8),
    ))
    del v1
    v3, v3_load, v3_peak = timed(lambda: load_candidate_predictor(v3_path, device="cpu"))
    parity_v3 = measure_text_parity(v3, batch)
    candidate, v3_batch, _ = timed(lambda: analyze_candidate_batch(
        batch, text_predictor=lambda texts: v3.predict_text_scores(texts, batch_size=8),
    ))
    report = dict(
        fixture_sha256=FIXTURE_SHA256, synthetic_only=True, is_ground_truth=False, device="cpu",
        torch_version=torch.__version__, transformers_version=transformers.__version__, torch_threads=1,
        baseline=dict(model_version=MODEL_VERSION, model_sha256=v1_hash, load_seconds=v1_load,
                      cumulative_peak_after_load_mib=v1_peak, product_batch_8_seconds=v1_batch, text_parity=parity_v1),
        candidate=dict(model_version=V31B_MODEL_VERSION, model_sha256=v3_hash, load_with_sha256_seconds=v3_load,
                       cumulative_peak_after_load_mib=v3_peak, product_batch_8_seconds=v3_batch, text_parity=parity_v3),
        cumulative_peak_before_models_mib=before, cumulative_peak_working_set_mib=peak_working_set_mib(),
        comparison=compare_rows(baseline, candidate),
    )
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    print(f"Compared {len(batch.reviews)} synthetic reviews: v1 parity={parity_v1['passed']}, "
          f"v3.1 B parity={parity_v3['passed']}")
    return 0 if parity_v1["passed"] and parity_v3["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
