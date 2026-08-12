"""Verifier — resolves quarantined writes with an adversarial LLM judge.

A quarantined fact is never silently lost: its full payload sits in the
gate journal (gate_decisions). The verifier picks up unresolved quarantines,
weighs the candidate's PROVENANCE against the active conflicting facts, and
returns a verdict:

  SUPERSEDE — the candidate is a genuine update/correction (e.g. the customer
              explicitly stated the new value): commit it, bi-temporally
              invalidate the old fact(s).
  REJECT    — insufficient evidence (weak/no provenance, likely hallucination):
              memory stays untouched.

The judge's default is REJECT — a fact must EARN its way into memory.
Every verdict is written back onto the journal row (resolution, resolver,
resolution_reason), so the whole lifecycle of a write is auditable.
"""

import json

from . import db, gate, llm

VERDICT_SYSTEM = """You are a memory verifier for a fleet of sales agents.
A new fact was QUARANTINED because it contradicts existing memory. Decide:

- "SUPERSEDE": ONLY if the candidate's provenance contains strong, explicit
  evidence — e.g. a direct customer utterance clearly stating the new value
  as an update or correction ("our budget is now X", "we switched to Y").
- "REJECT": in every other case. No utterance, vague wording, secondhand or
  unsourced values, agent guesses — all REJECT. When uncertain, REJECT.

Return ONLY JSON: {"verdict": "SUPERSEDE"|"REJECT", "reason": "<one sentence>"}"""


def _default_judge(existing: list, candidate: dict) -> dict:
    prompt = json.dumps(
        {"existing_facts": existing, "quarantined_candidate": candidate},
        ensure_ascii=False, default=str,
    )
    return llm.complete_json(prompt, system=VERDICT_SYSTEM)


def resolve_pending(conn, judge=None, limit: int = 20) -> list[dict]:
    """Resolve unresolved quarantined writes. Returns applied verdicts."""
    judge = judge or _default_judge

    def fetch(cur):
        cur.execute(
            """SELECT id, fact_payload FROM gate_decisions
               WHERE decision = %s AND resolution IS NULL
               ORDER BY ts LIMIT %s""",
            (gate.QUARANTINED, limit),
        )
        return cur.fetchall()

    pending = db.run_txn(conn, fetch)
    outcomes = []
    for row in pending:
        payload = row["fact_payload"]
        outcomes.append(_resolve_one(conn, judge, row["id"], payload))
    return outcomes


def _resolve_one(conn, judge, decision_id, payload) -> dict:
    subject_key, predicate = payload["subject"], payload["predicate"]

    def fetch_existing(cur):
        cur.execute(
            """SELECT f.id, f.object, f.confidence, f.valid_at, f.provenance,
                      a.name AS source_agent
               FROM facts f
               JOIN subjects s ON s.id = f.subject_id
               LEFT JOIN agents a ON a.id = f.source_agent_id
               WHERE s.external_key = %s AND f.predicate = %s
                 AND f.invalid_at IS NULL""",
            (subject_key, predicate),
        )
        return cur.fetchall()

    existing = db.run_txn(conn, fetch_existing)
    if not existing:
        # conflict evaporated (facts were invalidated meanwhile) — nothing to
        # judge against; candidate would pass a fresh gate run instead.
        verdict = {"verdict": "REJECT", "reason": "no active conflicting fact remains; re-assert instead"}
    else:
        try:
            verdict = judge(existing, payload)
        except Exception as e:  # any judge failure (incl. Bedrock/boto3) fails THIS row closed
            verdict = {"verdict": "REJECT", "reason": f"judge failed ({type(e).__name__}); fail closed"}
    approved = verdict.get("verdict") == "SUPERSEDE"

    def apply(cur):
        if approved:
            cur.execute(
                """INSERT INTO facts (subject_id, predicate, object, confidence,
                                      source_agent_id, provenance)
                   SELECT s.id, %s, %s, %s, a.id, %s
                   FROM subjects s
                   LEFT JOIN agents a ON a.name = %s
                   WHERE s.external_key = %s
                   RETURNING id""",
                (predicate, json.dumps(payload["object"], ensure_ascii=False),
                 payload.get("confidence", 0.5),
                 json.dumps(payload.get("provenance", {}), ensure_ascii=False),
                 payload.get("agent"), subject_key),
            )
            new_id = cur.fetchone()["id"]
            for old in existing:
                cur.execute(
                    """UPDATE facts SET invalid_at = now(), invalidated_by = %s
                       WHERE id = %s AND invalid_at IS NULL""",
                    (new_id, old["id"]),
                )
            gate.journal(
                cur,
                result=gate.GateResult("accepted_by_verifier", verdict.get("reason", ""),
                                       {"quarantine_decision_id": str(decision_id)}),
                agent_id=None, fact_payload=payload, fact_id=new_id,
            )
        cur.execute(
            """UPDATE gate_decisions
               SET resolution = %s, resolved_at = now(), resolver = %s,
                   resolution_reason = %s
               WHERE id = %s""",
            ("superseded" if approved else "rejected",
             "llm-verifier", verdict.get("reason", ""), decision_id),
        )

    db.run_txn(conn, apply)
    return {"decision_id": str(decision_id), "subject": subject_key,
            "predicate": predicate, "object": payload["object"],
            "verdict": verdict.get("verdict"), "reason": verdict.get("reason")}
