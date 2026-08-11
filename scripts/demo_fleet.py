"""Live demo arc — the 4 beats of the video, end to end.

1. Agent ALEX talks to a prospect; facts land in CockroachDB through the gate.
2. A "week later", agent MARIA (different agent, different session) REMEMBERS.
3. A rogue low-confidence write contradicting memory is QUARANTINED by the gate.
4. The customer changes their mind -> bi-temporal supersede (history preserved).

Run: set -a && source .env && set +a && .venv/bin/python scripts/demo_fleet.py
"""

import pathlib
import sys
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "src"))

from fleetmemory import facts, verifier
from fleetmemory.agents.sdr import FleetAgent

RUN = uuid.uuid4().hex[:6]
SUBJECT = f"demo-acme-{RUN}"
BAR = "=" * 70


def main() -> None:
    alex = FleetAgent("sdr-alex")
    maria = FleetAgent("sdr-maria")

    print(BAR, "\nBEAT 1 — Alex meets the prospect\n", BAR)
    r1 = alex.chat(
        subject_key=SUBJECT,
        user_text=("Hi, this is Jane from Acme. We're looking at your platform — "
                   "our budget is around $8,000. And please use email, I hate calls."),
        session_id=f"alex-{RUN}",
    )
    print("memory before:", r1["memory_block"])
    print("reply:", r1["reply"])
    for g in r1["gate_results"]:
        print("  gate:", g["predicate"], "->", g["decision"], f"({g['reason']})")

    print(BAR, "\nBEAT 2 — a week later, Maria picks up the thread\n", BAR)
    r2 = maria.chat(
        subject_key=SUBJECT,
        user_text="Hi, Jane from Acme again. Remind me — what did we discuss about pricing?",
        session_id=f"maria-{RUN}",
    )
    print("memory recalled by maria:")
    print(r2["memory_block"])
    print("reply:", r2["reply"])

    print(BAR, "\nBEAT 3 — rogue write: hallucinated price, low confidence\n", BAR)
    rogue = facts.assert_fact(
        alex.conn, subject_key=SUBJECT, predicate="budget",
        obj={"value": 100}, confidence=0.4, agent_name="sdr-rogue",
        provenance={"source": "hallucination-demo"},
    )
    print("rogue attempt ->", rogue["decision"], f"({rogue['reason']})")
    assert rogue["decision"] == "quarantined"

    print(BAR, "\nBEAT 4 — customer changes their mind (bi-temporal supersede)\n", BAR)
    r4 = alex.chat(
        subject_key=SUBJECT,
        user_text="Good news — finance approved more. Our budget is now $12,000.",
        session_id=f"alex-{RUN}",
    )
    for g in r4["gate_results"]:
        print("  gate:", g["predicate"], "->", g["decision"], f"({g['reason']})")
    print("current budget (still guarded):",
          [c["object"] for c in facts.current_facts(alex.conn, SUBJECT, predicate="budget")])

    print(BAR, "\nBEAT 5 — verifier resolves the quarantine\n", BAR)
    verdicts = verifier.resolve_pending(alex.conn)
    for v in verdicts:
        print(f"  {v['predicate']} {v['object']} -> {v['verdict']}: {v['reason']}")
    current = facts.current_facts(alex.conn, SUBJECT, predicate="budget")
    print("current budget after verifier:", [c["object"] for c in current])
    assert current[0]["object"] == {"value": 12000}, "legit update must supersede"

    print("\ngate journal (newest first):")
    for j in facts.gate_journal(alex.conn, limit=12):
        print(f"  {j['ts']:%H:%M:%S} {j['decision']:22} {j['reason']}")

    print(BAR, "\nDEMO ARC OK\n", BAR)


if __name__ == "__main__":
    main()
