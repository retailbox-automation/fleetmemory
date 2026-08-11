"""Live smoke: AgentCoreMemorySaver (short-term layer) persists LangGraph
state across separate invocations of the same thread.

Needs AWS creds + AGENTCORE_MEMORY_ID in the environment (see .env).
Run: .venv/bin/python scripts/smoke_agentcore.py
Verified 2026-08-11: session 2 resumed with session 1's state.
"""

import operator
import os
import uuid
from typing import Annotated, TypedDict

from langgraph.graph import StateGraph, START, END
from langgraph_checkpoint_aws import AgentCoreMemorySaver


class S(TypedDict):
    notes: Annotated[list, operator.add]


def add_note(state: S) -> S:
    return {"notes": [f"note-{len(state['notes']) + 1}"]}


def main() -> None:
    saver = AgentCoreMemorySaver(
        memory_id=os.environ["AGENTCORE_MEMORY_ID"],
        region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
    )
    g = StateGraph(S)
    g.add_node("add_note", add_note)
    g.add_edge(START, "add_note")
    g.add_edge("add_note", END)
    app = g.compile(checkpointer=saver)

    cfg = {"configurable": {"thread_id": f"smoke_{uuid.uuid4().hex[:8]}",
                            "actor_id": "agent-smoke"}}
    r1 = app.invoke({"notes": []}, cfg)
    r2 = app.invoke({"notes": []}, cfg)  # separate invocation, same thread
    assert r2["notes"] == ["note-1", "note-2"], r2["notes"]
    print("SMOKE OK — state persisted across invocations:", r2["notes"])


if __name__ == "__main__":
    main()
