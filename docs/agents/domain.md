# Domain Docs

How the engineering skills should consume this repo's domain documentation when exploring the codebase.

## Before exploring, read these

- **`CONTEXT.md`** at the repo root, or
- **`CONTEXT-MAP.md`** at the repo root if it exists — it points at one `CONTEXT.md` per context. Read each one relevant to the topic.
- **`docs/adr/`** — read ADRs that touch the area you're about to work in. In multi-context repos, also check `src/<context>/docs/adr/` for context-scoped decisions.

If any of these files don't exist, **proceed silently**. Don't flag their absence; don't suggest creating them upfront. The `/domain-modeling` skill (reached via `/grill-with-docs` and `/improve-codebase-architecture`) creates them lazily when terms or decisions actually get resolved.

> Note for this repo: `docs/memory-store-handoff.md` is the current source of truth for direction. It is **not** a `CONTEXT.md` glossary — read it for intent, but domain terminology still lives in `CONTEXT.md` once that file is created.

## File structure

This is a **single-context** repo — one `CONTEXT.md` + `docs/adr/` at the repo root:

```text
/
├── CONTEXT.md
├── docs/adr/
│   ├── 0001-hnsw-over-ivfflat.md
│   └── 0002-keyed-facts-table.md
└── mcp-server/
```

(Multi-context layout — a root `CONTEXT-MAP.md` pointing at per-context `CONTEXT.md` files — does not apply here. Switch to it only if hugr later splits into separately-modeled subsystems.)

## Use the glossary's vocabulary

When your output names a domain concept (in an issue title, a refactor proposal, a hypothesis, a test name), use the term as defined in `CONTEXT.md`. Don't drift to synonyms the glossary explicitly avoids.

If the concept you need isn't in the glossary yet, that's a signal — either you're inventing language the project doesn't use (reconsider) or there's a real gap (note it for `/domain-modeling`).

## Flag ADR conflicts

If your output contradicts an existing ADR, surface it explicitly rather than silently overriding:

> _Contradicts ADR-0002 (keyed facts table) — but worth reopening because…_
