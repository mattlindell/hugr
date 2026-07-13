# ADR 0003 — Always-injected Facts via a fail-open `UserPromptSubmit` hook reading a snapshot

**Status:** Accepted — 2026-07-12

## Context

`CLAUDE.md` is passive/always-on context: present but not reliably attended to, and it erodes under
long sessions and auto-compaction. That is the root cause of the confabulation. The fix is to move
stable Facts to a tier that removes the model's discretion — an always-injected hook.

The gating risk was whether `UserPromptSubmit` `additionalContext` survives the user's runtime.
The handoff flagged bug #49063 (dropped in the VS Code extension on Windows). The user runs **only
the raw CLI and Zed ACP** — no VS Code. **Injection over Zed ACP is proven working** (verified live
in-session), so the risk is retired.

The app runs on the desktop; Postgres is across the LAN. A live per-turn Fact query would cross the
LAN every turn and couple the hot path to DB availability and driver import cost.

## Decision

The **Injection Hook** is a `UserPromptSubmit` hook that:

- Injects Confirmed Facts as **plain statements** (never imperative "ALWAYS remember…", which can
  trip prompt-injection defenses).
- Injects **Facts only** (episodic recall stays retrieved-on-demand via the CLI); **global only**
  in 0.5.
- Is **fail-open**: ~2s timeout; on any error/missing input, inject nothing and proceed.
- Reads a pre-rendered **Snapshot** file on the desktop — **stdlib-only reader**, no DB, no
  embeddings, no LAN round-trip.

**Cold-start Invariant:** the hook's hot path never imports `torch`, `sentence-transformers`, or
`psycopg2`.

**Snapshot invalidation:**

- **Event-triggered** regeneration on every Fact mutation (primary — a Fact asserted via the CLI is
  live on the very next turn).
- **TTL age-check** on non-hot-path core actions (search/save/list/startup) as a resilience
  backstop for out-of-band changes (restore, manual `psql` edit, migration). **Never runs in the
  hook.**

## Consequences

- Re-injection every turn survives attention decay and compaction — the actual bug fix.
- A slightly stale Snapshot is acceptable (Facts are near-immutable), consistent with fail-open.
- Facts-only keeps the injected payload tiny and 100% high-signal, which is *why* it survives
  attention decay; episodic prose would be noise competing for attention.
- Project-scoped injection (later) needs either per-scope snapshots or a small structured file the
  hook filters by cwd — an extension, not a rewrite.
