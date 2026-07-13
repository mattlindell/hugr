# Hugr

The domain vocabulary for **hugr** — a persistent memory store whose job is to stop the model
from confabulating stable user facts. This glossary pins the terms that acquired a precise
meaning while designing the fact tier and its injection path. For *direction and rationale*, see
`docs/memory-store-handoff.md`; for *decisions*, see `docs/adr/`. This file is the **language**.

## Language

**Fact**:
An **atomic, identity-stable, confabulation-prone** datum about the user or a project, stored as a
single `key → value` and injected **verbatim** every turn. "Atomic" = one key/value, not a
narrative. "Identity-stable" = changes only by an explicit life event, never by inference.
"Confabulation-prone" = the model will confidently guess it wrong (a high-prior default like a
city) and being wrong is user-visible. All three must hold. Anything that fails one is a
**Memory**, not a Fact.
_Avoid_: "memory" (that's the fuzzy tier), "preference", "setting", "note".

**Fact Criterion**:
The three-part gate — atomic + identity-stable + confabulation-prone — that decides whether a
candidate is admitted to the fact tier at all. The promotion path tests against this; the injected
payload is bounded by it. Semi-stable stuff ("current project", "active branch") fails and stays
episodic.
_Avoid_: "the rules", "validation".

**Fact Scope**:
The dimension that says *where a Fact applies*: **global** (`*`) — true regardless of directory or
project (location, legal name, employer) — or **project** — a per-project immutable (language,
package manager). The `facts` primary key is composite `(scope, key)`. **0.5 injects global Facts
only**; the schema carries project scope from day one but project-scoped injection is deferred
until Scope Identity is resolved.
_Avoid_: "namespace", "project column" (there is no separate project column — scope subsumes it).

**Scope Identity**:
The unresolved problem of reliably keying a *project* Fact given worktrees, subdirectories, and
name collisions (two repos named `web`). Gates project-scoped injection; out of scope for 0.5.
_Avoid_: "project name" (that's the ambiguous thing we're trying to pin down).

**Memory** (episodic):
A fuzzy, prose-shaped recollection — past decisions and rationale, "have we discussed X",
session summaries, code context by meaning. Lives in the `memories` table behind vector search;
**retrieved on demand**, never injected every turn. Tolerant of the model *choosing* when it's
relevant.
_Avoid_: "fact" (a Memory is explicitly not identity-stable), "context".

**Provenance**:
The record of **who asserted a Fact** — `asserted_by ∈ {user, model}` — plus `confirmed_at`.
"User-stated" means an explicit in-session utterance the model transcribes; "model-distilled"
means batch inference over history. Provenance gates promotion into the injected tier.
_Avoid_: "origin", "author".

**Confirmed / Pending**:
A Fact is **Confirmed** (`confirmed_at IS NOT NULL`) or **Pending**. **Confirmed is the sole
injection gate** — the Snapshot and hook only ever see Confirmed Facts. A user assertion is
Confirmed on write; a model proposal is Pending until a human confirms it in the Console.
_Avoid_: "approved", "live" (say Confirmed), "draft" (say Pending).

**Console**:
The web UI (extended in place from the donor's `ui.html`) that views Facts and Memories and is the
**authoritative surface for confirming Pending Facts**. Confirmation happens here, out-of-band —
**never inline in an agent session** (inline reintroduces the model discretion the design removes).
_Avoid_: "dashboard", "admin panel".

**Snapshot**:
The pre-rendered, on-desktop file of Confirmed global Facts that the hook reads. Regenerated
**event-triggered** on every Fact mutation (primary), with a **TTL age-check** on non-hot-path core
actions as a resilience backstop for out-of-band changes. The access pattern that justifies it:
write-rarely, read-every-turn.
_Avoid_: "cache" (too generic), "dump".

**Injection Hook**:
The `UserPromptSubmit` hook that reads the Snapshot and injects Confirmed Facts as **plain
statements** (not imperative "ALWAYS remember…") every turn. This is the layer that actually fixes
the bug: it survives attention decay and compaction and removes the model's discretion. It is
**fail-open** (short ~2s timeout; on any error, inject nothing and let the turn proceed).
_Avoid_: "the injector", "middleware".

**Cold-start Invariant**:
The rule that the Injection Hook's hot path **never imports the heavy stack** (`torch`,
`sentence-transformers`, `psycopg2`). The hook is a stdlib-only Snapshot reader. Violating this
puts multi-second interpreter/import cost on every turn.
_Avoid_: "performance budget" (this is a hard invariant, not a budget).

**Fail-open**:
The posture of the Injection Hook and Snapshot read: any error, timeout, or missing file →
**inject nothing and proceed**. A memory store that occasionally forgets is fine; one that freezes
the editor is not. A slightly stale Snapshot is acceptable because Facts are near-immutable.
_Avoid_: "graceful degradation" (be specific: inject nothing, proceed).

## Storage taxonomy (the tiers)

Worst → best for stable atomic Facts:

1. **Parametric** (in weights) — unreliable; the source of the "San Diego" confabulation.
2. **In-context passive** (`CLAUDE.md`) — present but ignorable; compaction drops it.
3. **Retrieved-on-demand** (CLI/tool) — reliable *only if the model chooses to call it*. Correct
   tier for **Memory**, wrong tier for **Fact**.
4. **Always-injected** (Injection Hook) — removes discretion. Correct tier for **Fact**.

## Relationships

- A **Fact** is admitted only if it passes the **Fact Criterion**; otherwise it is a **Memory**.
- A **Fact** has a **Fact Scope** (global or project) and **Provenance**.
- A Fact is injectable only when **Confirmed**; the **Console** is where **Pending** Facts become
  Confirmed.
- The **Snapshot** contains exactly the Confirmed global Facts; the **Injection Hook** reads the
  Snapshot under the **Cold-start Invariant** and **Fail-open** posture.
- **Memory** lives in tier 3 (retrieved on demand); **Fact** lives in tier 4 (always injected).

## Example dialogue

> **Dev:** "The distiller decided I live in Seattle. Does that overwrite my location Fact?"
> **Domain expert:** "No. That's a **model** **Provenance** proposal, so it lands **Pending** — it
> can't touch a **Confirmed** Fact. The **Snapshot** never sees it, so the **Injection Hook** never
> injects it. It sits in the **Console** until you confirm. A user assertion would win; batch
> inference never overwrites a live Fact."

## Flagged ambiguities

- "memory" was overloaded (the project, the table, the tier). Resolved: **Memory** = the episodic
  tier only; the product is **hugr**; the fuzzy table is `memories`; a stable datum is a **Fact**.
- "project" is ambiguous between a Fact's **Fact Scope** and the unresolved **Scope Identity**
  problem — keep them distinct.
