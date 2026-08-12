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
                   FROM gate_decisions
                   WHERE fact_payload->>'subject' = %s
                   ORDER BY ts DESC LIMIT 40""",
                (subject,),
            )
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


class RogueIn(BaseModel):
    subject: str


@app.post("/api/rogue")
def inject_rogue(body: RogueIn):
    """Demo control: a rogue agent asserts a plausible-but-unsourced value.

    Goes through the exact same gate as every other write — the point of the
    demo is that the gate quarantines it and the verifier rejects it.
    """
    with _lock:
        c = conn()
        active = facts.current_facts(c, body.subject)
        if not active:
            return {"error": "no facts yet — teach an agent something first"}
        # prefer a numeric fact (budget-style) for a punchy contradiction
        target = next(
            (f for f in active
             if isinstance(f["object"], dict)
             and any(isinstance(v, (int, float)) for v in f["object"].values())),
            active[0],
        )
        obj = dict(target["object"]) if isinstance(target["object"], dict) else {"value": target["object"]}
        mutated = False
        for k, v in obj.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                obj[k] = max(1, int(v) // 10)
                mutated = True
                break
        if not mutated:
            k = next(iter(obj))
            obj[k] = f"{obj[k]} (misremembered)"
        confidence = max(0.3, float(target["confidence"]) - 0.1)
        r = facts.assert_fact(
            c, subject_key=body.subject, predicate=target["predicate"],
            obj=obj, confidence=confidence, agent_name="sdr-rogue",
            provenance={"source": "unsourced",
                        "note": "no customer utterance on file — the agent 'remembered' this value"},
        )
    return {"injected": {"predicate": target["predicate"], "object": obj,
                         "confidence": confidence}, **r}
