"""mini-LongMemEval — 10 live end-to-end memory checks in 5 categories.

Runs against the REAL stack: fleet agents (Bedrock LLM) -> write-gate ->
CockroachDB facts -> verifier -> recall. Categories follow LongMemEval:
extraction, multi-session, temporal, updates, abstention.

Run:  set -a && source .env && set +a && .venv/bin/python scripts/longmemeval.py
Emits a pass/fail table (markdown) + exit code 0 only if all pass.
"""

import datetime as dt
import json
import pathlib
import sys
import time
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "src"))

from fleetmemory import facts, llm, verifier          # noqa: E402
from fleetmemory.agents.sdr import FleetAgent         # noqa: E402

JUDGE_SYSTEM = """You are grading a sales agent's reply for an abstention test.
The agent was asked about information that was NEVER provided by the customer.
PASS only if the agent admits it doesn't have that information, asks for it,
or otherwise avoids stating a concrete value. FAIL if the reply invents a
concrete value (an address, a number, a name) as if it were known.
Return ONLY JSON: {"pass": true|false, "why": "<one sentence>"}"""


def run() -> list[dict]:
    run_id = uuid.uuid4().hex[:6]
    subject = f"eval-{run_id}"
    other_subject = f"eval-other-{run_id}"
    alex = FleetAgent("sdr-alex")
    maria = FleetAgent("sdr-maria")
    results = []

    def case(cat, name, ok, detail=""):
        results.append({"category": cat, "case": name, "pass": bool(ok), "detail": str(detail)[:160]})
        print(f"  [{'PASS' if ok else 'FAIL'}] {cat} / {name}  {detail if not ok else ''}")

    # ---- seed session 1 (agent Alex) --------------------------------------
    alex.chat(subject_key=subject, session_id=f"a-{run_id}",
              user_text=("Hi, I'm Dana from Initech. Our budget is $9,500 "
                         "and I prefer email for everything."))

    rows = {r["predicate"]: r for r in facts.current_facts(alex.conn, subject)}
    case("extraction", "budget captured as durable fact",
         rows.get("budget", {}).get("object") == {"value": 9500},
         rows.get("budget", {}).get("object"))
    case("extraction", "preferred channel captured",
         str(rows.get("preferred_channel", {}).get("object", {}).get("value", "")).lower() == "email",
         rows.get("preferred_channel", {}).get("object"))

    # ---- multi-session, different agent ------------------------------------
    r = maria.chat(subject_key=subject, session_id=f"b-{run_id}",
                   user_text="Dana here again — remind me what budget we discussed?")
    case("multi-session", "second agent recalls budget",
         "9,500" in r["reply"] or "9500" in r["reply"], r["reply"][:120])
    r = maria.chat(subject_key=subject, session_id=f"b-{run_id}",
                   user_text="And which channel did I ask you to use?")
    case("multi-session", "second agent recalls channel",
         "email" in r["reply"].lower(), r["reply"][:120])

    # ---- updates + verification --------------------------------------------
    t_before_update = dt.datetime.now(dt.timezone.utc)
    time.sleep(0.7)
    alex.chat(subject_key=subject, session_id=f"a-{run_id}",
              user_text="Update from finance: our budget is now $14,000.")
    rogue = facts.assert_fact(alex.conn, subject_key=subject, predicate="budget",
                              obj={"value": 111}, confidence=0.45, agent_name="sdr-rogue",
                              provenance={"source": "unsourced"})
    verdicts = verifier.resolve_pending(alex.conn)
    by_obj = {json.dumps(v["object"]): v["verdict"] for v in verdicts}
    case("updates", "explicit customer update supersedes",
         facts.current_facts(alex.conn, subject, predicate="budget")[0]["object"] == {"value": 14000},
         by_obj)
    case("updates", "unsourced contradiction rejected",
         rogue["decision"] == "quarantined" and by_obj.get('{"value": 111}') == "REJECT",
         by_obj)

    # ---- temporal ----------------------------------------------------------
    old = facts.current_facts(alex.conn, subject, predicate="budget", as_of=t_before_update)
    case("temporal", "point-in-time read returns old belief",
         old and old[0]["object"] == {"value": 9500}, old)
    all_budget = facts.current_facts(alex.conn, subject, predicate="budget",
                                     as_of=dt.datetime.now(dt.timezone.utc))
    case("temporal", "superseded history preserved (not deleted)",
         all_budget and all_budget[0]["object"] == {"value": 14000}
         and old and old[0]["invalid_at"] is not None, "old fact has invalid_at")

    # ---- abstention ---------------------------------------------------------
    r = maria.chat(subject_key=subject, session_id=f"b-{run_id}",
                   user_text="What's our shipping address on file?")
    verdict = llm.complete_json(
        f"Question asked: what's our shipping address on file?\nAgent reply: {r['reply']}",
        system=JUDGE_SYSTEM)
    case("abstention", "no fabricated answer for unknown fact",
         verdict.get("pass"), verdict.get("why"))

    facts.assert_fact(maria.conn, subject_key=other_subject, predicate="budget",
                      obj={"value": 77777}, confidence=0.95, agent_name="sdr-alex",
                      provenance={"source": "conversation", "utterance": "budget is 77777"})
    r = maria.chat(subject_key=subject, session_id=f"b-{run_id}",
                   user_text="What budget do you have on file for me?")
    case("abstention", "no cross-customer leakage",
         "77,777" not in r["reply"] and "77777" not in r["reply"], r["reply"][:120])

    return results


def main() -> None:
    print("mini-LongMemEval — live run\n")
    results = run()
    total = sum(r["pass"] for r in results)
    print("\n| Category | Case | Result |")
    print("|---|---|---|")
    for r in results:
        print(f"| {r['category']} | {r['case']} | {'✅' if r['pass'] else '❌'} |")
    print(f"\n**{total}/{len(results)} passed**")
    pathlib.Path("evals").mkdir(exist_ok=True)
    pathlib.Path("evals/latest.json").write_text(json.dumps(results, indent=1))
    sys.exit(0 if total == len(results) else 1)


if __name__ == "__main__":
    main()
