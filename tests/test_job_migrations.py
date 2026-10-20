"""명시적 설명 버전 migration의 잠금·재실행·과거 데이터 보존을 검증한다."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
import re
from threading import Barrier
from unittest.mock import Mock
from uuid import uuid4

import pytest

from app.repositories import job_migrations as migrations
from app.repositories.idempotency import request_hashes
from app.repositories.mysql_jobs import MySQLJobStore


def compatible_column(**overrides):
    column = dict(data_type="varchar", max_length=64, is_nullable="YES", column_default=None)
    column.update(overrides)
    return column


class SchemaCursor:
    def __init__(self, *, table_exists=True, column=None, lock=1, alter_error=None):
        self.table_exists = table_exists
        self.column = column
        self.lock = lock
        self.alter_error = alter_error
        self.statements = []
        self.row = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=()):
        self.statements.append((sql, params))
        if "GET_LOCK" in sql:
            self.row = {"acquired": self.lock}
        elif "information_schema.TABLES" in sql:
            self.row = {"present": 1} if self.table_exists else None
        elif "information_schema.COLUMNS" in sql:
            self.row = self.column
        elif "ALTER TABLE" in sql:
            if self.alter_error:
                raise self.alter_error
            self.column = compatible_column()
        else:
            self.row = None

    def fetchone(self):
        return self.row


def fake_store(cursor):
    store = Mock()
    store._settings = {"database": "validation-schema"}
    connection = Mock()
    connection.cursor.return_value = cursor

    @contextmanager
    def connect():
        yield connection

    store._connection = connect

    def initialize(_cursor):
        assert _cursor is cursor
        cursor.table_exists = True
        cursor.column = compatible_column()

    store._initialize_schema.side_effect = initialize
    return store, connection


def test_explicit_migration_adds_only_missing_nullable_column_and_is_repeatable():
    cursor = SchemaCursor()
    store, _ = fake_store(cursor)
    assert migrations.migrate_explanation_policy_version(store) == "added"
    assert migrations.migrate_explanation_policy_version(store) == "already_applied"
    sql = [statement for statement, _ in cursor.statements]
    assert sum("ALTER TABLE" in statement for statement in sql) == 1
    assert not any(re.search(r"\b(UPDATE|DELETE|DROP|TRUNCATE|INSERT)\b", statement)
                   for statement in sql)
    alter = next(statement for statement in sql if "ALTER TABLE" in statement)
    assert "explanation_policy_version VARCHAR(64) NULL DEFAULT NULL" in alter
    assert "ALGORITHM=INSTANT" in alter
    assert sql.index("SET SESSION lock_wait_timeout=10") < sql.index(alter)
    store._initialize_schema.assert_not_called()


def test_fresh_install_uses_same_locked_connection_schema_initializer():
    cursor = SchemaCursor(table_exists=False)
    store, _ = fake_store(cursor)
    assert migrations.migrate_explanation_policy_version(store) == "created"
    store._initialize_schema.assert_called_once_with(cursor)
    assert not any("ALTER TABLE" in statement for statement, _ in cursor.statements)


@pytest.mark.parametrize("lock", [0, None])
def test_lock_unavailable_never_runs_schema_ddl(lock):
    cursor = SchemaCursor(lock=lock)
    store, _ = fake_store(cursor)
    with pytest.raises(migrations.SchemaLockUnavailable):
        migrations.migrate_explanation_policy_version(store, timeout_seconds=0)
    assert len(cursor.statements) == 1


def test_failed_ddl_releases_lock_without_rewriting_records():
    cursor = SchemaCursor(alter_error=RuntimeError("synthetic schema failure"))
    store, _ = fake_store(cursor)
    with pytest.raises(RuntimeError, match="synthetic schema failure"):
        migrations.migrate_explanation_policy_version(store)
    assert "RELEASE_LOCK" in cursor.statements[-1][0]
    assert cursor.column is None
    alter_statements = [sql for sql, _ in cursor.statements if "ALTER TABLE" in sql]
    assert len(alter_statements) == 1 and "ALGORITHM=INSTANT" in alter_statements[0]


@pytest.mark.parametrize("column", [None,
    dict(data_type="text", max_length=65535, is_nullable="YES", column_default=None),
    dict(data_type="varchar", max_length=32, is_nullable="YES", column_default=None),
    dict(data_type="varchar", max_length=64, is_nullable="NO", column_default=None),
    dict(data_type="varchar", max_length=64, is_nullable="YES", column_default="neutral-summary-v1"),
])
def test_schema_check_rejects_missing_or_incompatible_column_without_ddl(column):
    cursor = SchemaCursor(column=column)
    with pytest.raises(migrations.SchemaMigrationRequired):
        migrations.require_explanation_policy_column(cursor, "validation-schema")
    assert len(cursor.statements) == 1
    assert "information_schema.COLUMNS" in cursor.statements[0][0]


@pytest.mark.parametrize("length", [64, 128])
def test_schema_check_accepts_nullable_no_default_column(length):
    cursor = SchemaCursor(column=dict(data_type="varchar", max_length=length,
                                      is_nullable="YES", column_default=None))
    migrations.require_explanation_policy_column(cursor, "validation-schema")


def test_incompatible_existing_column_is_not_silently_altered():
    cursor = SchemaCursor(column=dict(data_type="varchar", max_length=32,
                                      is_nullable="YES", column_default=None))
    store, _ = fake_store(cursor)
    with pytest.raises(migrations.SchemaMigrationRequired):
        migrations.migrate_explanation_policy_version(store)
    assert not any("ALTER TABLE" in statement for statement, _ in cursor.statements)
    assert "RELEASE_LOCK" in cursor.statements[-1][0]


@pytest.mark.parametrize("timeout", [-1, 21, 1.5, True, False])
def test_lock_timeout_stays_below_connection_read_timeout(timeout):
    cursor = SchemaCursor()
    _, connection = fake_store(cursor)
    with pytest.raises(ValueError):
        with migrations.schema_lock(connection, "validation-schema", timeout_seconds=timeout):
            pass
    assert cursor.statements == []


def test_lock_name_is_scoped_to_database_and_fits_mysql_limit():
    first = migrations.schema_lock_name("검증-" + "x" * 64)
    assert first == migrations.schema_lock_name("검증-" + "x" * 64)
    assert first != migrations.schema_lock_name("another-schema")
    assert len(first) <= 64


def test_cli_check_is_read_only(monkeypatch, capsys):
    cursor = SchemaCursor(column=compatible_column())
    store, _ = fake_store(cursor)
    monkeypatch.setattr(MySQLJobStore, "from_env", classmethod(lambda cls: store))
    assert migrations.main(["--check"]) == 0
    assert "compatible" in capsys.readouterr().out
    assert len(cursor.statements) == 1
    assert "information_schema.COLUMNS" in cursor.statements[0][0]


def test_cli_failure_never_prints_database_exception_or_settings(monkeypatch, capsys):
    sentinel = "synthetic-sensitive-exception-must-not-be-logged"
    monkeypatch.setattr(MySQLJobStore, "from_env", Mock(side_effect=RuntimeError(sentinel)))
    assert migrations.main([]) == 1
    output = capsys.readouterr()
    assert sentinel not in output.out + output.err
    assert "no analysis rows were rewritten" in output.err


@pytest.fixture
def isolated_schema():
    if os.getenv("RUN_MYSQL_TESTS") != "1":
        pytest.skip("Requires an explicitly configured disposable MySQL server")
    admin = MySQLJobStore.from_env()
    name = "explanation_migration_test_" + uuid4().hex
    # Every DDL target below is a newly owned UUID schema, never the configured DB.
    assert re.fullmatch(r"explanation_migration_test_[a-f0-9]{32}", name)
    with admin._connection() as connection, connection.cursor() as cursor:
        cursor.execute(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4")
    store = MySQLJobStore(**{**admin._settings, "database": name})
    try:
        yield store
    finally:
        assert re.fullmatch(r"explanation_migration_test_[a-f0-9]{32}", name)
        with admin._connection() as connection, connection.cursor() as cursor:
            cursor.execute(f"DROP DATABASE `{name}`")


def legacy_schema(store):
    with store._connection() as connection, connection.cursor() as cursor:
        cursor.execute("""CREATE TABLE ai_analysis_jobs (
            job_id VARCHAR(36) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
            status VARCHAR(16) NOT NULL, request_json JSON NOT NULL,
            result_json JSON NULL, error_code VARCHAR(255) NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
        cursor.execute("""CREATE TABLE ai_analysis_idempotency (
            key_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
            payload_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            job_id VARCHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL UNIQUE,
            metadata_json JSON NOT NULL, created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (job_id) REFERENCES ai_analysis_jobs(job_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")


def database_rows(store):
    with store._connection() as connection, connection.cursor() as cursor:
        cursor.execute("""SELECT job_id,status,request_json,result_json,error_code,created_at,updated_at
            FROM ai_analysis_jobs ORDER BY job_id""")
        jobs = cursor.fetchall()
        cursor.execute("SELECT * FROM ai_analysis_idempotency ORDER BY key_hash")
        return jobs, cursor.fetchall()


def test_live_mysql_legacy_upgrade_preserves_rows_nulls_idempotency_and_replay(isolated_schema):
    store = isolated_schema
    legacy_schema(store)
    request = {"platform": "test", "reviews": [{"content": "기존 결과 😃"}]}
    result = {"rti": -1, "level": None, "reasons": []}
    key_hash, payload_hash = request_hashes("isolated-old-done", request)
    old_versions = {"contract_version": "v0.5", "policy_version": "rti-v0.1"}
    with store._connection() as connection, connection.cursor() as cursor:
        for status in ("DONE", "RUNNING", "FAILED"):
            cursor.execute("""INSERT INTO ai_analysis_jobs
                (job_id,status,request_json,result_json,error_code) VALUES (%s,%s,%s,%s,%s)""",
                (status.lower(), status, json.dumps(request, ensure_ascii=False),
                 json.dumps(result) if status == "DONE" else None,
                 "OLD_FAILURE" if status == "FAILED" else None))
        cursor.execute("""INSERT INTO ai_analysis_idempotency
            (key_hash,payload_hash,job_id,metadata_json) VALUES (%s,%s,'done',%s)""",
            (key_hash, payload_hash, json.dumps(old_versions)))
    before = database_rows(store)
    with pytest.raises(migrations.SchemaMigrationRequired):
        store.initialize()
    assert database_rows(store) == before
    assert migrations.migrate_explanation_policy_version(store) == "added"
    assert database_rows(store) == before
    for job_id in ("done", "running", "failed"):
        assert store.get(job_id)["explanation_policy_version"] is None
    replay = store.claim_idempotent("isolated-old-done", request, {"policy_version": "future"})
    assert not replay.is_new and replay.versions == old_versions
    assert store.get(replay.job_id)["result"] == result
    assert store.get(replay.job_id)["explanation_policy_version"] is None

    new_id = store.create({"new": True})
    store.complete(new_id, result, explanation_policy_version="neutral-summary-v1")
    complete_before = database_rows(store)
    assert migrations.migrate_explanation_policy_version(store) == "already_applied"
    assert database_rows(store) == complete_before
    assert store.get(new_id)["explanation_policy_version"] == "neutral-summary-v1"
    assert store.get("done")["explanation_policy_version"] is None


def test_live_mysql_fresh_install_and_repeated_migration(isolated_schema):
    store = isolated_schema
    assert migrations.migrate_explanation_policy_version(store) == "created"
    assert migrations.migrate_explanation_policy_version(store) == "already_applied"
    store.initialize()
    job_id = store.create({"new": True})
    assert store.get(job_id)["explanation_policy_version"] is None


def test_live_mysql_concurrent_migrations_and_worker_initialization_are_serialized(isolated_schema):
    store = isolated_schema
    legacy_schema(store)
    barrier = Barrier(2)

    def migrate(_):
        barrier.wait(timeout=5)
        return migrations.migrate_explanation_policy_version(store)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(migrate, range(2)))
    assert sorted(results) == ["added", "already_applied"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: store.initialize(), range(2)))
    assert store.get(store.create({"two_workers": True}))["status"] == "RUNNING"


def test_live_mysql_fresh_two_worker_initialization_is_serialized(isolated_schema):
    store = isolated_schema
    barrier = Barrier(2)

    def initialize(_):
        barrier.wait(timeout=5)
        store.initialize()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(initialize, range(2)))
    assert migrations.migrate_explanation_policy_version(store) == "already_applied"
    job_id = store.create({"fresh_two_workers": True})
    assert store.get(job_id)["explanation_policy_version"] is None


def test_live_mysql_named_lock_blocks_other_worker_without_ddl(isolated_schema):
    store = isolated_schema
    legacy_schema(store)
    with store._connection() as connection:
        with migrations.schema_lock(connection, store._settings["database"]):
            with pytest.raises(migrations.SchemaLockUnavailable):
                migrations.migrate_explanation_policy_version(store, timeout_seconds=0)
    assert migrations.migrate_explanation_policy_version(store) == "added"
