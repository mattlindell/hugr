# Triage Labels

The skills speak in terms of five canonical triage roles. In Linear these are **labels** (grouped under the **"Agentic State Machine"** label group on the Photon Ventures team), distinct from workflow *statuses* (see `issue-tracker.md`). Each role maps 1:1 to an existing label — no overrides.

| Canonical role   | Linear label      | Meaning                                  |
| ---------------- | ----------------- | ---------------------------------------- |
| `needs-triage`   | `needs-triage`    | Maintainer needs to evaluate this issue  |
| `needs-info`     | `needs-info`      | Waiting on reporter for more information |
| `ready-for-agent`| `ready-for-agent` | Fully specified, ready for an AFK agent  |
| `ready-for-human`| `ready-for-human` | Requires human implementation            |
| `wontfix`        | `wontfix`         | Will not be actioned                     |

When a skill mentions a role (e.g. "apply the AFK-ready triage label"), apply the corresponding Linear label **by name** — `linearis issues update PV-<n> --labels <name> --label-mode add` (MCP `save_issue` with the updated `labels` set as fallback). These labels already exist — do **not** create new ones.

Use `--label-mode add` to append a triage label; `--label-mode overwrite` (or omit) to replace the set. Raw label UUIDs (for disambiguation or to skip a lookup) live in `docs/agents/linear-ids.local.md` (gitignored, not committed); resolve by name if that file is absent.
