-- FleetMemory schema v1 — CockroachDB (serverless, AWS us-east-1)
-- Conventions: UUID PKs via gen_random_uuid() (no sequences — hotspot-safe),
-- bi-temporal facts (invalid_at, never DELETE), every write journaled by the gate.

CREATE TABLE IF NOT EXISTS agents (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name STRING NOT NULL UNIQUE,
  role STRING NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS subjects (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  external_key STRING NOT NULL UNIQUE,
  display_name STRING NOT NULL,
  kind STRING NOT NULL DEFAULT 'prospect',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Long-term structured memory. A fact is never deleted: a correction sets
-- invalid_at + invalidated_by, preserving full history for point-in-time reads.
CREATE TABLE IF NOT EXISTS facts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  subject_id UUID NOT NULL REFERENCES subjects(id),
  predicate STRING NOT NULL,
  object JSONB NOT NULL,
  confidence FLOAT8 NOT NULL DEFAULT 1.0,
  source_agent_id UUID REFERENCES agents(id),
  provenance JSONB NOT NULL DEFAULT '{}',
  valid_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  invalid_at TIMESTAMPTZ,
  invalidated_by UUID,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS facts_active_idx
  ON facts (subject_id, predicate) WHERE invalid_at IS NULL;

-- Gate journal: every attempted write lands here with the decision and the
-- per-check results — accepted, rejected, quarantined, or superseding.
CREATE TABLE IF NOT EXISTS gate_decisions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  ts TIMESTAMPTZ NOT NULL DEFAULT now(),
  agent_id UUID,
  decision STRING NOT NULL,
  reason STRING NOT NULL,
  fact_payload JSONB NOT NULL,
  checks JSONB NOT NULL DEFAULT '{}',
  fact_id UUID
);

CREATE INDEX IF NOT EXISTS gate_decisions_ts_idx ON gate_decisions (ts DESC);

-- Verifier resolution of quarantined writes (verdict written back to the row).
ALTER TABLE gate_decisions ADD COLUMN IF NOT EXISTS resolution STRING;
ALTER TABLE gate_decisions ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMPTZ;
ALTER TABLE gate_decisions ADD COLUMN IF NOT EXISTS resolver STRING;
ALTER TABLE gate_decisions ADD COLUMN IF NOT EXISTS resolution_reason STRING;

-- Semantic memory: C-SPANN vector index (preview) — prefix column first so
-- similarity search is scoped per subject; vectors inserted row-by-row.
CREATE TABLE IF NOT EXISTS memory_embeddings (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  subject_id UUID NOT NULL,
  kind STRING NOT NULL DEFAULT 'note',
  content STRING NOT NULL,
  embedding VECTOR(1024) NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  VECTOR INDEX (subject_id, embedding)
);
