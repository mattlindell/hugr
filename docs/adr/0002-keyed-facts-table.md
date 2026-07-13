# ADR 0002 — Dedicated keyed `facts` table with a confirmed-only write path

**Status:** Accepted — 2026-07-12

## Context

The mission is to stop confabulation of stable atomic facts. Putting such facts behind vector
similarity search reintroduces the exact failure (fuzzy, non-deterministic recall). Facts need
**deterministic keyed lookup** and must be injected verbatim every turn — so a wrong value here is
worse than the original bug (now it's authoritative *and* wrong). Two questions had to be settled:
where facts live, and how a value is allowed to get in.

## Decision

**A dedicated `facts` table, not a `type:fact` tag convention on `memories`.** A fact is a
`key → value`, not free text; the `memories` table is built for fuzzy prose (embedding, cosine
dedup). A separate table lets the schema *enforce* the Fact Criterion structurally.

Shape:

```
facts(
  scope        TEXT    NOT NULL,          -- '*' = global; else a project key
  key          TEXT    NOT NULL,
  value        TEXT    NOT NULL,
  asserted_by  TEXT    NOT NULL,          -- 'user' | 'model'
  confirmed_at TIMESTAMP,                 -- NULL = Pending; NOT NULL = injectable
  created_at   TIMESTAMP DEFAULT NOW(),
  updated_at   TIMESTAMP DEFAULT NOW(),
  PRIMARY KEY (scope, key)                -- single-valued per (scope, key)
)
```

No embedding column, no `project` column (**Fact Scope** subsumes it). **0.5 injects global
(`scope = '*'`) only**; project scope is carried but not injected until Scope Identity is resolved.

**Write path (confirmed-only, two entries):**

1. **User assertion** → written live immediately, `asserted_by = user`, `confirmed_at = now()`.
2. **Model proposal** (distiller) → lands **Pending** (`confirmed_at IS NULL`); promotion to
   injectable requires explicit human confirmation in the Console.

**`confirmed_at IS NOT NULL` is the sole injection gate.**

**Conflict / update semantics:**

- Single-valued per `(scope, key)` — enforced by the PK.
- A **user assertion always wins** and overwrites in place.
- A **model proposal never overwrites a live fact** — it raises a Pending conflict at most.
- Trust order: **user > existing confirmed > model proposal.**

## Consequences

- The San Diego → Portland incident becomes structurally unrepeatable: no batch inference can
  silently mutate an injected fact.
- `fact_audit` history is **deferred** (not load-bearing for the fix; cheap to add later).
- The donor's fuzzy-prose tooling (cosine dedup, `check_memory`) does not apply to Facts by design.
