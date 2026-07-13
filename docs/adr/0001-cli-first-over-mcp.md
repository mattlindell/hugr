# ADR 0001 — CLI-first architecture, MCP dropped

**Status:** Accepted — 2026-07-12
**Supersedes:** the handoff's "reuse the FastMCP server + tool surface ~as-is."

## Context

The donor centers everything on a FastMCP server (`mcp-server/server.py`): the model-facing tool
surface, the REST API, and the web UI all hang off the `@mcp` object, served over SSE on `:3333`.
hugr runs almost entirely **locally** — the app (core + CLI + hook) on the Windows desktop, talking
over the LAN to Postgres and LiteLLM. The user is opposed to MCP in principle for a local tool, and
the injection hook — the component that actually fixes the bug — is CLI-shaped anyway (a script
that reads Facts).

A key correctness observation: switching MCP → CLI **does not change the reliability tier** of
episodic recall. Both are tier-3, retrieved-on-demand — the model must still *choose* to call them.
The fix is the always-injected hook (tier-4), not the retrieval surface. So the MCP objection costs
nothing on correctness.

## Decision

Adopt a **core-library + CLI-first** architecture:

- A `hugr` **core package** holds all logic (Facts, provenance, snapshot, later: search, dedup).
- A **CLI** is the primary agent- and human-facing surface (`hugr fact set/get/list/confirm`,
  later `hugr search/save`), **allowlisted in `settings.json`** so calls don't prompt.
- The **Injection Hook** is a thin CLI/shared-code path.
- A **thin HTTP server** exists **only** to serve the web Console (`ui.html` + read/confirm API).
- **MCP is dropped.** Because CLI and MCP would both be thin frontends over the same core, an MCP
  adapter is cheap to re-add later if a non-CLI context ever needs it.

Language is **Python for now**; a compiled-binary rewrite (Go/Rust) is a follow-on if the hook's
cold start proves too slow after the core-lib/CLI reconfiguration.

## Consequences

- Kills the unauthenticated SSE surface entirely — no `:3333` endpoint to secure.
- Hook and CLI share one code path (DRY).
- Model invokes a CLI via Bash/PowerShell rather than a schema'd tool — mitigated by allowlisting
  and documenting the CLI in `CLAUDE.md`. Discoverability is the model's responsibility either way
  (same discretion caveat as MCP).
- The donor's MCP-centric `server.py` is demoted to a thin UI adapter or removed.
