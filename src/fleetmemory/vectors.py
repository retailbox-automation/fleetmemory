"""Semantic (episodic) memory — C-SPANN distributed vector index in CockroachDB.

Raw conversation moments are embedded (Bedrock Titan v2, 1024-d, normalized)
and stored per subject. The VECTOR INDEX is prefixed by subject_id, so nearest-
neighbour search is scoped to one customer. C-SPANN is in preview: vectors go
in row-by-row (no bulk IMPORT), similarity is Euclidean (`<->`).
"""

import json
import os

import boto3

from . import db

_rt = None


def _runtime():
    global _rt
    if _rt is None:
        _rt = boto3.client(
            "bedrock-runtime",
            region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        )
    return _rt


def embed(text: str) -> list[float]:
    resp = _runtime().invoke_model(
        modelId="amazon.titan-embed-text-v2:0",
        body=json.dumps({"inputText": text[:8000], "dimensions": 1024, "normalize": True}),
    )
    return json.loads(resp["body"].read())["embedding"]


def _vec_literal(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def remember_note(conn, *, subject_key: str, content: str, kind: str = "utterance",
                  embed_fn=None) -> str:
    vec = (embed_fn or embed)(content)

    def txn(cur):
        cur.execute(
            """INSERT INTO memory_embeddings (subject_id, kind, content, embedding)
               SELECT id, %s, %s, %s::vector FROM subjects WHERE external_key = %s
               RETURNING id""",
            (kind, content, _vec_literal(vec), subject_key),
        )
        row = cur.fetchone()
        return str(row["id"]) if row else None

    return db.run_txn(conn, txn)


def semantic_recall(conn, *, subject_key: str, query: str, k: int = 3,
                    embed_fn=None) -> list[dict]:
    """Nearest conversation moments for this subject (C-SPANN ANN search)."""
    vec = (embed_fn or embed)(query)

    def txn(cur):
        cur.execute("SELECT id FROM subjects WHERE external_key = %s", (subject_key,))
        row = cur.fetchone()
        if not row:
            return []
        # equality on the index's prefix column + ORDER BY distance = the
        # C-SPANN ANN access path
        cur.execute(
            """SELECT content, kind, created_at,
                      embedding <-> %s::vector AS distance
               FROM memory_embeddings
               WHERE subject_id = %s
               ORDER BY embedding <-> %s::vector
               LIMIT %s""",
            (_vec_literal(vec), row["id"], _vec_literal(vec), k),
        )
        return cur.fetchall()

    return db.run_txn(conn, txn)
