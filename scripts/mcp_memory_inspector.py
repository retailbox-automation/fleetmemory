"""Memory inspector over the CockroachDB Cloud MANAGED MCP SERVER.

Instead of a direct SQL connection, this client speaks MCP (streamable HTTP)
to https://cockroachlabs.cloud/mcp — the same server any AI tool (Claude Code,
Cursor, agents) can use to introspect the fleet's memory: schemas, gate
decisions, fact history. Auth: service-account API key (headless).

Run:
  set -a && source .env && set +a && .venv/bin/python scripts/mcp_memory_inspector.py
"""

import json
import os
import urllib.request

ENDPOINT = "https://cockroachlabs.cloud/mcp"


class ManagedMCP:
    def __init__(self, api_key: str, cluster_id: str):
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "mcp-cluster-id": cluster_id,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        self._id = 0
        self.session = None
        self._rpc("initialize", {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "fleetmemory-inspector", "version": "0.1"},
        })
        self._notify("notifications/initialized")

    def _post(self, payload: dict):
        headers = dict(self.headers)
        if self.session:
            headers["mcp-session-id"] = self.session
        req = urllib.request.Request(ENDPOINT, data=json.dumps(payload).encode(),
                                     headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=60) as resp:
            if resp.headers.get("mcp-session-id"):
                self.session = resp.headers["mcp-session-id"]
            body = resp.read().decode()
        for line in body.splitlines():  # SSE frames
            if line.startswith("data: "):
                return json.loads(line[6:])
        return json.loads(body) if body.strip() else None

    def _rpc(self, method: str, params: dict):
        self._id += 1
        out = self._post({"jsonrpc": "2.0", "id": self._id,
                          "method": method, "params": params})
        if out and "error" in out:
            raise RuntimeError(out["error"])
        return out["result"] if out else None

    def _notify(self, method: str):
        self._post({"jsonrpc": "2.0", "method": method})

    def list_tools(self) -> list[str]:
        return [t["name"] for t in self._rpc("tools/list", {})["tools"]]

    def query(self, sql: str, database: str = "fleetmem") -> list[dict]:
        r = self._rpc("tools/call", {"name": "select_query",
                                     "arguments": {"database": database, "query": sql}})
        text = "".join(c.get("text", "") for c in r.get("content", []))
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return [{"raw": text}]
        return parsed.get("rows", parsed) if isinstance(parsed, dict) else parsed


def main() -> None:
    mcp = ManagedMCP(os.environ["CRDB_MCP_API_KEY"], os.environ["CRDB_CLUSTER_ID"])
    print("tools:", ", ".join(mcp.list_tools()), "\n")

    print("== Gate decisions (via Managed MCP) ==")
    for row in mcp.query(
        "SELECT decision, count(*) AS n FROM gate_decisions GROUP BY decision ORDER BY n DESC"):
        print(f"  {row}")

    print("\n== Current beliefs about acme-corp ==")
    for row in mcp.query(
        """SELECT f.predicate, f.object, f.confidence
           FROM facts f JOIN subjects s ON s.id = f.subject_id
           WHERE s.external_key = 'acme-corp' AND f.invalid_at IS NULL"""):
        print(f"  {row}")


if __name__ == "__main__":
    main()
