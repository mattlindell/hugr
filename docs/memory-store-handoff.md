# Handoff — Persistent memory store for Claude Code / Conclave

**Date:** 2026-07-12
**From:** planning conversation (chat)
**To:** Claude Code agent picking up the build
**Status:** Design locked; ready to implement. No code written yet.

---

## Objective

Build a reliable persistent memory store so the model stops confabulating stable user
facts. Triggering incident: Claude Code claimed the user lives in **San Diego** when he's
in the **Portland, OR metro**, despite `~/.claude/CLAUDE.md` stating the correct location.

## Root-cause framing

The failure is confabulation from parametric memory when grounding fails to fire — the
model sampled a high-prior US city and delivered it fluently instead of doing a lookup.
`CLAUDE.md` is passive/always-on: the fact is in context but not reliably *attended to*,
and it erodes under long sessions / auto-compaction.

Storage taxonomy to keep in mind (worst → best for stable atomic facts):

1. Parametric (in weights) — unreliable; this is where "San Diego" came from.
2. In-context passive (`CLAUDE.md`) — present but ignorable; compaction can drop it.
3. Retrieved-on-demand (MCP tool) — reliable *only if the model chooses to call it*.
4. Always-injected (hook) — removes the model's discretion. Correct tier for stable facts.

## Core design decisions (with rationale)

1. **Route by fact type.** Do NOT put atomic stable facts (location, name, employer,
   dog's name) behind similarity search — vector recall is fuzzy/non-deterministic and
   reintroduces the exact failure. Stable facts → deterministic keyed lookup, injected
   verbatim every turn. Vector store → episodic/fuzzy recall only (past decisions +
   rationale, "have we discussed X before," session summaries, code context by meaning).

2. **Deterministic injection via a `UserPromptSubmit` hook.** This is the layer that
   actually fixes the bug — it re-injects every turn, surviving attention decay and
   compaction, and removes the model's discretion.
   - Phrase injected facts as plain statements, not system-style commands. Imperative /
     "ALWAYS remember…" phrasing can trip prompt-injection defenses and get surfaced to
     the user instead of absorbed.
   - **Fail open.** Short timeout (~2s, well under the ~30s hook ceiling); on any error,
     inject nothing and let the turn proceed. A memory store that occasionally forgets is
     fine; one that freezes the editor is not.
   - **KNOWN BUG:** `UserPromptSubmit` `additionalContext` reportedly not delivered in the
     VS Code extension on Windows (GH issue #49063, open as of Apr 2026). Works in CLI.
     Dev runs a Windows fleet — test in the raw CLI first before debugging a "working"
     hook that's silently dropped downstream.

3. **Storage: PostgreSQL 16 + pgvector on the home server.** Use an **HNSW** index (not
   IVFFlat) — better recall/latency, no training step. Cosine distance (`<=>`). Match
   embedding dimension to the model; consider `halfvec` only if dims are large enough to
   approach pgvector's index dimension ceiling. At personal scale this is nowhere near
   needing a dedicated vector DB — Postgres is the right call.

4. **Embeddings: general-purpose TEXT model for prose memories** (e.g. `all-mpnet-base-v2`,
   768-dim). Do NOT reuse the jina *code* embeddings — those stay with Vera; code-tuned
   embeddings underperform on natural-language memories. *Open decision:* run embeddings
   in-container (simpler) vs route through the Jetson/LiteLLM endpoint (centralizes GPU).

5. **Verification ("think before you speak").** A generic self-check is weak — the verifier
   confabulates on the same priors. The useful version is **grounded** verification: diff
   each factual claim against an authoritative source. No clean seam for this in stock
   Claude Code; it belongs in the Conclave loop as a critic subagent, gated to responses
   that make factual claims, checking against the fact store (Vera retrieves ground truth).
   Budget for ~1 extra model pass; gate it so it doesn't run every turn.

## Chosen base to fork

**Repo:** https://github.com/daringanitch/claude-memory
Postgres 16 + pgvector + FastMCP, 768-dim `all-mpnet-base-v2`, 18 MCP tools, Ollama
distillation. Treat as an **organ donor, not a dependency** (single author, low adoption,
active through Apr 2026, 76 mocked tests). Audit before trusting.

**Reuse ~as-is:**
- FastMCP server + tool surface (this is the "memory service" component, already written).
- `hybrid_search` — keyword + semantic fusion, already built.
- `extract_signals.py` — LLM-free capture of correction-signal preferences ("don't do
  that", "actually no") plus tool/command/file usage patterns. The sleeper hit.
- Write hygiene — `content_hash` dedup, ≥0.92 cosine auto-dedup, `check_memory`
  ADD/UPDATE/NOOP dry-run guard, soft deletes, `/health`.

**Swap:**
- IVFFlat → HNSW in `init.sql`.
- Deployment: docker-compose@localhost + macOS LaunchAgent → containers on the home server
  reached over the tailnet + a systemd timer. Translate the macOS-isms (`brew`,
  `launchctl`, `host.docker.internal`).
- Point distillation at the Jetson inference node if centralizing (note: a general 7B
  distills prose better than the Coder variant used for Conclave).

**Add (the delta the repo lacks):**
- (a) `UserPromptSubmit` hook doing deterministic per-turn injection, fail-open.
- (b) A keyed `facts` table (or a `type:fact` convention pulled by key, not ANN) for the
  location-class stable facts.
- (c) Provenance tagging on the write path.

## Risks / gotchas

- **Windows `additionalContext` bug #49063** — test in CLI first.
- **Hook timeout can stall the session** — fail open, short timeout.
- **Repo security defaults** — default Postgres password + unauthenticated SSE on `:3333`,
  localhost-bound. Over the tailnet: bind to the tailnet interface (not `0.0.0.0`), rotate
  creds, lean on tailnet ACLs. Never expose `5432`/`3333` to the LAN or wider.
- **Write-path confabulation** — dedup/guards stop *duplicates*, not *false facts*; a 7B
  distiller can invent "durable knowledge." Tolerable for advisory episodic memories,
  dangerous for anything promoted into the deterministic `facts` table (injected verbatim
  every turn). Require provenance (user-stated vs model-distilled) and gate promotion
  behind confirmation.

## Next steps (ordered)

1. Fork the repo; stand up Postgres 16 + pgvector + FastMCP on the home server; bind to the
   tailnet, rotate creds.
2. Migration: IVFFlat → HNSW; add a keyed `facts` table for deterministic exact lookup
   alongside `memories`.
3. Write the fail-open `UserPromptSubmit` hook: query `facts` (deterministic) + top-k
   `memories` (ANN over-fetch → rerank on Jetson), inject as plain statements; ~2s timeout,
   fail open. Test in raw CLI first.
4. Add provenance: `source` / `asserted_by` columns; tag user-stated vs model-distilled;
   gate promotion into `facts` behind confirmation.
5. Decide embeddings + distillation placement (in-container vs Jetson/LiteLLM) and wire it.
6. Import existing `~/.claude/projects` history; run `extract_signals`; smoke-test with the
   original failure — "where does the user live?" must deterministically return Portland.
7. (Conclave) Add the grounded critic subagent for factual-claim responses.

## Open questions

- Embeddings in-container vs via LiteLLM/Jetson?
- Distillation model: general 7B vs the existing Coder variant?
- `facts` table vs tag-convention for the deterministic lookup — schema choice.
- Conclave critic in v1, or defer?
