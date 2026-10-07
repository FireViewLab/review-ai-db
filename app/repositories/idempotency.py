"""DB 기반 중복 요청 판정과 안정적인 요청 해시를 정의한다."""

import hashlib
import json
from dataclasses import dataclass


class IdempotencyConflict(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class AnalysisClaim:
    job_id: str
    is_new: bool
    versions: dict


def request_hashes(key: str, payload: dict) -> tuple[str, str]:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"))
    return (hashlib.sha256(key.encode("utf-8")).hexdigest(),
            hashlib.sha256(canonical.encode("utf-8")).hexdigest())


def existing_claim(row: dict, payload_hash: str) -> AnalysisClaim:
    if row["payload_hash"] != payload_hash:
        raise IdempotencyConflict("IDEMPOTENCY_KEY_REUSED")
    if row["status"] == "RUNNING":
        raise IdempotencyConflict("IDEMPOTENCY_IN_PROGRESS")
    if row["status"] != "DONE":
        raise IdempotencyConflict("IDEMPOTENCY_FAILED")
    return AnalysisClaim(row["job_id"], False, json.loads(row["metadata_json"]))
