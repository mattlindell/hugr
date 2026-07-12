# CLAUDE.md

Guidance for Claude Code (claude.ai/code) when working in this repository.

## Read this first

**hugr** is a fresh venture that *starts* from a fork of
[`daringanitch/claude-memory`](https://github.com/daringanitch/claude-memory) — but it is
**not** that project and we will diverge heavily. Treat the inherited code as an **organ
donor, not a dependency**: reuse the parts that fit, replace the ones that don't, and don't
feel bound by the donor's design.

> The single source of truth for *where this project is going* is
> **`docs/memory-store-handoff.md`**. Read it before planning any work. This file summarizes
> it; the handoff has the full rationale.

**Current status (as of the fork):** design is locked, but **no divergence code has been
written yet**. Everything currently in the tree is the donor's implementation. Do not mistake
the donor's polished scripts and docs for hugr's spec — they describe the base we are cutting
apart.

The name **hugr** is the Old Norse word for the mind — thought, willpower, personality, and
the seat of emotions. It's deliberately not "yet another memory-X."

## The mission

Build a persistent memory store that stops the model from **confabulating stable user facts**.
The triggering incident: Claude Code confidently claimed the user lives in San Diego when he's
in the Portland, OR metro — even though `~/.claude/CLAUDE.md` stated the correct location.

The root cause is that `CLAUDE.md` is passive/always-on context: present, but not reliably
*attended to*, and it erodes under long sessions and auto-compaction. The fix is to move stable
facts to a tier that removes the model's discretion.

Storage taxonomy (worst → best for stable atomic facts):

1. **Parametric** (in weights) — unreliable; this is where "San Diego" came from.
2. **In-context passive** (`CLAUDE.md`) — present but ignorable; compaction can drop it.
3. **Retrieved-on-demand** (MCP tool) — reliable *only if the model chooses to call it*.
4. **Always-injected** (hook) — removes discretion. The correct tier for stable facts.

### Design thesis (the decisions that matter)

- **Route by fact type.** Atomic stable facts (location, name, employer) must **not** sit
  behind similarity search — fuzzy ANN recall reintroduces the exact failure. Stable facts →
  deterministic keyed lookup, injected verbatim every turn. The vector store is for
  **episodic/fuzzy** recall only (past decisions + rationale, "have we discussed X," session
  summaries, code context by meaning).
- **Deterministic injection via a `UserPromptSubmit` hook.** This is the layer that actually
  fixes the bug: it re-injects every turn, surviving attention decay and compaction. Phrase
  injected facts as plain statements (not imperative "ALWAYS remember…", which can trip
  prompt-injection defenses). **Fail open** — short timeout (~2s), and on any error inject
  nothing and let the turn proceed. A memory store that occasionally forgets is fine; one that
  freezes the editor is not.
  - ⚠️ **Known bug #49063:** `UserPromptSubmit` `additionalContext` is reportedly dropped by
    the VS Code extension on Windows (open as of Apr 2026). Works in the raw CLI. The dev runs
    a Windows fleet — **test the hook in the raw `claude` CLI first** before debugging a hook
    that's silently dropped downstream.
- **Provenance is mandatory on the write path.** Distillation by a small local LLM can invent
  "durable knowledge." That's tolerable for advisory episodic memories but dangerous for
  anything promoted into the deterministic fact store (injected verbatim every turn). Track
  user-stated vs. model-distilled, and gate promotion into the fact tier behind confirmation.

## Where hugr diverges from the inherited base

| | Plan |
|---|---|
| **Reuse ~as-is** | FastMCP server + tool surface (`mcp-server/server.py`); `hybrid_search`; `extract_signals.py` (LLM-free correction/preference capture — the sleeper hit); write hygiene (`content_hash` dedup, cosine auto-dedup, `check_memory` dry-run guard, soft deletes, `/health`) |
| **Swap** | IVFFlat → **HNSW** in `init.sql` (better recall/latency, no training step; cosine `<=>`). Deployment: docker-compose@localhost + **macOS LaunchAgent** → containers on the **home server over the tailnet** + a **systemd timer**. Translate the macOS-isms (`brew`, `launchctl`, `host.docker.internal`) |
| **Add** | (a) the fail-open `UserPromptSubmit` injection hook; (b) a keyed **`facts` table** (or a `type:fact` convention pulled by key, not ANN) for stable location-class facts; (c) provenance columns (`source` / `asserted_by`) on the write path |

### Next steps (ordered — from the handoff)

1. Stand up Postgres 16 + pgvector + FastMCP on the home server; bind to the **tailnet
   interface** (not `0.0.0.0`), rotate the default creds.
2. Migrate IVFFlat → HNSW; add a keyed `facts` table alongside `memories`.
3. Write the fail-open `UserPromptSubmit` hook (query `facts` deterministically + top-k
   `memories`, inject as plain statements, ~2s timeout, fail open). **Test in raw CLI first.**
4. Add provenance; gate promotion into `facts` behind confirmation.
5. Decide embeddings + distillation placement (in-container vs. Jetson/LiteLLM).
6. Import existing history, run `extract_signals`, and smoke-test the original failure:
   "where does the user live?" must deterministically return Portland.
7. (Conclave, later) grounded critic subagent for factual-claim responses.

### Open questions (unresolved — don't assume)

- Embeddings in-container vs. via LiteLLM/Jetson?
- Distillation model: general 7B vs. a Coder variant? (A general 7B distills prose better.)
- `facts` table vs. tag-convention for the deterministic lookup — schema choice.
- Conclave grounded critic in v1, or defer?

### Security defaults to fix before any exposure

The donor ships a default Postgres password and an **unauthenticated SSE endpoint on `:3333`**,
localhost-bound. Over the tailnet: bind to the tailnet interface, rotate creds, lean on tailnet
ACLs. **Never** expose `5432` / `3333` to the LAN or wider.

---

## Inherited base — architecture (what's in the tree today)

This is the donor implementation currently checked in. It works, and it's the starting point,
but several pieces are slated to change per the table above.

Two services, orchestrated by Docker Compose, plus an in-stack Ollama:

- **PostgreSQL 16 + pgvector** (port 5432): memories with 768-dim embeddings; schema in
  `init.sql`. Currently uses an **IVFFlat** cosine index (→ HNSW), GIN indexes on tags and
  full-text search, an auto-updating `updated_at` trigger, and soft-deletes via `deleted_at`.
- **FastMCP server** (port 3333): `mcp-server/server.py` exposes MCP tools over SSE plus a web
  UI and REST endpoints. Embeds with `all-mpnet-base-v2` (sentence-transformers). A
  `ThreadedConnectionPool` keeps 1–5 persistent DB connections.
- **Ollama** (in-stack; host port 11737): local LLM for `distill_sessions.py` and
  `behavioral_pass.py`. No API key. Recommended model: `qwen2.5:7b`.

### Commands

```bash
docker compose up -d                                   # start all services
docker compose logs -f mcp-server                      # tail server logs
docker compose down                                    # stop
docker compose down -v                                 # stop + delete volumes (destroys memories)
docker compose build mcp-server && docker compose up -d mcp-server   # rebuild after code changes
```

### Standalone scripts (donor)

- `import_memories.py` — bulk-import from Claude Code session history (`~/.claude/projects/`),
  Claude.ai export JSON, or text/markdown files.
- `distill_sessions.py` — local-LLM distillation of raw sessions into durable memories
  (skips <5-message sessions; dedups new memories at ≥0.85 cosine).
- `extract_signals.py` — **LLM-free** behavioral signal extraction (correction signals →
  preferences; tool/command/file habits → patterns). Slated for reuse.
- `behavioral_pass.py` — targeted LLM pass over distilled sessions for `type:behavior`
  observations (skips <10-message sessions).
- `generate_user_profile.py` — synthesizes memories into `~/.claude/user.md`.
- `import-cron.sh` — the four-step pipeline (import → distill → signals → behavioral).
- `setup-launchagent.sh` — **macOS** LaunchAgent (→ to be replaced by a systemd timer).
- `backup.sh` / `restore.sh` — pg snapshot + restore.

Migrations live in `migrations/` (apply with
`docker exec -i <db-container> psql -U claude -d memory < migrations/NNN_*.sql`).

### MCP tools (inherited surface)

| Tool | Key Parameters | Purpose |
|------|---------------|---------|
| `startup_context` | `project` | Session-start snapshot — behavioral signals + recent distilled memories |
| `save_memory` | `content`, `tags[]`, `source`, `project` | Store with auto-embedding; dedup at high cosine similarity |
| `check_memory` | `content` | Dry-run write guard — ADD/UPDATE/NOOP with nearest match |
| `semantic_search` | `query`, `limit`, `min_similarity`, `project`, `since`, `before` | Vector similarity search (cached) |
| `search_memories` | `query`, `limit`, `project`, `since`, `before` | Full-text keyword search (cached) |
| `hybrid_search` | `query`, `limit`, `keyword_weight`, `semantic_weight`, … | Combined keyword + semantic |
| `list_memories` | `limit`, `offset`, `tag`, `project`, `since`, `before` | Paginated list |
| `get_memory` | `memory_id` | Fetch one by ID (includes `deleted_at`) |
| `recent_context` | `project`, `limit` | Recent distilled memories (falls back to active) |
| `update_memory` | `memory_id`, `content`, `tags[]`, `force` | Update + re-embed |
| `delete_memory` / `restore_memory` | `memory_id` | Soft-delete / restore |
| `purge_memory` | `memory_id` | Permanent delete (must soft-delete first) |
| `find_duplicates` | `threshold`, `limit`, `project`, `scan_limit` | Near-duplicate pairs |
| `bulk_delete` | `tag`, `project`, `source`, `dry_run` | Bulk soft-delete (dry-run by default) |
| `list_tags` / `get_stats` | — | Tag counts / memory + session stats |
| `export_memories` | `project`, `tag`, `since`, `before`, `output_format` | Export JSON or markdown |

REST endpoints and a browser UI (`GET /ui`) are served on the same origin as the MCP server;
`/health` is a liveness probe and `POST /cache/invalidate` clears the search cache.

### Configuration (inherited)

| Variable | Default | Purpose |
|----------|---------|---------|
| `DATABASE_URL` | `postgresql://claude:memory_pass@localhost:5432/memory` | PostgreSQL connection |
| `POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` | `memory` / `claude` / `memory_pass` | DB creds (**rotate**) |
| `OLLAMA_URL` | `http://ollama:11434/v1` (host: `http://localhost:11737/v1`) | Ollama endpoint for distillation |
| `DISTILL_MODEL` / `DISTILL_WORKERS` | `qwen2.5:7b` / `4` | Distillation model + parallelism |
| `TRANSFORMERS_OFFLINE` / `HF_DATASETS_OFFLINE` | `1` (Docker) | Prevent HuggingFace network calls on restart |
| `GUARD_NOOP_THRESHOLD` / `GUARD_UPDATE_THRESHOLD` | `0.85` / `0.75` | Write-guard cosine thresholds |
| `DISTILL_DEDUP_THRESHOLD` | `0.85` | Skip near-duplicate distilled memories |
| `CACHE_MAX_SIZE` / `CACHE_TTL_SECONDS` | `500` / `600` | Search cache size + TTL |

Data persists to `./data/postgres/`; the HuggingFace model cache is volume-mounted.

### Tests

```bash
pytest tests/ -v   # inherited mocked suite — no Docker or GPU required
```

Heavy dependencies (sentence-transformers, psycopg2, openai) are mocked by `tests/conftest.py`.
