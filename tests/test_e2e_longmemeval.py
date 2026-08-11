"""E2E: the full mini-LongMemEval suite against the live stack.

Costs real LLM calls (~20 Bedrock invocations) — opt in explicitly:
  FLEETMEM_E2E=1 .venv/bin/python -m pytest tests/test_e2e_longmemeval.py -v
"""

import os
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "scripts"))

pytestmark = pytest.mark.skipif(
    os.environ.get("FLEETMEM_E2E") != "1",
    reason="live e2e (Bedrock + CRDB) — set FLEETMEM_E2E=1 to run",
)


def test_longmemeval_all_pass(_env):
    import longmemeval

    results = longmemeval.run()
    failed = [r for r in results if not r["pass"]]
    assert not failed, f"failed cases: {failed}"
    assert len(results) == 10


def test_managed_mcp_reads_memory(_env):
    if not os.environ.get("CRDB_MCP_API_KEY"):
        pytest.skip("no MCP key")
    from mcp_memory_inspector import ManagedMCP

    mcp = ManagedMCP(os.environ["CRDB_MCP_API_KEY"], os.environ["CRDB_CLUSTER_ID"])
    assert "select_query" in mcp.list_tools()
    rows = mcp.query("SELECT count(*) AS n FROM gate_decisions")
    assert rows and rows[0]["n"] >= 0
