"""Process isolation and recovery regression tests for the standalone v2 cache."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from app.contracts.data_ai_v2 import v2_versions
from app.repositories.idempotency import IdempotencyConflict, request_hashes
from app.repositories.v2_file_jobs import FileV2JobStore

KEY = "do-not-persist-this-key"
PAYLOAD = {"content": "do-not-persist-the-body"}
ROOT = Path(__file__).resolve().parents[1]


def child(path, *, key=KEY, payload=PAYLOAD, crash=False):
    script = """
import json, os, sys
from app.contracts.data_ai_v2 import v2_versions
from app.repositories.idempotency import IdempotencyConflict
from app.repositories.v2_file_jobs import FileV2JobStore

root, key, data, crash = sys.argv[1:]
try:
    with FileV2JobStore(root).claim(key, json.loads(data), v2_versions()) as job:
        print(json.dumps([job.claim.job_id, job.claim.is_new, job.result]), flush=True)
        if crash == "True":
            os._exit(25)
except IdempotencyConflict as exc:
    print(exc.code)
"""
    return subprocess.run([sys.executable, "-c", script, str(path), key, json.dumps(payload), str(crash)],
                          cwd=ROOT, text=True, capture_output=True, timeout=20)


def test_cross_process_contention_and_replay(tmp_path):
    store = FileV2JobStore(tmp_path / "v2")
    result = {"results": [{"review_id": "1"}]}
    with store.claim(KEY, PAYLOAD, v2_versions()) as job:
        job_id = job.claim.job_id
        for payload, code in ((PAYLOAD, "IDEMPOTENCY_IN_PROGRESS"), ({}, "IDEMPOTENCY_KEY_REUSED")):
            probe = child(store.root, payload=payload)
            assert probe.returncode == 0, probe.stderr
            assert probe.stdout.strip() == code
        job.complete(result, explanation_policy_version="test")
    probe = child(store.root)
    assert probe.returncode == 0, probe.stderr
    assert json.loads(probe.stdout) == [job_id, False, result]
    snapshot = next(store.root.glob("*.json")).read_text(encoding="utf-8")
    assert KEY not in snapshot and PAYLOAD["content"] not in snapshot


def test_crash_recovery_without_new_job_id(tmp_path):
    store = FileV2JobStore(tmp_path / "v2")
    crashed = child(store.root, crash=True)
    assert crashed.returncode == 25, crashed.stderr
    original, fresh, result = json.loads(crashed.stdout)
    assert fresh and result is None
    with pytest.raises(IdempotencyConflict, match="IDEMPOTENCY_KEY_REUSED"):
        with store.claim(KEY, {}, v2_versions()):
            pytest.fail("A crashed job key is still bound to its input")
    with pytest.raises(IdempotencyConflict, match="IDEMPOTENCY_FAILED"):
        with store.claim(KEY, PAYLOAD, {**v2_versions(), "model_version": "new-model"}):
            pytest.fail("Never relabel recalculated results with the old model version")
    with store.claim(KEY, PAYLOAD, v2_versions()) as job:
        assert job.claim.job_id == original and job.claim.is_new
        job.complete({"ok": True}, explanation_policy_version="test")
    with store.claim(KEY, PAYLOAD, {**v2_versions(), "model_version": "new-model"}) as job:
        assert job.claim.job_id == original and not job.claim.is_new
        assert job.claim.versions == v2_versions() and job.result == {"ok": True}


def test_failed_key_and_corrupted_snapshot_are_not_recomputed(tmp_path):
    store = FileV2JobStore(tmp_path / "v2")
    with store.claim(KEY, PAYLOAD, v2_versions()) as job:
        job.fail("V2_MODEL_UNAVAILABLE")
    with pytest.raises(IdempotencyConflict, match="IDEMPOTENCY_FAILED"):
        with store.claim(KEY, PAYLOAD, v2_versions()):
            pytest.fail("FAILED results never replay")
    key_hash, _ = request_hashes(KEY, PAYLOAD)
    (store.root / f"{key_hash}.json").write_text("{", encoding="utf-8")
    with pytest.raises(ValueError):
        with store.claim(KEY, PAYLOAD, v2_versions()):
            pytest.fail("Corrupt snapshot must fail closed")
