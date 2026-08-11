import os
import re
import sys
import pathlib

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "src"))

from fleetmemory import db  # noqa: E402


def _test_url() -> str:
    base = os.environ.get("CRDB_URL")
    if not base:
        raise RuntimeError("CRDB_URL not set (source .env)")
    return re.sub(r"/defaultdb\b", "/fleetmem_test", base)


@pytest.fixture(scope="session", autouse=True)
def _env():
    os.environ["FLEETMEM_URL"] = _test_url()


@pytest.fixture(scope="session")
def schema_conn(_env):
    conn = db.connect()
    db.apply_schema(conn)
    yield conn
    conn.close()


@pytest.fixture()
def conn(schema_conn):
    with schema_conn.transaction():
        with schema_conn.cursor() as cur:
            for table in ("gate_decisions", "facts", "memory_embeddings", "subjects", "agents"):
                cur.execute(f"DELETE FROM {table}")
    yield schema_conn
