"""Bi-temporal fact store — the long-term memory API of FleetMemory.

Facts are asserted through the write-gate, corrections invalidate (never
delete), and reads are point-in-time: current_facts(as_of=...) reproduces
what the fleet believed at any past moment.
"""

import json

from . import db, gate


def ensure_agent(cur, name: str, role: str = "agent"):
    cur.execute(
        """INSERT INTO agents (name, role) VALUES (%s, %s)
           ON CONFLICT (name) DO UPDATE SET role = excluded.role
           RETURNING id""",
        (name, role),
    )
    return cur.fetchone()["id"]


def ensure_subject(cur, external_key: str, display_name: str = "", kind: str = "prospect"):
    cur.execute(
        """INSERT INTO subjects (external_key, display_name, kind) VALUES (%s, %s, %s)
           ON CONFLICT (external_key) DO UPDATE SET display_name =
             CASE WHEN excluded.display_name != '' THEN excluded.display_name
                  ELSE subjects.display_name END
           RETURNING id""",
        (external_key, display_name or external_key, kind),
    )
    return cur.fetchone()["id"]


def assert_fact(conn, *, subject_key: str, predicate: str, obj, confidence: float = 0.9,
                agent_name: str | None = None, provenance: dict | None = None) -> dict:
    """Gate → journal → (maybe) commit. Returns {decision, reason, fact_id, superseded}."""

    def txn(cur):
        subject_id = ensure_subject(cur, subject_key)
        agent_id = ensure_agent(cur, agent_name) if agent_name else None

        result = gate.evaluate(
            cur, subject_id=subject_id, predicate=predicate, obj=obj, confidence=confidence
        )
        payload = {"subject": subject_key, "predicate": predicate,
                   "object": obj, "confidence": confidence,
                   "agent": agent_name, "provenance": provenance or {}}

        fact_id = None
        if result.accepted:
            cur.execute(
                """INSERT INTO facts (subject_id, predicate, object, confidence,
                                      source_agent_id, provenance)
                   VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                (subject_id, predicate, json.dumps(obj, ensure_ascii=False),
                 confidence, agent_id, json.dumps(provenance or {}, ensure_ascii=False)),
            )
            fact_id = cur.fetchone()["id"]
            for old_id in result.supersedes:
                cur.execute(
                    """UPDATE facts SET invalid_at = now(), invalidated_by = %s
                       WHERE id = %s AND invalid_at IS NULL""",
                    (fact_id, old_id),
                )

        gate.journal(cur, result=result, agent_id=agent_id,
                     fact_payload=payload, fact_id=fact_id)
        return {"decision": result.decision, "reason": result.reason,
                "fact_id": fact_id, "superseded": result.supersedes}

    return db.run_txn(conn, txn)


def invalidate_fact(conn, fact_id, reason: str = "correction") -> bool:
    """Bi-temporal correction: the fact stops being true, history stays."""

    def txn(cur):
        cur.execute(
            "UPDATE facts SET invalid_at = now() WHERE id = %s AND invalid_at IS NULL",
            (fact_id,),
        )
        invalidated = cur.rowcount == 1
        if invalidated:
            gate.journal(
                cur,
                result=gate.GateResult("invalidated", reason, {"fact_id": str(fact_id)}),
                agent_id=None,
                fact_payload={"fact_id": str(fact_id)},
                fact_id=fact_id,
            )
        return invalidated

    return db.run_txn(conn, txn)


def current_facts(conn, subject_key: str, predicate: str | None = None, as_of=None) -> list:
    """Active facts now, or the world as the fleet believed it at as_of."""

    def txn(cur):
        q = """SELECT f.id, f.predicate, f.object, f.confidence, f.valid_at,
                      f.invalid_at, f.invalidated_by, a.name AS source_agent
               FROM facts f
               JOIN subjects s ON s.id = f.subject_id
               LEFT JOIN agents a ON a.id = f.source_agent_id
               WHERE s.external_key = %s"""
        params: list = [subject_key]
        if predicate:
            q += " AND f.predicate = %s"
            params.append(predicate)
        if as_of is not None:
            q += " AND f.valid_at <= %s AND (f.invalid_at IS NULL OR f.invalid_at > %s)"
            params.extend([as_of, as_of])
        else:
            q += " AND f.invalid_at IS NULL"
        q += " ORDER BY f.valid_at"
        cur.execute(q, params)
        return cur.fetchall()

    return db.run_txn(conn, txn)


def gate_journal(conn, limit: int = 50) -> list:
    def txn(cur):
        cur.execute(
            "SELECT ts, decision, reason, fact_payload, checks FROM gate_decisions ORDER BY ts DESC LIMIT %s",
            (limit,),
        )
        return cur.fetchall()

    return db.run_txn(conn, txn)
