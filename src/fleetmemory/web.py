"""FleetMemory demo web app — FastAPI over the memory core.

Run: .venv/bin/uvicorn fleetmemory.web:app --app-dir src --port 8300
"""

import pathlib
import threading

from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import db, facts, verifier
from .agents.sdr import FleetAgent

WEB_DIR = pathlib.Path(__file__).parent.parent.parent / "web"

app = FastAPI(title="FleetMemory")

_lock = threading.Lock()
_agents: dict[str, FleetAgent] = {}
_conn = None


def conn():
    global _conn
    if _conn is None or _conn.closed:
        _conn = db.connect()
    return _conn


def agent(name: str) -> FleetAgent:
    if name not in _agents:
        _agents[name] = FleetAgent(name)
    return _agents[name]


class ChatIn(BaseModel):
    agent: str
    subject: str
    text: str


@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html")


@app.get("/api/state")
def state(subject: str):
    with _lock:
        c = conn()

        def txn(cur):
            cur.execute(
                """SELECT f.id, f.predicate, f.object, f.confidence, f.valid_at,
                          f.invalid_at, f.invalidated_by, a.name AS agent
                   FROM facts f
                   JOIN subjects s ON s.id = f.subject_id
                   LEFT JOIN agents a ON a.id = f.source_agent_id
                   WHERE s.external_key = %s ORDER BY f.valid_at""",
                (subject,),
            )
            rows = cur.fetchall()
            cur.execute(
                """SELECT ts, decision, reason, fact_payload, resolution,
                          resolution_reason
                   FROM gate_decisions ORDER BY ts DESC LIMIT 40""")
            journal = cur.fetchall()
            return rows, journal

        rows, journal = db.run_txn(c, txn)
    return {
        "facts": [
            {
                "id": str(r["id"]), "predicate": r["predicate"], "object": r["object"],
                "confidence": r["confidence"], "agent": r["agent"],
                "valid_at": r["valid_at"].isoformat(),
                "invalid_at": r["invalid_at"].isoformat() if r["invalid_at"] else None,
            }
            for r in rows
        ],
        "journal": [
            {
                "ts": j["ts"].isoformat(), "decision": j["decision"],
                "reason": j["reason"], "payload": j["fact_payload"],
                "resolution": j["resolution"],
                "resolution_reason": j["resolution_reason"],
            }
            for j in journal
        ],
        "pending": sum(1 for j in journal
                       if j["decision"] == "quarantined" and not j["resolution"]),
    }


@app.get("/api/history")
def history(agent_name: str, subject: str):
    with _lock:
        msgs = agent(agent_name).history(session_id=f"web-{agent_name}-{subject}")
    return {"messages": msgs}


@app.post("/api/chat")
def chat(body: ChatIn):
    with _lock:
        out = agent(body.agent).chat(
            subject_key=body.subject, user_text=body.text,
            session_id=f"web-{body.agent}-{body.subject}",
        )
    return out


@app.post("/api/verifier")
def run_verifier():
    with _lock:
        verdicts = verifier.resolve_pending(conn())
    return {"verdicts": verdicts}
