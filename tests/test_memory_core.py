"""E2E tests of the FleetMemory core against a live CockroachDB cluster."""

import datetime as dt
import time

from fleetmemory import facts


def prov(text):
    return {"source": "conversation", "utterance": text}


def test_assert_and_read_back(conn):
    r = facts.assert_fact(
        conn, subject_key="acme", predicate="preferred_channel",
        obj={"value": "email"}, confidence=0.9, agent_name="sdr-1",
        provenance=prov("we prefer email"),
    )
    assert r["decision"] == "accepted"
    assert r["fact_id"] is not None

    rows = facts.current_facts(conn, "acme")
    assert len(rows) == 1
    assert rows[0]["object"] == {"value": "email"}
    assert rows[0]["source_agent"] == "sdr-1"


def test_duplicate_rejected(conn):
    facts.assert_fact(conn, subject_key="acme", predicate="budget",
                      obj={"value": 5000}, confidence=0.9, agent_name="sdr-1",
                      provenance=prov("budget is 5000"))
    r = facts.assert_fact(conn, subject_key="acme", predicate="budget",
                          obj={"value": 5000}, confidence=0.9, agent_name="sdr-2")
    assert r["decision"] == "rejected"
    assert r["reason"] == "duplicate"
    assert len(facts.current_facts(conn, "acme")) == 1


def test_contradiction_higher_confidence_supersedes(conn):
    r1 = facts.assert_fact(conn, subject_key="acme", predicate="budget",
                           obj={"value": 5000}, confidence=0.6, agent_name="sdr-1",
                           provenance=prov("budget around 5000"))
    r2 = facts.assert_fact(conn, subject_key="acme", predicate="budget",
                           obj={"value": 8000}, confidence=0.95, agent_name="sdr-2",
                           provenance=prov("budget is 8000, confirmed"))
    assert r2["decision"] == "accepted_superseding"
    assert r1["fact_id"] in r2["superseded"]

    rows = facts.current_facts(conn, "acme", predicate="budget")
    assert len(rows) == 1
    assert rows[0]["object"] == {"value": 8000}


def test_contradiction_lower_confidence_quarantined(conn):
    facts.assert_fact(conn, subject_key="acme", predicate="budget",
                      obj={"value": 5000}, confidence=0.9, agent_name="sdr-1",
                      provenance=prov("budget is 5000"))
    r = facts.assert_fact(conn, subject_key="acme", predicate="budget",
                          obj={"value": 100}, confidence=0.4, agent_name="rogue")
    assert r["decision"] == "quarantined"
    assert r["reason"] == "contradiction_needs_verifier"
    rows = facts.current_facts(conn, "acme", predicate="budget")
    assert rows[0]["object"] == {"value": 5000}  # memory unchanged


def test_low_confidence_rejected(conn):
    r = facts.assert_fact(conn, subject_key="acme", predicate="mood",
                          obj={"value": "happy"}, confidence=0.1, agent_name="sdr-1")
    assert r["decision"] == "rejected"
    assert r["reason"] == "low_confidence"


def test_point_in_time_read(conn):
    facts.assert_fact(conn, subject_key="acme", predicate="stage",
                      obj={"value": "discovery"}, confidence=0.6, agent_name="sdr-1",
                      provenance=prov("we are in discovery"))
    # valid_at is set by the SERVER clock — pad both sides so client/server
    # clock skew can't put t_between outside the [fact1, fact2] window
    time.sleep(0.7)
    t_between = dt.datetime.now(dt.timezone.utc)
    time.sleep(0.7)
    facts.assert_fact(conn, subject_key="acme", predicate="stage",
                      obj={"value": "negotiation"}, confidence=0.95, agent_name="sdr-2",
                      provenance=prov("moving to negotiation"))

    now_rows = facts.current_facts(conn, "acme", predicate="stage")
    assert now_rows[0]["object"] == {"value": "negotiation"}

    then_rows = facts.current_facts(conn, "acme", predicate="stage", as_of=t_between)
    assert then_rows[0]["object"] == {"value": "discovery"}


def test_invalidate_correction(conn):
    r = facts.assert_fact(conn, subject_key="acme", predicate="poc_contact",
                          obj={"value": "Jane"}, confidence=0.9, agent_name="sdr-1",
                          provenance=prov("your contact is Jane"))
    assert facts.invalidate_fact(conn, r["fact_id"], reason="client_corrected")
    assert facts.current_facts(conn, "acme", predicate="poc_contact") == []
    # double-invalidate is a no-op
    assert not facts.invalidate_fact(conn, r["fact_id"])


def test_every_decision_journaled(conn):
    facts.assert_fact(conn, subject_key="acme", predicate="budget",
                      obj={"value": 5000}, confidence=0.9, agent_name="sdr-1",
                      provenance=prov("budget is 5000"))
    facts.assert_fact(conn, subject_key="acme", predicate="budget",
                      obj={"value": 5000}, confidence=0.9, agent_name="sdr-2")  # duplicate
    facts.assert_fact(conn, subject_key="acme", predicate="budget",
                      obj={"value": 1}, confidence=0.4, agent_name="rogue")  # quarantine

    journal = facts.gate_journal(conn)
    decisions = [j["decision"] for j in journal]
    assert "accepted" in decisions
    assert "rejected" in decisions
    assert "quarantined" in decisions
    assert len(journal) == 3


def test_unsourced_novel_quarantined(conn):
    """A brand-new claim with no provenance must not sail into memory."""
    r = facts.assert_fact(conn, subject_key="acme", predicate="decision_maker",
                          obj={"value": "John"}, confidence=0.9, agent_name="rogue")
    assert r["decision"] == "quarantined"
    assert r["reason"] == "needs_provenance"
    assert facts.current_facts(conn, "acme", predicate="decision_maker") == []


def test_unsourced_high_confidence_cannot_supersede(conn):
    """Claimed confidence alone never overwrites a sourced fact."""
    facts.assert_fact(conn, subject_key="acme", predicate="budget",
                      obj={"value": 5000}, confidence=0.6, agent_name="sdr-1",
                      provenance=prov("budget is 5000"))
    r = facts.assert_fact(conn, subject_key="acme", predicate="budget",
                          obj={"value": 9000}, confidence=0.99, agent_name="rogue")
    assert r["decision"] == "quarantined"
    assert facts.current_facts(conn, "acme", predicate="budget")[0]["object"] == {"value": 5000}
