# hugr

*Old Norse for the mind — thought, willpower, personality, and the seat of emotions.*

A persistent memory system for Claude Code, built to solve one problem well: **stop the model
from confabulating stable facts about you.**

> **Status: early.** hugr starts from a fork of
> [`daringanitch/claude-memory`](https://github.com/daringanitch/claude-memory) and diverges
> from it heavily. The design is locked; the divergence build is just beginning. The code
> currently in the tree is the inherited base (the "organ donor") — not the finished system.
> The full design and rationale live in [`docs/memory-store-handoff.md`](docs/memory-store-handoff.md).

## The problem

`~/.claude/CLAUDE.md` said the user lives in the Portland, OR metro. Claude Code confidently
answered "San Diego" anyway. That's **confabulation from parametric memory**: when grounding
fails to fire, the model samples a high-prior answer and delivers it fluently.

Passive context (`CLAUDE.md`) isn't enough — it's present but not reliably *attended to*, and
it erodes under long sessions and auto-compaction. Fixing this means moving stable facts to a
tier where the model has no discretion to skip them.

## The thesis

**Route memory by fact type.**

- **Stable atomic facts** (location, name, employer, the dog's name) → a **deterministic keyed
  store**, injected verbatim into every turn via a `UserPromptSubmit` hook. No similarity
  search — fuzzy recall is exactly what caused the bug. The hook fails open: short timeout, and
  on any error it injects nothing rather than stalling the editor.
- **Episodic / fuzzy memory** (past decisions and their rationale, "have we discussed this,"
  session summaries, code context by meaning) → a **pgvector semantic store**. This is where
  approximate recall belongs, and where it's harmless.
- **Provenance on everything.** User-stated facts and model-distilled guesses are tracked
  separately; only confirmed facts get promoted into the always-injected tier.

## Lineage

hugr is an **organ donor transplant**, not a fork in the maintain-upstream sense. From
[`daringanitch/claude-memory`](https://github.com/daringanitch/claude-memory) (MIT) we keep the
FastMCP server and its tool surface, the hybrid keyword+semantic search, the LLM-free
behavioral signal extractor, and the write-hygiene machinery (dedup, dry-run write guard, soft
deletes, health checks). Full credit to that project for the foundation.

What changes:

| | |
|---|---|
| **Swap** | IVFFlat → **HNSW** vector index • macOS LaunchAgent + localhost → **home-server containers over Tailscale** + a systemd timer |
| **Add** | Fail-open **`UserPromptSubmit` injection hook** • a keyed **`facts` table** for deterministic lookup • **provenance** on the write path |
| **Later** | A grounded critic that diffs factual claims against the fact store before the model speaks |

## Roadmap

1. Stand the stack up on the home server; bind to the tailnet, rotate the default creds.
2. Migrate the vector index to HNSW; add the keyed `facts` table.
3. Build the fail-open `UserPromptSubmit` hook — deterministic `facts` lookup + top-k episodic
   recall, injected as plain statements. (Tested in the raw CLI first — see the Windows
   `additionalContext` caveat in the handoff.)
4. Add provenance and gate fact promotion behind confirmation.
5. Import existing history and smoke-test the original failure: *"where does the user live?"*
   must deterministically return Portland.

See [`docs/memory-store-handoff.md`](docs/memory-store-handoff.md) for the full plan, rationale,
open questions, and risks.

## Running the inherited base

The donor stack still runs as-is while the divergence is built out:

```bash
docker compose up -d          # PostgreSQL 16 + pgvector, FastMCP server (:3333), in-stack Ollama
docker compose logs -f mcp-server
```

Operational detail (import scripts, distillation, MCP tool reference, configuration) lives in
[`CLAUDE.md`](CLAUDE.md).

> ⚠️ **Security:** the inherited stack ships a default Postgres password and an
> **unauthenticated** SSE endpoint on `:3333`. Rotate the credentials and keep both `5432` and
> `3333` bound to loopback or the tailnet interface — never expose them to the LAN or wider.

## Stack

- [pgvector](https://github.com/pgvector/pgvector) — vector similarity search for PostgreSQL
- [FastMCP](https://github.com/jlowin/fastmcp) — MCP server framework
- [sentence-transformers](https://www.sbert.net/) — `all-mpnet-base-v2` for 768-dim embeddings
- [Model Context Protocol](https://modelcontextprotocol.io/) — tool interface for Claude
- [Ollama](https://ollama.com) — local LLM inference for session distillation

## License

MIT. Includes code from [`daringanitch/claude-memory`](https://github.com/daringanitch/claude-memory)
(Copyright © 2026 daringanitch) — see [LICENSE](LICENSE).
