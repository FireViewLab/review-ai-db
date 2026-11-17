"""기존 분석 계약을 유지하며 MySQL에 작업·입력·결과를 트랜잭션으로 저장한다."""
import json
import os
from contextlib import contextmanager
from uuid import uuid4
from app.repositories.idempotency import AnalysisClaim, existing_claim, request_hashes
from app.repositories.job_migrations import require_explanation_policy_column, schema_lock

JOB_TABLES = {
    "v1": ("ai_analysis_jobs", "ai_analysis_idempotency"),
    "v2": ("ai_analysis_v2_jobs", "ai_analysis_v2_idempotency"),
}


class MySQLJobStore:
    def __init__(self, *, host, port, user, password, database, scope="v1"):
        self._settings = dict(host=host, port=port, user=user,
                              password=password, database=database)
        self._jobs, self._keys = JOB_TABLES[scope]

    @classmethod
    def from_env(cls, *, scope="v1"):
        password = os.getenv("DB_PASSWORD")
        if not password:
            raise ValueError("MySQL requires DB_PASSWORD; SQLite fallback is disabled")
        return cls(host=os.getenv("DB_HOST", "127.0.0.1"),
                   port=int(os.getenv("DB_PORT", "3307")),
                   user=os.getenv("DB_USER", "root"), password=password,
                   database=os.getenv("DB_NAME", "review_system"), scope=scope)

    @contextmanager
    def _connection(self):
        import pymysql
        db = pymysql.connect(**self._settings, charset="utf8mb4",
                             cursorclass=pymysql.cursors.DictCursor,
                             autocommit=False, connect_timeout=10,
                             read_timeout=30, write_timeout=30)
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def initialize(self):
        # Serialize fresh-install CREATE/check across Workers; upgrades are explicit, never startup ALTER.
        with self._connection() as db:
            with schema_lock(db, self._settings["database"]) as cursor:
                self._initialize_schema(cursor)

    def _initialize_schema(self, cursor):
        """Called with the schema advisory lock held, including by the explicit migrator."""
        cursor.execute(f"""CREATE TABLE IF NOT EXISTS {self._jobs} (
                job_id VARCHAR(36) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
                status VARCHAR(16) NOT NULL,
                request_json JSON NOT NULL,
                result_json JSON NULL,
                error_code VARCHAR(255) NULL,
                explanation_policy_version VARCHAR(64) NULL DEFAULT NULL,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
        cursor.execute(f"""CREATE TABLE IF NOT EXISTS {self._keys} (
                key_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
                payload_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
                job_id VARCHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL UNIQUE,
                metadata_json JSON NOT NULL,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (job_id) REFERENCES {self._jobs}(job_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
        if self._jobs == JOB_TABLES["v1"][0]:
            require_explanation_policy_column(cursor, self._settings["database"])

    def claim_idempotent(self, key: str, payload: dict, versions: dict) -> AnalysisClaim:
        import pymysql
        key_hash, payload_hash = request_hashes(key, payload)
        job_id = str(uuid4())
        try:
            # Both inserts commit together. A losing concurrent insert rolls back its job.
            with self._connection() as db, db.cursor() as cursor:
                cursor.execute(f"INSERT INTO {self._jobs}(job_id,status,request_json) VALUES (%s, 'RUNNING', %s)",
                               (job_id, json.dumps(payload, ensure_ascii=False)))
                cursor.execute(f"""INSERT INTO {self._keys}
                    (key_hash,payload_hash,job_id,metadata_json) VALUES (%s,%s,%s,%s)""",
                    (key_hash, payload_hash, job_id, json.dumps(versions)))
        except pymysql.err.IntegrityError as exc:
            if exc.args[0] != 1062:
                raise
            with self._connection() as db, db.cursor() as cursor:
                cursor.execute(f"""SELECT i.*, j.status FROM {self._keys} i
                    JOIN {self._jobs} j ON j.job_id=i.job_id WHERE i.key_hash=%s""",
                    (key_hash,))
                row = cursor.fetchone()
            if row is None:
                raise RuntimeError("Idempotency record unavailable") from None
            return existing_claim(row, payload_hash)
        return AnalysisClaim(job_id, True, dict(versions))

    def create(self, payload: dict) -> str:
        job_id = str(uuid4())
        self._execute(
            f"INSERT INTO {self._jobs}(job_id,status,request_json) VALUES (%s, 'RUNNING', %s)",
            (job_id, json.dumps(payload, ensure_ascii=False)))
        return job_id

    def _execute(self, sql, params):
        with self._connection() as db, db.cursor() as cursor:
            cursor.execute(sql, params)

    def save_input(self, job_id: str, payload: dict) -> None:
        self._execute(
            f"UPDATE {self._jobs} SET request_json=%s, updated_at=CURRENT_TIMESTAMP WHERE job_id=%s",
            (json.dumps(payload, ensure_ascii=False), job_id))

    def complete(self, job_id: str, result: dict, *, explanation_policy_version: str | None = None) -> None:
        self._execute(f"""UPDATE {self._jobs} SET status='DONE', result_json=%s,
            explanation_policy_version=%s, error_code=NULL, updated_at=CURRENT_TIMESTAMP WHERE job_id=%s""",
            (json.dumps(result, ensure_ascii=False), explanation_policy_version, job_id))

    def fail(self, job_id: str, code: str) -> None:
        self._execute(f"""UPDATE {self._jobs} SET status='FAILED', error_code=%s,
            updated_at=CURRENT_TIMESTAMP WHERE job_id=%s AND status='RUNNING'""",
            (code, job_id))

    def get(self, job_id: str) -> dict | None:
        with self._connection() as db, db.cursor() as cursor:
            cursor.execute(f"SELECT * FROM {self._jobs} WHERE job_id=%s", (job_id,))
            row = cursor.fetchone()
        if row is None:
            return None
        result = dict(row)
        result["request"] = json.loads(result.pop("request_json"))
        raw = result.pop("result_json")
        result["result"] = json.loads(raw) if raw is not None else None
        for key in ("created_at", "updated_at"):
            result[key] = result[key].isoformat(sep=" ")
        return result
