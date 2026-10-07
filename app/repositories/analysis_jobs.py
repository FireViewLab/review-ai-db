"""저장소 인터페이스와 격리 테스트·과거 파일 조회용 SQLite 구현을 제공한다."""
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Protocol
from uuid import uuid4
from app.repositories.idempotency import AnalysisClaim, existing_claim, request_hashes


class JobStore(Protocol):
    def initialize(self) -> None: ...
    def create(self, payload: dict) -> str: ...
    def save_input(self, job_id: str, payload: dict) -> None: ...
    def complete(self, job_id: str, result: dict) -> None: ...
    def fail(self, job_id: str, code: str) -> None: ...
    def get(self, job_id: str) -> dict | None: ...
    def claim_idempotent(self, key: str, payload: dict, versions: dict) -> AnalysisClaim: ...


class SQLiteJobStore:
    def __init__(self, path: str):
        self.path = path

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def initialize(self):
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS ai_analysis_jobs (
                job_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                request_json TEXT NOT NULL,
                result_json TEXT,
                error_code TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS ai_analysis_idempotency (
                key_hash TEXT PRIMARY KEY,
                payload_hash TEXT NOT NULL,
                job_id TEXT NOT NULL UNIQUE REFERENCES ai_analysis_jobs(job_id),
                metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")

    def claim_idempotent(self, key: str, payload: dict, versions: dict) -> AnalysisClaim:
        key_hash, payload_hash = request_hashes(key, payload)
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT i.*, j.status FROM ai_analysis_idempotency i
                JOIN ai_analysis_jobs j ON j.job_id=i.job_id WHERE i.key_hash=?""",
                (key_hash,)).fetchone()
            if row is not None:
                return existing_claim(dict(row), payload_hash)
            job_id = str(uuid4())
            db.execute("INSERT INTO ai_analysis_jobs(job_id,status,request_json) VALUES (?, 'RUNNING', ?)",
                       (job_id, json.dumps(payload, ensure_ascii=False)))
            db.execute("""INSERT INTO ai_analysis_idempotency
                (key_hash,payload_hash,job_id,metadata_json) VALUES (?,?,?,?)""",
                (key_hash, payload_hash, job_id, json.dumps(versions)))
        return AnalysisClaim(job_id, True, dict(versions))

    def create(self, payload: dict) -> str:
        job_id = str(uuid4())
        with closing(self._connect()) as db, db:
            db.execute(
                "INSERT INTO ai_analysis_jobs(job_id,status,request_json) VALUES (?, 'RUNNING', ?)",
                (job_id, json.dumps(payload, ensure_ascii=False)),
            )
        return job_id

    def save_input(self, job_id: str, payload: dict) -> None:
        with closing(self._connect()) as db, db:
            db.execute(
                "UPDATE ai_analysis_jobs SET request_json=?, updated_at=CURRENT_TIMESTAMP WHERE job_id=?",
                (json.dumps(payload, ensure_ascii=False), job_id),
            )

    def complete(self, job_id: str, result: dict) -> None:
        with closing(self._connect()) as db, db:
            db.execute(
                """UPDATE ai_analysis_jobs SET status='DONE', result_json=?,
                error_code=NULL, updated_at=CURRENT_TIMESTAMP WHERE job_id=?""",
                (json.dumps(result, ensure_ascii=False), job_id),
            )

    def fail(self, job_id: str, code: str) -> None:
        with closing(self._connect()) as db, db:
            db.execute(
                """UPDATE ai_analysis_jobs SET status='FAILED', error_code=?,
                updated_at=CURRENT_TIMESTAMP WHERE job_id=? AND status='RUNNING'""",
                (code, job_id),
            )

    def get(self, job_id: str) -> dict | None:
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM ai_analysis_jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["request"] = json.loads(result.pop("request_json"))
        raw = result.pop("result_json")
        result["result"] = json.loads(raw) if raw else None
        return result
