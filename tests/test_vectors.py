"""C-SPANN vector layer with deterministic stub embeddings (no Bedrock)."""

from fleetmemory import facts, vectors


def _stub_embed(text: str) -> list[float]:
    """Map known phrases to distinct 1024-d unit-ish vectors."""
    axes = {"pricing": 0, "shipping": 1, "onboarding": 2}
    v = [0.0] * 1024
    for word, axis in axes.items():
        if word in text.lower():
            v[axis] = 1.0
    if not any(v):
        v[3] = 1.0
    return v


def test_semantic_recall_scoped_to_subject(conn):
    facts.assert_fact(conn, subject_key="acme", predicate="stage",
                      obj={"value": "x"}, confidence=0.9)   # creates subject
    facts.assert_fact(conn, subject_key="other", predicate="stage",
                      obj={"value": "x"}, confidence=0.9)

    vectors.remember_note(conn, subject_key="acme",
                          content="asked about pricing tiers", embed_fn=_stub_embed)
    vectors.remember_note(conn, subject_key="acme",
                          content="complained about shipping delays", embed_fn=_stub_embed)
    vectors.remember_note(conn, subject_key="other",
                          content="pricing question from another customer", embed_fn=_stub_embed)

    hits = vectors.semantic_recall(conn, subject_key="acme",
                                   query="what did they say about pricing?",
                                   k=1, embed_fn=_stub_embed)
    assert len(hits) == 1
    assert hits[0]["content"] == "asked about pricing tiers"
    assert hits[0]["distance"] < 0.5

    # the other subject's note must never leak in
    all_hits = vectors.semantic_recall(conn, subject_key="acme",
                                       query="pricing", k=10, embed_fn=_stub_embed)
    assert all(h["content"] != "pricing question from another customer" for h in all_hits)


def test_recall_unknown_subject_empty(conn):
    assert vectors.semantic_recall(conn, subject_key="ghost", query="anything",
                                   k=3, embed_fn=_stub_embed) == []
