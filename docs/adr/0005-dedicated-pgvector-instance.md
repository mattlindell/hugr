# ADR 0005 — Dedicated pgvector instance, not a tenant on the shared production cluster

**Status:** Accepted — 2026-07-12
**Supersedes:** the handoff's "PostgreSQL 16 on the home server" / "use the existing Postgres."

## Context

The handoff assumed a fresh PostgreSQL 16. The user's actual home Postgres (`postdb1` on Legion,
`192.168.20.200`, behind pgbouncer) is **`postgres:14.6` and shared production** — it runs Home
Assistant, Grafana, Mealie, Paperless, and LiteLLM. hugr's workload is bursty and hostile to a
shared box: bulk history import, distillation write storms, and an HNSW index build that pins
CPU/IO. Installing the `pgvector` extension is a server-level change on the cluster that runs
household automation. The estate is also **GitOps / pull-based** — changes to it flow through the
home-server repo's Reconciler, never hand-applied.

(Note: pgbouncer is `pool_mode = session`, so the transaction-pooling prepared-statement hazard
does **not** apply.)

## Decision

Run hugr's store as a **dedicated pgvector instance**, isolated from `postdb1`:

- **0.5:** a `pgvector/pgvector:pg17` container **on the desktop** (localhost — no LAN hop for the
  DB, no GitOps dependency to stand it up). The extension is installed so the schema is ready;
  0.5 exercises only the `facts` table (no vectors yet).
- Zero blast radius on Home Assistant; current PG + pgvector + HNSW instead of being pinned to
  14.6; independent tuning and backup.

## Consequences

- One more service to run and back up — accepted for isolation from household-critical infra.
- **Future path:** the user plans to roll `postdb1` forward off 14.6 to a pgvector-capable version.
  Once that lands, the dedicated instance *may* be consolidated back as a tenant (or GitOps-promoted
  to Legion) if centralization is wanted. Not a 0.5/1.0 concern.
- Tailnet is dropped for now (the desktop doesn't travel), so no tailnet-interface binding is
  required; localhost binding suffices.
