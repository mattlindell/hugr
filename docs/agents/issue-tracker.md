# Issue tracker: Linear

Issues, PRDs, and specs for this repo live in **Linear**. There are two ways in:

- **`linearis` CLI (default / fast path)** — a local CLI (`.claude/skills/linearis-cli`; installed globally). JSON output, pipe through `jq`. Faster than the MCP and covers the whole everyday loop: list/search/read, create/update (labels, status, project, `--parent-ticket`, `blocks`/`blocked-by` relations), and discussion threads. **Reach for this first.**
- **Linear MCP (`mcp__linear__*`, fallback)** — broader surface. Use it only for operations the CLI can't do (see "CLI gaps" below) or when a skill explicitly needs an MCP tool.

Both accept **human-readable identifiers** (team key `PV`, issue `PV-123`, project/label names), so no UUIDs are needed on either path.

> **Prerequisite:** the CLI path needs the `linearis` binary on `PATH` and authenticated — it is
> **not** installed by this repo. The committed `.claude/skills/linearis-cli` skill is only the
> command reference; the binary is a separate install (Matt's fleet installs it via mise). Auth:
> `linearis auth login`, `--api-token <token>`, `LINEAR_API_TOKEN`, or `~/.linearis/token`. If
> `linearis` is unavailable (fresh clone, CI, AFK agent without it provisioned), use the **MCP
> path** for everything instead.

### CLI gaps → use the MCP

- **Creating labels** — `linearis labels` only *lists*; it can't create. New labels → MCP `create_issue_label`. (The triage and Wayfinder label groups already exist, so this is rarely needed.)
- Anything outside the CLI's domains (`issues`, `comments`, `labels`, `projects`, `cycles`, `milestones`, `documents`, `files`, `attachments`, `teams`, `users`, `initiatives`) — check `linearis usage` / `linearis <domain> usage`, and fall back to the MCP if absent.

## Fixed coordinates

Both tools resolve teams, projects, and labels **by name**, so use these names directly:

| | Value |
| --- | --- |
| **Team** | Photon Ventures — key `PV` |
| **Project** | Hugr ("Agentic memory") |

Always create issues **scoped to the Hugr project and the Photon Ventures team** unless told otherwise.

> **Resolved workspace IDs are not committed** (they're unique to this Linear workspace and this
> repo is public). If you need the raw team/project/status/label UUIDs — to skip a lookup or to
> disambiguate — read `docs/agents/linear-ids.local.md` (gitignored). If that file is absent,
> resolve everything by the names above; both tools handle name resolution.

## Workflow states (Linear statuses)

Linear models "state" as a status, not a label. The Photon Ventures team statuses, by name:
`Backlog`, `Todo`, `In Progress`, `In Review`, `Done`, `Duplicate`, `Canceled`. Pass the status
by name (CLI `--status`, MCP `state`); raw status IDs live in `linear-ids.local.md`.

The five **triage roles** are Linear *labels*, not statuses — see `triage-labels.md`.

## Conventions

Each operation lists the **CLI (default)** then the **MCP (fallback)**.

- **Create an issue**: `linearis issues create "<title>" --team PV --project Hugr --description "<md>" [--labels a,b] [--status "<name>"]` · MCP `save_issue` with `title`, `description` (real newlines, not `\n`), `team` = `PV`, `project` = `Hugr`.
- **Read an issue**: `linearis issues read PV-<n> --with-comment-threads` · MCP `get_issue` + `list_comments`.
- **List / search issues**: `linearis issues list --team PV --project Hugr [--label … --status … --assignee …]` or `linearis issues search "<query>"` · MCP `list_issues`.
- **Comment on an issue**: `linearis issues discuss PV-<n> --body "<md>"` (start a thread); reply with `linearis issues reply <thread-id> --body "…"` · MCP `save_comment`.
- **Apply / change labels or status**: `linearis issues update PV-<n> --labels <names> [--label-mode add] [--status "<name>"]` · MCP `save_issue` with the new `labels`/`state`. (Use `--label-mode add` to append rather than overwrite.)
- **Close**: `linearis issues update PV-<n> --status Done` (or `Canceled`) · MCP `save_issue` transitioning `state`.

## When a skill says "publish to the issue tracker"

Create a Linear issue in the Hugr project — `linearis issues create … --team PV --project Hugr` (MCP `save_issue` fallback).

## When a skill says "fetch the relevant ticket"

`linearis issues read PV-<n> --with-comment-threads` (MCP `get_issue` + `list_comments` fallback).

## Pull requests as a triage surface

Not applicable — Linear is the request surface, not GitHub PRs. Code review happens on GitHub PRs, but incoming requests/bugs/features are triaged as Linear issues.

## Wayfinding operations

Used by `/wayfinder`. Model the **map** and its **child tickets** as Linear issues:

Wayfinder labels are a workspace-level **"Wayfinder" label group** with children named `wayfinder:map`, `wayfinder:research`, `wayfinder:prototype`, `wayfinder:grilling`, `wayfinder:task` — apply the full prefixed name. (These already exist workspace-wide; no seeding needed.)

- **Map**: a single issue labeled `wayfinder:map` holding the Notes / Decisions-so-far / Fog body.
- **Child ticket**: an issue linked to the map as a Linear **sub-issue** via `linearis issues create "<title>" --team PV --project Hugr --parent-ticket PV-<map> --labels wayfinder:<type>` (MCP `save_issue` with `parent` as fallback), where `<type>` is `research` / `prototype` / `grilling` / `task`. Once claimed, assign the ticket to the driving dev.
- **Blocking**: Linear's native **blocks / blocked-by relations** — `linearis issues update PV-<child> --blocked-by PV-<blocker>` (MCP fallback). A child is unblocked when every blocker is `Done`/`Canceled`. Query blocked children with `linearis issues list --has-blockers`. Where a relation can't be created, fall back to a `Blocked by: PV-<n>, PV-<n>` line at the top of the child body.
- **Frontier query**: `linearis issues list --team PV --parent PV-<map>` for the map's open sub-issues (drop completed/canceled statuses); drop any with an open blocker (`--has-blockers` marks these) or an assignee; first in map order wins.
- **Claim**: `linearis issues update PV-<n> --assignee <me>` — the session's first write.
- **Resolve**: `linearis issues discuss PV-<n> --body "<answer>"`, `linearis issues update PV-<n> --status Done`, then append a context pointer to the map's Decisions-so-far.
