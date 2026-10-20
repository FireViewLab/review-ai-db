"""Find CSV rows immediately below and at/above the configured P_text threshold."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any, TextIO

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.analyzers import p_text  # noqa: E402

DEFAULT_CONFIG = REPO_ROOT / "models/ptext-koelectra-v1-2epoch-20260929/inference_config.json"


def configured_threshold(config_path: Path) -> float:
    settings = json.loads(config_path.read_text(encoding="utf-8"))
    threshold = settings["threshold"]
    p_text.probability_result(0.0, threshold)
    return float(threshold)


def find_candidates(
    csv_path: Path,
    *,
    limit: int,
    content_column: str,
    encoding: str,
    threshold: float,
    model_path: str | None = None,
) -> dict[str, Any]:
    if limit < 1:
        raise ValueError("limit must be at least 1")
    below: dict[str, Any] | None = None
    above: dict[str, Any] | None = None
    rows_read = 0
    inferred = 0
    with csv_path.open("r", encoding=encoding, newline="") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames is None or content_column not in reader.fieldnames:
            raise ValueError(f"CSV must contain a {content_column!r} column")
        for row_number, row in enumerate(reader, start=2):
            if rows_read >= limit:
                break
            rows_read += 1
            content = row.get(content_column)
            if not isinstance(content, str) or not content.strip():
                continue
            prediction = p_text.predict_text_score(content, model_path=model_path)
            probability = prediction.get("suspicious_probability")
            if probability is None:
                continue
            inferred += 1
            candidate = {
                "row_number": row_number,
                "content": content,
                "text_score": prediction["text_score"],
                "suspicious_probability": probability,
                "predicted_label": prediction["predicted_label"],
                "absolute_delta_from_threshold": abs(probability - threshold),
            }
            if probability < threshold:
                if below is None or probability > below["suspicious_probability"]:
                    below = candidate
            elif above is None or probability < above["suspicious_probability"]:
                above = candidate
    if inferred == 0:
        raise RuntimeError("no analyzable row produced an available inference result")
    return {
        "source_csv": str(csv_path),
        "content_column": content_column,
        "threshold": threshold,
        "row_limit": limit,
        "rows_read": rows_read,
        "rows_inferred": inferred,
        "closest_below": below,
        "closest_at_or_above": above,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Find P_text predictions nearest the configured threshold")
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("--limit", type=int, default=1000, help="maximum data rows to inspect (default: 1000)")
    parser.add_argument("--content-column", default="content")
    parser.add_argument("--encoding", default="utf-8-sig")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--model-path", default=None)
    parser.add_argument("--output", type=Path, help="optional JSON output; never modifies the parity fixture")
    return parser.parse_args()


def write_result(result: dict[str, Any], output: Path | None, stream: TextIO = sys.stdout) -> None:
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if output is None:
        stream.write(rendered)
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
        stream.write(f"Wrote candidates to {output}\n")


def main() -> int:
    args = parse_args()
    threshold = configured_threshold(args.config)
    result = find_candidates(
        args.csv_path,
        limit=args.limit,
        content_column=args.content_column,
        encoding=args.encoding,
        threshold=threshold,
        model_path=args.model_path,
    )
    write_result(result, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
