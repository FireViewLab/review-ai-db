"""Compare fixed synthetic Evidence v3 reasons locally; Groq is opt-in and never serves the website."""

import argparse
from dataclasses import dataclass
import json
from math import isfinite
from pathlib import Path
import sys
from typing import Literal

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.experimental.candidate_analysis import _REASON_MESSAGES
from app.integrations.groq_reason_naturalizer import (
    GROQ_ENDPOINT, GroqReasonSettings, load_settings, naturalize_reasons_batch,
)
from app.scoring.meta_scorer import classify_rti
from scripts.compare_ai_v3_candidate import FIXTURE_SHA256, sha256

LIVE_REVIEW_IDS = ("T01", "T02", "T05", "T10")  # SAFE, DANGER, WARN/unknown, WARN/length boundary.
MOCK_SCENARIOS = (
    "success", "timeout", "invalid_json", "missing_reason", "rate_limited",
    "rate_limit_recovery", "reordered_review", "reordered_reason", "extra_score",
    "unsafe_language",
)
LOCAL_OUTPUT = Path(__file__).resolve().parents[1] / "output"


@dataclass(frozen=True, slots=True)
class ReasonCase:
    review_id: str
    content: str
    policy_index_v3: float
    evidence_level_v3: str
    codes: tuple[str, ...]
    templates: tuple[str, ...]


def load_cases(fixture: Path, comparison: Path) -> tuple[ReasonCase, ...]:
    """Only the original handoff fixture can ever supply review text."""
    if sha256(fixture) != FIXTURE_SHA256:
        raise ValueError("Use the original synthetic fixture (SHA-256 mismatch)")
    reviews = json.loads(fixture.read_text(encoding="utf-8-sig"))["reviews"]
    report = json.loads(comparison.read_text(encoding="utf-8"))
    if (report["fixture_sha256"] != FIXTURE_SHA256
            or report["synthetic_only"] is not True or report["is_ground_truth"] is not False):
        raise ValueError("The comparison is not from the original synthetic fixture")
    rows = report["comparison"]["rows"]
    ids = [review["review_id"] for review in reviews]
    if (len(ids) != 12 or len(set(ids)) != 12
            or [row["review_id"] for row in rows] != ids):
        raise ValueError("Comparison must contain the 12 synthetic rows in fixture order")

    cases = []
    for review, row in zip(reviews, rows, strict=True):
        policy = row["policy_v3"]
        preview = row["proposed_web_preview"]
        score = policy["policy_index_v3"]
        level = policy["evidence_level_v3"]
        codes = policy["evidence_reasons_v3"]
        if (not isinstance(codes, list) or not codes
                or any(not isinstance(code, str) or code not in _REASON_MESSAGES for code in codes)):
            raise ValueError("Missing or unknown Evidence v3 code")
        templates = tuple(_REASON_MESSAGES[code] for code in codes)
        if (isinstance(score, bool) or not isinstance(score, (float, int)) or not isfinite(score)
                or not 0 <= score <= 100 or classify_rti(score).value.upper() != level
                or preview["review_id"] != review["review_id"]
                or preview["rti"] != score or preview["level"] != level.lower()
                or preview["reasons"] != list(templates)):
            raise ValueError("Policy output does not match the original rules-based preview")
        cases.append(ReasonCase(review["review_id"], review["content"], score, level, tuple(codes), templates))
    return tuple(cases)


def compare_cases(cases: tuple[ReasonCase, ...], *, mode: Literal["mock", "live"],
                  settings: GroqReasonSettings, transport) -> list[dict]:
    """Reuse the production converter; accept text for offline review only, never for display."""
    if mode not in ("mock", "live") or not cases or len(cases) > (12 if mode == "mock" else 5):
        raise ValueError("Invalid comparison mode or batch size")
    if transport is None:
        raise ValueError("An explicit transport is required")
    codes = [list(case.codes) for case in cases]
    try:
        phrases = naturalize_reasons_batch(
            contents=[case.content for case in cases], reasons=codes,
            settings=settings, transport=transport,
        )
    except Exception:
        phrases = []
    if not isinstance(phrases, list) or len(phrases) != len(cases):
        phrases = [None] * len(cases)
    blocked = [value.casefold() for value in
               (settings.model, settings.primary_key, settings.secondary_key) if value]
    rows = []
    for case, messages in zip(cases, phrases, strict=True):
        accepted = (
            isinstance(messages, list) and len(messages) == len(case.codes)
            and all(isinstance(msg, str) and msg.strip() and msg.strip() != code
                    and not any(value in msg.casefold() for value in blocked)
                    for code, msg in zip(case.codes, messages))
        )
        # A raw code is the existing converter's abstention/failure signal. Fall
        # back for the whole review rather than mixing partially checked wording.
        generated = list(messages) if accepted else None
        rows.append({
            "review_id": case.review_id,
            "policy_index_v3": case.policy_index_v3,
            "evidence_level_v3": case.evidence_level_v3,
            "evidence_reasons_v3": list(case.codes),
            "rules_reasons_ko": list(case.templates),
            "mock_reasons_ko": generated if mode == "mock" else None,
            "groq_reasons_ko": generated if mode == "live" else None,
            "display_reasons_ko": list(case.templates),
            "status": "offline_review_only" if accepted else "rules_fallback",
            # Schema verification does not establish semantics or detect invented facts.
            "meaning_matches": None,
            "unsupported_or_exaggerated": None,
            "review_status": "requires_semantic_review" if accepted else "no_generated_text",
            "rules_total_chars": sum(map(len, case.templates)),
            "generated_total_chars": sum(map(len, generated)) if generated is not None else None,
        })
    return rows


def mock_run(cases: tuple[ReasonCase, ...], scenario: str) -> dict:
    if scenario not in MOCK_SCENARIOS:
        raise ValueError("Unknown mock scenario")
    settings = GroqReasonSettings(True, "test-only-primary", "test-only-secondary", "test-only-model")
    statuses = []
    seen_ids = []
    used_secondary = []
    request_contract_ok = []

    def handler(request):
        body = json.loads(request.content)
        items = json.loads(body["messages"][-1]["content"])["items"]
        request_contract_ok.append(
            str(request.url) == GROQ_ENDPOINT
            and body["response_format"]["type"] == "json_schema"
            and body["response_format"]["json_schema"]["strict"] is True
            and body["reasoning_effort"] == "low" and body["temperature"] == 0
            and all(item == {"item_id": item["item_id"],
                             "content": cases[int(item["item_id"])].content,
                             "reasons": list(cases[int(item["item_id"])].codes)} for item in items)
        )
        seen_ids.append([cases[int(item["item_id"])].review_id for item in items])
        used_secondary.append(request.headers["Authorization"] == "Bearer " + settings.secondary_key)
        if scenario == "timeout":
            statuses.append("timeout")
            raise httpx.ReadTimeout("simulated timeout")
        if scenario == "rate_limited" or (scenario == "rate_limit_recovery" and not used_secondary[-1]):
            statuses.append(429)
            return httpx.Response(429)
        statuses.append(200)
        replies = [{"item_id": item["item_id"], "reasons": [
            {"code": code, "message": _REASON_MESSAGES[code]}
            for code in item["reasons"]
        ]} for item in items]
        if scenario == "missing_reason":
            replies[0]["reasons"].pop()
        if scenario == "reordered_review":
            replies.reverse()
        if scenario == "reordered_reason":
            replies[0]["reasons"].reverse()
        if scenario == "unsafe_language":
            # Structurally valid but false: these are intentionally bad mock phrases.
            replies[0]["reasons"][1]["message"] = "사진 때문에 실제 구매가 확인됐습니다."
            replies[0]["reasons"][4]["message"] = "광고성 허위 리뷰로 확인됐습니다."
            replies[4]["reasons"][1]["message"] = "첨부 사진이 없다는 사실이 확인됐습니다."
            replies[4]["reasons"][2]["message"] = "별점 5점으로 확인됐습니다."
        result = {"items": replies}
        if scenario == "extra_score":
            result["items"][0]["policy_index_v3"] = 100
        content = "{invalid-json" if scenario == "invalid_json" else json.dumps(result, ensure_ascii=False)
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": content}}]})

    rows = compare_cases(cases, mode="mock", settings=settings, transport=httpx.MockTransport(handler))
    for row in rows:
        example = row["mock_reasons_ko"]
        if example is not None:
            # Only canned fixtures are labeled this way; live output is always unreviewed here.
            row["meaning_matches"] = example == row["rules_reasons_ko"]
            row["unsupported_or_exaggerated"] = not row["meaning_matches"]
            row["review_status"] = "known_mock_fixture"
    return {"scenario": scenario, "request_statuses": statuses, "requested_review_ids": seen_ids,
            "used_secondary": used_secondary, "request_contract_ok": request_contract_ok, "rows": rows}


class LiveTransport(httpx.AsyncBaseTransport):
    """Record only status codes, and cap the one small comparison at two attempts."""

    def __init__(self):
        self.transport = httpx.AsyncHTTPTransport(trust_env=False, retries=0)
        self.statuses: list[int | str] = []

    async def handle_async_request(self, request):
        if len(self.statuses) >= 2 or str(request.url) != GROQ_ENDPOINT:
            raise httpx.RequestError("Live experiment request refused")
        self.statuses.append("transport_error_or_timeout")
        try:
            response = await self.transport.handle_async_request(request)
        except httpx.TransportError:
            raise httpx.TransportError("Live transport failed") from None
        self.statuses[-1] = response.status_code
        return response

    async def aclose(self):
        await self.transport.aclose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--comparison", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="New ignored JSON under the repository output/")
    parser.add_argument("--live", action="store_true", help="Explicitly authorize sending the four synthetic reviews to Groq")
    args = parser.parse_args()
    output = args.output.resolve()
    if output.parent != LOCAL_OUTPUT or output.suffix != ".json" or output.name.endswith("_sample.json") or output.exists():
        parser.error("Use a new, ignored .json file directly in output/")
    try:
        cases = load_cases(args.fixture, args.comparison)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        parser.error("Invalid synthetic fixture or v3 comparison: check input paths, digest and preview")

    if args.live:
        from dotenv import load_dotenv

        load_dotenv(LOCAL_OUTPUT.parent / ".env", override=False)
        settings = load_settings()
        if not settings.enabled:
            parser.error("Existing Groq settings are disabled, incomplete or invalid; no call made")
        by_id = {case.review_id: case for case in cases}
        subset = tuple(by_id[review_id] for review_id in LIVE_REVIEW_IDS)
        transport = LiveTransport()
        rows = compare_cases(subset, mode="live", settings=settings, transport=transport)
        data = {"mode": "live", "reason_source": "evidence_v3_only", "request_statuses": transport.statuses,
                "schema_and_options_accepted": bool(transport.statuses)
                and all(row["groq_reasons_ko"] is not None for row in rows),
                "rows": rows}
        summary = f"Local Groq report: {len(rows)} synthetic reviews; semantic review required."
    else:
        scenarios = [mock_run(cases, scenario) for scenario in MOCK_SCENARIOS]
        data = {"mode": "mock", "reason_source": "evidence_v3_only", "external_calls": 0,
                "live_language_quality_verified": False, "scenarios": scenarios}
        summary = f"Local mock report: {len(cases)} synthetic reviews × {len(scenarios)} scenarios; no external calls."
    with output.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
