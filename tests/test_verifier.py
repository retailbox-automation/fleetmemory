"""Verifier resolution paths with a stubbed judge (no LLM calls)."""

from fleetmemory import facts, verifier


def _quarantine_budget_update(conn, new_value=12000, confidence=0.9):
    facts.assert_fact(conn, subject_key="acme", predicate="budget",
                      obj={"value": 8000}, confidence=0.9, agent_name="sdr-1",
                      provenance={"source": "conversation", "utterance": "budget is 8000"})
    r = facts.assert_fact(conn, subject_key="acme", predicate="budget",
                          obj={"value": new_value}, confidence=confidence,
                          agent_name="sdr-2",
                          provenance={"source": "conversation",
                                      "utterance": f"budget is now {new_value}"})
    assert r["decision"] == "quarantined"
    return r


def test_supersede_applies_bitemporally(conn):
    _quarantine_budget_update(conn)
    out = verifier.resolve_pending(
        conn, judge=lambda ex, cand: {"verdict": "SUPERSEDE", "reason": "explicit update"})
    assert [o["verdict"] for o in out] == ["SUPERSEDE"]

    rows = facts.current_facts(conn, "acme", predicate="budget")
    assert len(rows) == 1
    assert rows[0]["object"] == {"value": 12000}

    journal = facts.gate_journal(conn)
    decisions = [j["decision"] for j in journal]
    assert "accepted_by_verifier" in decisions


def test_reject_keeps_memory_untouched(conn):
    _quarantine_budget_update(conn)
    out = verifier.resolve_pending(
        conn, judge=lambda ex, cand: {"verdict": "REJECT", "reason": "no evidence"})
    assert [o["verdict"] for o in out] == ["REJECT"]
    rows = facts.current_facts(conn, "acme", predicate="budget")
    assert rows[0]["object"] == {"value": 8000}


def test_judge_failure_fails_closed(conn):
    _quarantine_budget_update(conn)

    def broken_judge(ex, cand):
        raise KeyError("boom")

    out = verifier.resolve_pending(conn, judge=broken_judge)
    assert out[0]["verdict"] == "REJECT"
    assert facts.current_facts(conn, "acme", predicate="budget")[0]["object"] == {"value": 8000}


def test_resolved_rows_not_reprocessed(conn):
    _quarantine_budget_update(conn)
    verifier.resolve_pending(conn, judge=lambda ex, c: {"verdict": "REJECT", "reason": "x"})
    again = verifier.resolve_pending(conn, judge=lambda ex, c: {"verdict": "SUPERSEDE", "reason": "y"})
    assert again == []
