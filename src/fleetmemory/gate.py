"""Write-gate: no fact reaches memory without passing it.

Every attempted write is evaluated BEFORE commit and every decision is
journaled to gate_decisions — accepted, rejected, quarantined, or
accepted_superseding. Deterministic checks live here; LLM verifier
subagents plug in on top for contradiction resolution.

Gate placement rule: the gate sits at the very start of the write path,
before any branching — a writer may grow a second call-site later.
"""

import json
from dataclasses import dataclass, field

MIN_CONFIDENCE = 0.3
SUPERSEDE_MARGIN = 0.05
MAX_OBJECT_BYTES = 8192

ACCEPTED = "accepted"
ACCEPTED_SUPERSEDING = "accepted_superseding"
REJECTED = "rejected"
QUARANTINED = "quarantined"


@dataclass
class GateResult:
    decision: str
    reason: str
    checks: dict = field(default_factory=dict)
    supersedes: list = field(default_factory=list)  # fact ids to invalidate

    @property
    def accepted(self) -> bool:
        return self.decision in (ACCEPTED, ACCEPTED_SUPERSEDING)


def evaluate(cur, *, subject_id, predicate, obj, confidence) -> GateResult:
    """Deterministic gate checks against the current active facts."""
    checks = {}

    if not predicate or not predicate.strip():
        checks["junk"] = "empty predicate"
        return GateResult(REJECTED, "junk_predicate", checks)
    if obj is None or obj == "" or obj == {}:
        checks["junk"] = "empty object"
        return GateResult(REJECTED, "junk_object", checks)
    payload = json.dumps(obj, ensure_ascii=False)
    if len(payload.encode()) > MAX_OBJECT_BYTES:
        checks["size"] = f"{len(payload.encode())}b > {MAX_OBJECT_BYTES}b"
        return GateResult(REJECTED, "oversized_object", checks)
    checks["junk"] = "ok"

    if confidence < MIN_CONFIDENCE:
        checks["confidence"] = f"{confidence} < {MIN_CONFIDENCE}"
        return GateResult(REJECTED, "low_confidence", checks)
    checks["confidence"] = "ok"

    cur.execute(
        """SELECT id, object, confidence FROM facts
           WHERE subject_id = %s AND predicate = %s AND invalid_at IS NULL""",
        (subject_id, predicate),
    )
    active = cur.fetchall()

    duplicates = [r for r in active if r["object"] == obj]
    if duplicates:
        checks["duplicate"] = str(duplicates[0]["id"])
        return GateResult(REJECTED, "duplicate", checks)
    checks["duplicate"] = "none"

    conflicting = [r for r in active if r["object"] != obj]
    if conflicting:
        strongest = max(r["confidence"] for r in conflicting)
        if confidence >= strongest + SUPERSEDE_MARGIN:
            checks["contradiction"] = (
                f"supersedes {len(conflicting)} fact(s), "
                f"new {confidence} >= old {strongest} + {SUPERSEDE_MARGIN}"
            )
            return GateResult(
                ACCEPTED_SUPERSEDING,
                "contradiction_resolved_by_confidence",
                checks,
                supersedes=[r["id"] for r in conflicting],
            )
        checks["contradiction"] = (
            f"conflicts with {len(conflicting)} active fact(s), "
            f"new {confidence} < old {strongest} + {SUPERSEDE_MARGIN}"
        )
        return GateResult(QUARANTINED, "contradiction_needs_verifier", checks)
    checks["contradiction"] = "none"

    return GateResult(ACCEPTED, "clean", checks)


def journal(cur, *, result: GateResult, agent_id, fact_payload, fact_id=None) -> None:
    cur.execute(
        """INSERT INTO gate_decisions (agent_id, decision, reason, fact_payload, checks, fact_id)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        (
            agent_id,
            result.decision,
            result.reason,
            json.dumps(fact_payload, ensure_ascii=False, default=str),
            json.dumps(result.checks, ensure_ascii=False, default=str),
            fact_id,
        ),
    )
