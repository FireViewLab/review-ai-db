"""Explicit additive v2 MySQL migration; never edit or delete v1 tables."""

import argparse

from app.repositories.mysql_jobs import JOB_TABLES, MySQLJobStore


def check_v2_schema(store):
    jobs, keys = JOB_TABLES["v2"]
    with store._connection() as db, db.cursor() as cursor:
        cursor.execute(f"SELECT job_id,status,request_json,result_json,error_code,explanation_policy_version,"
                       f"created_at,updated_at FROM {jobs} LIMIT 0")
        cursor.execute(f"SELECT i.key_hash,i.payload_hash,i.metadata_json,j.status FROM {keys} i "
                       f"JOIN {jobs} j ON j.job_id=i.job_id LIMIT 0")


def migrate_v2_schema(store):
    if (store._jobs, store._keys) != JOB_TABLES["v2"]:
        raise ValueError("Not a v2 store")
    store.initialize()
    check_v2_schema(store)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true", help="Read-only compatibility check")
    action.add_argument("--apply", action="store_true", help="Create only the two v2 tables")
    args = parser.parse_args()
    try:
        store = MySQLJobStore.from_env(scope="v2")
        migrate_v2_schema(store) if args.apply else check_v2_schema(store)
        print("V2 MySQL schema compatible")
        return 0
    except Exception:
        print("V2 MySQL schema check/apply failed; inspect permissions, table structure and DB connectivity")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
