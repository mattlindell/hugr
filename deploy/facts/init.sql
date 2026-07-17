-- hugr facts store — schema initialization (PV-1)
--
-- Runs once, on first boot of the dedicated pgvector/pgvector:pg17 container
-- (mounted into /docker-entrypoint-initdb.d). Defines the keyed `facts` table
-- from ADR 0002 and installs the pgvector extension so the schema is ready for
-- the episodic `memories` tier in a later milestone. Release 0.5 exercises only
-- `facts` — no vector columns or indexes yet.

-- Ready the extension now (ADR 0005): installing it is a server-level change we
-- do on this dedicated instance so the shared prod cluster never has to.
CREATE EXTENSION IF NOT EXISTS vector;

-- The Fact tier (ADR 0002): deterministic keyed lookup, injected verbatim every
-- turn. A dedicated table — not a `type:fact` tag on `memories` — so the schema
-- can enforce the Fact shape structurally: one value per (scope, key), a
-- constrained provenance domain, and a nullable confirmation gate.
CREATE TABLE facts (
  scope        TEXT      NOT NULL,                 -- '*' = global; else a project key (Fact Scope)
  key          TEXT      NOT NULL,
  value        TEXT      NOT NULL,
  asserted_by  TEXT      NOT NULL
                 CHECK (asserted_by IN ('user', 'model')),  -- Provenance domain
  confirmed_at TIMESTAMP,                          -- NULL = Pending; NOT NULL = injectable (sole injection gate)
  created_at   TIMESTAMP NOT NULL DEFAULT NOW(),
  updated_at   TIMESTAMP NOT NULL DEFAULT NOW(),
  PRIMARY KEY (scope, key)                         -- single-valued per (scope, key)
);

-- No embedding column (Facts are not fuzzy-searched) and no `project` column
-- (Fact Scope subsumes it — global '*' vs. a project key).

-- Keep updated_at honest on every mutation, mirroring the donor idiom.
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER set_facts_updated_at
BEFORE UPDATE ON facts
FOR EACH ROW EXECUTE FUNCTION update_updated_at();
