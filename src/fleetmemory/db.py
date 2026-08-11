"""Database access for FleetMemory (CockroachDB).

CockroachDB runs SERIALIZABLE by default: any transaction may abort with
SQLSTATE 40001 and MUST be retried client-side. All writes go through
run_txn(), which owns that retry loop.
"""

import os
import pathlib
import random
import time

import psycopg
from psycopg.rows import dict_row

MAX_RETRIES = 6
BASE_BACKOFF_S = 0.1

SCHEMA_PATH = pathlib.Path(__file__).parent / "schema.sql"


def dsn() -> str:
    url = os.environ.get("FLEETMEM_URL") or os.environ.get("CRDB_URL")
    if not url:
        raise RuntimeError("Set FLEETMEM_URL or CRDB_URL")
    return url


def connect() -> psycopg.Connection:
    return psycopg.connect(dsn(), row_factory=dict_row)


def run_txn(conn: psycopg.Connection, fn):
    """Run fn(cur) in a transaction, retrying serialization failures (40001)."""
    for attempt in range(MAX_RETRIES):
        try:
            with conn.transaction():
                with conn.cursor() as cur:
                    return fn(cur)
        except psycopg.errors.SerializationFailure:
            if attempt == MAX_RETRIES - 1:
                raise
            time.sleep(BASE_BACKOFF_S * (2 ** attempt) + random.uniform(0, 0.05))


def apply_schema(conn: psycopg.Connection) -> None:
    ddl = SCHEMA_PATH.read_text()
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(ddl)
