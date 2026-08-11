"""FleetAgent — an SDR agent wired to the two-layer memory.

Short-term (conversation state): AWS AgentCore Memory via AgentCoreMemorySaver.
Long-term (durable facts): CockroachDB through the write-gate — every fact the
agent hears is extracted, gated, journaled, and only then committed.

Any agent in the fleet recalls what any other agent learned: memory is shared
at the subject level, not per-agent.
"""

import json
import operator
import os
from typing import Annotated, TypedDict

from langgraph.graph import StateGraph, START, END
from langgraph_checkpoint_aws import AgentCoreMemorySaver

from .. import db, facts, llm, vectors

EXTRACT_SYSTEM = """You extract durable CRM facts from a customer's message.
Return ONLY a JSON array (no prose). Each element:
{"predicate": "<snake_case>", "object": {"value": <string|number>}, "confidence": <0.0-1.0>}

Prefer these predicates when applicable: budget, preferred_channel, poc_contact,
stage, timeline, objection, company_size, product_interest, preference.
Extract only facts stated by the customer that are durable (worth remembering
next week). Confidence: 0.9+ explicit statement, 0.6-0.8 implied, below 0.5 a guess.
Empty array if nothing durable was stated."""

RESPOND_SYSTEM = """You are {name}, a sales development representative at FleetCommerce.
Be warm, concise (2-4 sentences), and concretely helpful.

Durable memory about this customer (shared across the whole fleet — use it,
reference it naturally when relevant):
{memory_block}"""


class ChatState(TypedDict):
    messages: Annotated[list, operator.add]   # [{"role", "text"}]
    subject_key: str
    memory_block: str
    reply: str
    gate_results: list


class FleetAgent:
    def __init__(self, name: str, memory_id: str | None = None):
        self.name = name
        self.conn = db.connect()
        saver = AgentCoreMemorySaver(
            memory_id=memory_id or os.environ["AGENTCORE_MEMORY_ID"],
            region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        )
        g = StateGraph(ChatState)
        g.add_node("recall", self._recall)
        g.add_node("respond", self._respond)
        g.add_node("memorize", self._memorize)
        g.add_edge(START, "recall")
        g.add_edge("recall", "respond")
        g.add_edge("respond", "memorize")
        g.add_edge("memorize", END)
        self.app = g.compile(checkpointer=saver)

    # -- nodes ------------------------------------------------------------
    def _recall(self, state: ChatState) -> dict:
        rows = facts.current_facts(self.conn, state["subject_key"])
        block = []
        if rows:
            block.append("Durable facts:")
            block += [
                f"- {r['predicate']} = {json.dumps(r['object'], ensure_ascii=False)}"
                f" (confidence {r['confidence']}, learned by {r['source_agent'] or 'unknown'}"
                f" at {r['valid_at']:%Y-%m-%d})"
                for r in rows
            ]
        last_user = next(
            (m["text"] for m in reversed(state["messages"]) if m["role"] == "user"), "")
        if last_user:
            try:
                moments = vectors.semantic_recall(
                    self.conn, subject_key=state["subject_key"], query=last_user, k=3)
            except Exception:
                moments = []  # vector layer must never break the conversation
            if moments:
                block.append("Conversation moments recalled semantically:")
                block += [
                    f'- "{m["content"][:200]}" ({m["created_at"]:%Y-%m-%d}, distance {m["distance"]:.2f})'
                    for m in moments
                ]
        if not block:
            return {"memory_block": "(no durable facts yet — first contact)"}
        return {"memory_block": "\n".join(block)}

    def _respond(self, state: ChatState) -> dict:
        convo = "\n".join(f"{m['role']}: {m['text']}" for m in state["messages"])
        reply = llm.complete(
            convo,
            system=RESPOND_SYSTEM.format(name=self.name, memory_block=state["memory_block"]),
        )
        return {"reply": reply, "messages": [{"role": "assistant", "text": reply}]}

    def _memorize(self, state: ChatState) -> dict:
        last_user = next(
            (m["text"] for m in reversed(state["messages"]) if m["role"] == "user"), ""
        )
        if last_user:
            try:
                vectors.remember_note(
                    self.conn, subject_key=state["subject_key"], content=last_user)
            except Exception:
                pass  # episodic layer is best-effort
        try:
            extracted = llm.complete_json(last_user, system=EXTRACT_SYSTEM)
        except (json.JSONDecodeError, KeyError):
            extracted = []
        results = []
        for f in extracted if isinstance(extracted, list) else []:
            r = facts.assert_fact(
                self.conn,
                subject_key=state["subject_key"],
                predicate=str(f.get("predicate", "")),
                obj=f.get("object"),
                confidence=float(f.get("confidence", 0.5)),
                agent_name=self.name,
                provenance={"source": "conversation", "utterance": last_user[:500]},
            )
            results.append({"predicate": f.get("predicate"), **r})
        return {"gate_results": results}

    # -- public -----------------------------------------------------------
    def chat(self, *, subject_key: str, user_text: str, session_id: str) -> dict:
        cfg = {"configurable": {"thread_id": session_id, "actor_id": self.name}}
        out = self.app.invoke(
            {
                "messages": [{"role": "user", "text": user_text}],
                "subject_key": subject_key,
                "memory_block": "",
                "reply": "",
                "gate_results": [],
            },
            cfg,
        )
        return {"reply": out["reply"], "gate_results": out["gate_results"],
                "memory_block": out["memory_block"]}

    def history(self, *, session_id: str) -> list:
        cfg = {"configurable": {"thread_id": session_id, "actor_id": self.name}}
        snap = self.app.get_state(cfg)
        return (snap.values or {}).get("messages", [])
