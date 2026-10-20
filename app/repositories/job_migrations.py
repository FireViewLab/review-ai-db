"""작업 설명 버전 컬럼을 명시적으로 업그레이드하고 Worker의 DDL을 직렬화한다."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from hashlib import sha256
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.repositories.mysql_jobs import MySQLJobStore


class SchemaMigrationRequired(RuntimeError):
    """현재 바이너리가 안전하게 사용할 수 없는 결과 저장 스키마다."""


class SchemaLockUnavailable(RuntimeError):
    """다른 startup/migration이 같은 DB의 DDL 잠금을 사용하고 있다."""


def schema_lock_name(database: str) -> str:
    # MySQL named locks are server-wide and limited to 64 characters.
    return "review-ai-jobs-schema:" + sha256(database.encode("utf-8")).hexdigest()[:32]


@contextmanager
def schema_lock(connection, database: str, *, timeout_seconds: int = 10):
    """같은 DB의 startup CREATE와 명시적 ALTER를 같은 connection lock으로 묶는다."""
    if type(timeout_seconds) is not int or not 0 <= timeout_seconds <= 20:
        raise ValueError("Schema lock timeout must be an integer from 0 to 20 seconds")
    with connection.cursor() as cursor:
        name = schema_lock_name(database)
        cursor.execute("SELECT GET_LOCK(%s, %s) AS acquired", (name, timeout_seconds))
        row = cursor.fetchone()
        if row is None or row["acquired"] != 1:
            raise SchemaLockUnavailable("Analysis job schema is busy; retry after other startup/migration finishes")
        try:
            # This dedicated connection closes after schema work. Bound metadata-lock
            # waiting below the store's 30-second client read timeout as well.
            cursor.execute("SET SESSION lock_wait_timeout=10")
            yield cursor
        finally:
            try:
                cursor.execute("SELECT RELEASE_LOCK(%s)", (name,))
            except Exception:
                # Closing the owning connection also releases its named lock. Preserve
                # the original DDL/connection exception instead of masking it here.
                pass


def _explanation_column(cursor, database: str) -> dict | None:
    cursor.execute("""SELECT DATA_TYPE AS data_type,
        CHARACTER_MAXIMUM_LENGTH AS max_length, IS_NULLABLE AS is_nullable,
        COLUMN_DEFAULT AS column_default
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA=%s AND TABLE_NAME='ai_analysis_jobs'
          AND COLUMN_NAME='explanation_policy_version'""", (database,))
    return cursor.fetchone()


def require_explanation_policy_column(cursor, database: str) -> None:
    """읽기 전용 검사다. 기존 테이블을 Worker 시작 중에 자동 ALTER하지 않는다."""
    column = _explanation_column(cursor, database)
    if column is None:
        raise SchemaMigrationRequired(
            "Analysis jobs require the explanation policy migration; "
            "run python -m app.repositories.job_migrations before starting workers"
        )
    if (column["data_type"] != "varchar" or column["max_length"] < 64
            or column["is_nullable"] != "YES" or column["column_default"] is not None):
        raise SchemaMigrationRequired(
            "Analysis jobs explanation_policy_version must be nullable VARCHAR(64 or larger) DEFAULT NULL; "
            "inspect the existing schema before retrying"
        )


def migrate_explanation_policy_version(store: MySQLJobStore, *, timeout_seconds: int = 10) -> str:
    """nullable 컬럼만 추가한다. MySQL DDL implicit commit은 자동 rollback할 수 없다."""
    database = store._settings["database"]
    with store._connection() as connection:
        with schema_lock(connection, database, timeout_seconds=timeout_seconds) as cursor:
            cursor.execute("""SELECT 1 AS present FROM information_schema.TABLES
                WHERE TABLE_SCHEMA=%s AND TABLE_NAME='ai_analysis_jobs'""", (database,))
            if cursor.fetchone() is None:
                store._initialize_schema(cursor)
                return "created"
            column = _explanation_column(cursor, database)
            if column is None:
                cursor.execute("""ALTER TABLE ai_analysis_jobs
                    ADD COLUMN explanation_policy_version VARCHAR(64) NULL DEFAULT NULL,
                    ALGORITHM=INSTANT""")
                status = "added"
            else:
                status = "already_applied"
            require_explanation_policy_column(cursor, database)
            return status


def main(argv: list[str] | None = None) -> int:
    """출력에는 접속 설정이나 DB 예외 원문을 포함하지 않는다."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Read-only schema compatibility check")
    parser.add_argument("--lock-timeout-seconds", type=int, default=10,
                        help="Named-lock wait from 0 to 20 seconds (default: 10); metadata-lock wait stays 10 seconds")
    args = parser.parse_args(argv)
    if not 0 <= args.lock_timeout_seconds <= 20:
        parser.error("--lock-timeout-seconds must be from 0 to 20")
    try:
        from dotenv import load_dotenv
        from app.repositories.mysql_jobs import MySQLJobStore

        load_dotenv()
        store = MySQLJobStore.from_env()
        if args.check:
            with store._connection() as connection, connection.cursor() as cursor:
                require_explanation_policy_column(cursor, store._settings["database"])
            print("Analysis job explanation policy schema: compatible")
        else:
            status = migrate_explanation_policy_version(store, timeout_seconds=args.lock_timeout_seconds)
            print("Analysis job explanation policy migration: " + status)
        return 0
    except Exception:
        print("Analysis job schema operation failed. Check connectivity, privileges and schema; "
              "no analysis rows were rewritten. Follow the migration recovery instructions.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
