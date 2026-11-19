"""Single-host v2 idempotency: per-key OS locks and atomic, durable snapshots."""

import errno
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import sleep
from typing import BinaryIO
from uuid import uuid4

from app.repositories.idempotency import AnalysisClaim, IdempotencyConflict, request_hashes


def _try_lock(path: Path) -> BinaryIO | None:
    lock = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        lock.close()
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            return None
        raise
    return lock


def _sync_dir(path: Path) -> None:
    if os.name != "nt":  # Windows support is for local runs; production uses a Linux named volume.
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _replace_snapshot(path: Path, value: dict) -> None:
    temporary = None
    try:
        with NamedTemporaryFile(dir=path.parent, prefix=".job-", delete=False, mode="w", encoding="utf-8") as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(10):
            try:
                os.replace(temporary, path)
                break
            except PermissionError as exc:
                # Windows readers can temporarily hold the old snapshot open during replace.
                if os.name != "nt" or exc.winerror not in (5, 32) or attempt == 9:
                    raise
                sleep(.01)
        _sync_dir(path.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _read_snapshot(path: Path) -> dict | None:
    try:
        with path.open(encoding="utf-8") as stream:
            value = json.load(stream)
    except FileNotFoundError:
        return None
    if (not isinstance(value, dict) or value.get("status") not in ("RUNNING", "FAILED", "DONE")
            or not isinstance(value.get("job_id"), str) or not isinstance(value.get("payload_hash"), str)
            or not isinstance(value.get("versions"), dict)
            or set(value["versions"]) != {"contract_version", "model_version", "policy_version"}
            or not all(isinstance(version, str) for version in value["versions"].values())
            or value["versions"]["contract_version"] != "v2"
            or (value["status"] == "DONE" and not isinstance(value.get("result"), dict))):
        raise ValueError("Invalid v2 job snapshot")
    return value


@dataclass(frozen=True)
class V2Job:
    claim: AnalysisClaim
    result: dict | None
    _path: Path
    _snapshot: dict

    def complete(self, result: dict, *, explanation_policy_version: str) -> None:
        if not self.claim.is_new:
            raise ValueError("Cannot complete a replay")
        _replace_snapshot(self._path, {**self._snapshot, "status": "DONE", "result": result,
                                      "explanation_policy_version": explanation_policy_version})

    def fail(self, code: str) -> None:
        if not self.claim.is_new:
            raise ValueError("Cannot fail a replay")
        _replace_snapshot(self._path, {**self._snapshot, "status": "FAILED", "error_code": code})


class FileV2JobStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    @classmethod
    def from_env(cls):
        return cls(os.getenv("V2_JOB_DIR") or "data/v2-jobs")

    @contextmanager
    def claim(self, key: str, payload: dict, versions: dict):
        key_hash, payload_hash = request_hashes(key, payload)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        _sync_dir(self.root.parent)
        path = self.root / f"{key_hash}.json"
        lock = _try_lock(self.root / f"{key_hash}.lock")
        try:
            snapshot = _read_snapshot(path)
            if snapshot is not None and snapshot["payload_hash"] != payload_hash:
                raise IdempotencyConflict("IDEMPOTENCY_KEY_REUSED")
            if lock is None:
                raise IdempotencyConflict("IDEMPOTENCY_IN_PROGRESS")
            if snapshot is not None:
                if snapshot["status"] == "DONE":
                    _sync_dir(self.root)  # Also covers a prior rename whose directory sync was interrupted.
                    yield V2Job(AnalysisClaim(snapshot["job_id"], False, snapshot["versions"]),
                                snapshot["result"], path, snapshot)
                    return
                if snapshot["status"] == "FAILED" or snapshot["versions"] != versions:
                    raise IdempotencyConflict("IDEMPOTENCY_FAILED")
                # RUNNING with no lock holder: the previous process was interrupted. Same input and versions only.
            else:
                snapshot = dict(job_id=str(uuid4()), payload_hash=payload_hash, status="RUNNING", versions=dict(versions))
                _replace_snapshot(path, snapshot)
            yield V2Job(AnalysisClaim(snapshot["job_id"], True, snapshot["versions"]), None, path, snapshot)
        finally:
            if lock is not None:
                lock.close()
