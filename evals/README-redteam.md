# Adversarial red-team eval (`scripts/redteam.py`)

Fires 30 hallucinated/malicious writes and 10 legitimate customer-driven
updates through the **real production write path**
(`fleetmemory.facts.assert_fact` -> `fleetmemory.gate.evaluate` ->
`fleetmemory.verifier.resolve_pending`, the same code the live agents/demo
use) and reports whether each candidate's value actually made it into active
memory. No gate/verifier logic is reimplemented in the test.

## Run it

```bash
set -a && source .env && set +a
.venv/bin/python scripts/redteam.py
```

Runs live against the cloud CockroachDB cluster and makes real AWS Bedrock
calls for every quarantined item (~19 in a typical run) — a few cents, not
free, not expensive. Writes `evals/redteam.json` (full detail, incl. every
prompt/verdict) and prints a human summary.

## Isolation

Everything runs under `redteam-eval-<8-hex-run-id>` — a subject key that
never collides with a real demo subject (`acme-corp`, `globex`, etc.) and is
unique per run, so reruns never step on each other or need cleanup.

One caveat, surfaced by the script itself (not swept under the rug): the
verifier's `resolve_pending()` drains the **entire shared** `gate_decisions`
quarantine queue by design (a real verifier subagent is a fleet-wide
background worker, not scoped to one prospect — that's the correct
production behavior). If something else quarantines a write concurrently
(e.g. someone clicking the live demo's "😈 hallucinate a fact" button while
this script is running), this script's verifier pass will also resolve that
foreign item and the script prints a `*** WARNING ***` naming it, plus
records `foreign_subject_quarantines_touched` in the JSON. It never *writes*
to a foreign subject — it only completes an already-pending resolution with
the same real judge that would have handled it anyway.

## Reading the result

- `headline.adversarial_blocked` — every attack, all 5 categories.
- `headline.core_defense_blocked` — attacks that actually engage the gate's
  contradiction/junk/size/confidence-floor defenses (excludes
  `unsourced_novel`, see below). This is the number that answers "does the
  gate+verifier stop a hallucination it has any signal to catch."
- `headline.legitimate_accepted` / `legitimate_lost` — did real customer
  corrections survive.
- `known_limitation` — the `unsourced_novel` category (brand-new predicate,
  nothing existing to conflict with) is **not** part of what the
  deterministic gate or the verifier can catch: the verifier only ever
  reviews *quarantined* (contradicted) writes, and an unsupported claim with
  no prior fact to contradict has nothing to trip the wire. This is reported
  separately and explicitly, not blended into the headline number.
- `by_category` — per-category totals.
- `final_active_facts` vs `expected_active_facts` — an independent
  ground-truth check by re-reading current memory after the run, not just
  trusting the per-write decision labels.

## What the last live run found (2026-08-14, run `d148b846`)

- **24/24 core-defense** attacks blocked (junk, oversized, low-confidence,
  and unsourced contradictions of seeded facts all correctly rejected or
  quarantined-then-REJECTed).
- **6/6 `unsourced_novel`** claims were accepted — expected, documented scope
  boundary above, not a verifier miss.
- **9/10 legitimate** customer-confirmed updates correctly SUPERSEDEd
  in via the verifier. The 1 "lost" case is a genuine, interesting finding,
  not a tuning artifact: the legitimate-update chain in this run round-trips
  a value back to its original ("Slack" -> "email" -> "phone" -> "Slack").
  Because all writes in this script fire before any verifier resolution runs
  (mirroring real async production timing), the gate's synchronous
  duplicate-check saw the *still-unresolved* original "Slack" fact as
  current at write time and rejected the final "back to Slack" write as a
  literal duplicate, before the intervening "email"/"phone" updates had been
  verified away. It's an honest eventual-consistency edge case of
  write-before-verify batching, not a defect this script papered over.
- Gate write latency (`facts.assert_fact`, includes the deterministic
  checks + CockroachDB round trip, no LLM call) — **p50 ≈ 293ms, p95 ≈
  453ms** against the managed cloud cluster from a laptop.

Full per-item detail, every judge prompt/verdict, and the complete decision
trail live in `evals/redteam.json`.
