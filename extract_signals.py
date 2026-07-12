#!/usr/bin/env python3
"""
Extract behavioral signals from Claude Code session JSONL files.
Produces preference and pattern memories without an LLM.

Per-session signals (saved immediately):
  - Correction/negation messages → explicit user preference memories

Per-project signals (aggregated across all sessions for the project):
  - Tool usage patterns → workflow fingerprint
  - Bash command patterns → tooling habits
  - Frequently accessed files → hotspot awareness

Usage:
  python extract_signals.py                    # process all pending sessions
  python extract_signals.py --project osint    # filter by project
  python extract_signals.py --dry-run          # preview without writing
"""

import argparse
import json
import logging
import os
import re
from collections import Counter
from pathlib import Path

import psycopg2
import psycopg2.extras
from pgvector.psycopg2 import register_vector
from sentence_transformers import SentenceTransformer

_raw = os.environ.get("LOGLEVEL", "INFO").upper()
_level = getattr(logging, _raw, None)
if not isinstance(_level, int):
    _level = logging.INFO
logging.basicConfig(
    # Honor LOGLEVEL env var (set by Invoke-ImportPipeline.ps1's -Verbosity flag);
    # fall back to INFO for unset or unrecognised values.
    level=_level,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("signals")

# Silence third-party INFO chatter that drowns out real progress (sentence-
# transformers emits a model-load chunk on import). Keep WARNING+ so genuine
# problems still surface.
for noisy in ("httpx", "httpcore", "openai", "urllib3", "sentence_transformers"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


def progress(msg):
    """User-facing progress line — bypasses LOGLEVEL filtering so it stays
    visible at any -Verbosity. Use this for per-session/per-project status
    and run summaries. Reserve log.info() for diagnostic events that the
    user may want suppressed at low verbosity."""
    import time
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')}  {msg}", flush=True)

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://claude:memory_pass@localhost:5432/memory")
CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"

# Detect user messages that correct or negate Claude's previous action.
# Must appear at the START of the message (anchored with ^).
CORRECTION_RE = re.compile(
    r"^\s*("
    r"no\b|nope\b|don'?t\b|do not\b|stop\b|never\b|wrong\b|incorrect\b|"
    r"not that\b|actually[,\s]|wait[,\s]|instead\b|avoid\b|"
    r"please don'?t|that'?s not|don'?t use|don'?t do|don'?t add|"
    r"don'?t include|don'?t make|don'?t put|don'?t create|don'?t import"
    r")",
    re.IGNORECASE,
)

# Tools grouped by workflow category for fingerprinting.
TOOL_CATEGORIES = {
    "file_editing": {"Read", "Edit", "Write", "NotebookEdit"},
    "search":       {"Glob", "Grep"},
    "execution":    {"Bash"},
    "web":          {"WebSearch", "WebFetch"},
    "ai_agent":     {"Agent"},
}

# Minimum sessions before emitting an aggregate pattern memory.
MIN_SESSIONS_FOR_AGGREGATE = 2
# Minimum times a file must appear to be included in the hotspot list.
MIN_FILE_HITS = 2


# ── DB helpers ────────────────────────────────────────────────────────────────

def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    register_vector(conn)
    return conn


def ensure_migration(conn):
    """Add signals_extracted column to imported_sessions if not already present."""
    with conn.cursor() as cur:
        cur.execute(
            "ALTER TABLE imported_sessions "
            "ADD COLUMN IF NOT EXISTS signals_extracted BOOLEAN DEFAULT FALSE"
        )
    conn.commit()


def embed(text, embedder):
    return embedder.encode(text, normalize_embeddings=True, show_progress_bar=False)


def insert_memory(cur, content, tags, source, project, embedder):
    vector = embed(content, embedder)
    cur.execute(
        "INSERT INTO memories (content, tags, source, project, embedding) "
        "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (content_hash) DO NOTHING",
        (content, tags, source, project, vector),
    )


def upsert_aggregate_memory(conn, content, tags, source, project, embedder):
    """Replace an auto-generated aggregate memory in a single transaction."""
    vector = embed(content, embedder)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM memories WHERE source = %s", (source,))
        cur.execute(
            "INSERT INTO memories (content, tags, source, project, embedding) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (content_hash) DO NOTHING",
            (content, tags, source, project, vector),
        )
    conn.commit()


def get_pending_sessions(conn, project_filter=None):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        if project_filter:
            cur.execute(
                "SELECT session_id, project FROM imported_sessions "
                "WHERE signals_extracted = FALSE AND project ILIKE %s ORDER BY imported_at",
                (f"%{project_filter}%",),
            )
        else:
            cur.execute(
                "SELECT session_id, project FROM imported_sessions "
                "WHERE signals_extracted = FALSE ORDER BY imported_at"
            )
        return cur.fetchall()


def get_all_sessions_for_project(conn, project):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT session_id FROM imported_sessions WHERE project = %s",
            (project,),
        )
        return [row[0] for row in cur.fetchall()]


def mark_extracted(conn, session_id):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE imported_sessions SET signals_extracted = TRUE WHERE session_id = %s",
            (session_id,),
        )
    conn.commit()


# ── JSONL parsing ─────────────────────────────────────────────────────────────

def find_jsonl(session_id):
    """Search ~/.claude/projects/* for <session_id>.jsonl. Returns Path or None."""
    if not CLAUDE_PROJECTS_DIR.exists():
        return None
    for project_dir in CLAUDE_PROJECTS_DIR.iterdir():
        path = project_dir / f"{session_id}.jsonl"
        if path.exists():
            return path
    return None


def load_records(jsonl_path):
    records = []
    for line in jsonl_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


# ── Signal extraction ─────────────────────────────────────────────────────────

def extract_tool_calls(records):
    """Return list of (tool_name, tool_input) from all assistant records."""
    calls = []
    for r in records:
        if r.get("type") != "assistant":
            continue
        content = r.get("message", {}).get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                calls.append((block.get("name", ""), block.get("input", {})))
    return calls


def extract_corrections(records):
    """
    Find user text messages that immediately follow an assistant tool_use turn
    and match the correction pattern.

    Returns list of (correction_text, preceding_tool_name, preceding_tool_input).
    """
    corrections = []
    last_assistant_tools = []

    for r in records:
        rtype = r.get("type")

        if rtype == "assistant":
            content = r.get("message", {}).get("content", [])
            if isinstance(content, list):
                tools = [
                    (b.get("name", ""), b.get("input", {}))
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "tool_use"
                ]
                if tools:
                    last_assistant_tools = tools

        elif rtype == "user" and last_assistant_tools:
            content = r.get("message", {}).get("content", "")

            # Skip messages that are purely tool results (not human input)
            if isinstance(content, list):
                if any(
                    isinstance(b, dict) and b.get("type") == "tool_result"
                    for b in content
                ):
                    continue
                text = " ".join(
                    b.get("text", "")
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                ).strip()
            else:
                text = str(content).strip()

            if not text or len(text) > 300:
                last_assistant_tools = []
                continue

            if CORRECTION_RE.match(text):
                tool_name, tool_input = last_assistant_tools[-1]
                corrections.append((text, tool_name, tool_input))

            last_assistant_tools = []

    return corrections


def corrections_to_memories(corrections, project):
    """Format raw correction signals as preference memory strings."""
    memories = []
    for text, tool_name, tool_input in corrections:
        context = ""
        if tool_name == "Bash":
            cmd = tool_input.get("command", "").strip()[:80]
            context = f" (after running: `{cmd}`)" if cmd else ""
        elif tool_name in ("Edit", "Write", "Read"):
            fp = tool_input.get("file_path", "")
            context = f" (while editing `{fp}`)" if fp else ""
        elif tool_name == "Agent":
            context = " (after delegating to a subagent)"

        memories.append(
            f'User preference [{project}]: "{text}"{context}'
        )
    return memories


def bash_command_counts(tool_calls):
    """Extract leading command words from Bash tool inputs."""
    counts = Counter()
    for name, inp in tool_calls:
        if name != "Bash":
            continue
        cmd = inp.get("command", "").strip()
        if not cmd:
            continue
        first_word = re.split(r"[\s|;&]", cmd)[0].lstrip("$").strip()
        if first_word and len(first_word) < 40 and not first_word.startswith("-"):
            counts[first_word] += 1
    return counts


def file_path_counts(tool_calls):
    """Extract file names from file-related tool calls."""
    counts = Counter()
    file_tools = {"Read", "Edit", "Write", "Glob", "Grep"}
    for name, inp in tool_calls:
        if name not in file_tools:
            continue
        for key in ("file_path", "path", "pattern"):
            val = inp.get(key, "")
            if val and isinstance(val, str) and not val.startswith("**"):
                p = Path(val)
                if p.suffix:  # only count actual files, not directories
                    counts[p.name] += 1
    return counts


def tool_category_summary(tool_calls):
    """Return (category_counts, top_individual_tool_counts)."""
    tool_name_counts = Counter(name for name, _ in tool_calls)
    cat_counts = Counter()
    for cat, tools in TOOL_CATEGORIES.items():
        cat_counts[cat] = sum(tool_name_counts.get(t, 0) for t in tools)
    return cat_counts, tool_name_counts


# ── Per-session processing ────────────────────────────────────────────────────

def process_session(conn, session_id, project, embedder, dry_run=False):
    """
    Extract correction signals from one session and save as preference memories.
    Returns the number of memories saved.
    """
    jsonl_path = find_jsonl(session_id)
    if not jsonl_path:
        log.debug("  [%s] JSONL not found — skipping", session_id[:8])
        if not dry_run:
            mark_extracted(conn, session_id)
        return 0

    records = load_records(jsonl_path)
    corrections = extract_corrections(records)

    if not corrections:
        if not dry_run:
            mark_extracted(conn, session_id)
        return 0

    memories = corrections_to_memories(corrections, project)
    saved = 0
    failed = 0

    for content in memories:
        progress(f"  [{session_id[:8]}] Preference: {content[:120]}")
        if not dry_run:
            tags = ["type:preference", f"project:{project}", "source:signals", "correction"]
            try:
                with conn.cursor() as cur:
                    insert_memory(cur, content, tags, f"signals/{session_id[:8]}", project, embedder)
                conn.commit()
                saved += 1
            except psycopg2.Error as e:
                conn.rollback()
                log.error("  [%s] DB error: %s", session_id[:8], e)
                failed += 1

    if not dry_run:
        if failed:
            log.warning("  [%s] %d/%d inserts failed — not marking extracted (will retry next run)",
                        session_id[:8], failed, len(memories))
        else:
            mark_extracted(conn, session_id)

    return saved


# ── Per-project aggregate ─────────────────────────────────────────────────────

def run_project_aggregate(conn, project, embedder, dry_run=False):
    """
    Read all JSONL files for a project, aggregate tool/command/file signals,
    and upsert pattern memories.
    """
    session_ids = get_all_sessions_for_project(conn, project)

    all_tool_calls = []
    sessions_found = 0

    for session_id in session_ids:
        jsonl_path = find_jsonl(session_id)
        if not jsonl_path:
            continue
        records = load_records(jsonl_path)
        all_tool_calls.extend(extract_tool_calls(records))
        sessions_found += 1

    if sessions_found < MIN_SESSIONS_FOR_AGGREGATE or not all_tool_calls:
        progress(f"  [{project}] {sessions_found} sessions found — skipping aggregate (min {MIN_SESSIONS_FOR_AGGREGATE})")
        return 0

    cat_counts, tool_counts = tool_category_summary(all_tool_calls)
    bash_commands = bash_command_counts(all_tool_calls)
    file_paths = file_path_counts(all_tool_calls)
    saved = 0

    # 1. Workflow fingerprint
    total = sum(cat_counts.values())
    if total > 0:
        top_cats = [
            f"{cat} ({round(count / total * 100)}%)"
            for cat, count in cat_counts.most_common(4)
            if count > 0
        ]
        top_tools = [f"{t} ({c}x)" for t, c in tool_counts.most_common(6)]
        content = (
            f"Workflow pattern for project '{project}' ({sessions_found} sessions): "
            f"tool categories — {', '.join(top_cats)}. "
            f"Top tools: {', '.join(top_tools)}."
        )
        tags = ["type:pattern", "type:behavior", f"project:{project}", "source:signals", "workflow"]
        source = f"signals/aggregate/workflow/{project}"
        progress(f"  [{project}] Workflow pattern: {content[:120]}")
        if not dry_run:
            upsert_aggregate_memory(conn, content, tags, source, project, embedder)
        saved += 1

    # 2. Bash command habits
    if bash_commands:
        top_cmds = [cmd for cmd, _ in bash_commands.most_common(10)]
        content = (
            f"Common shell commands in project '{project}': {', '.join(top_cmds)}. "
            f"({sum(bash_commands.values())} Bash calls across {sessions_found} sessions)"
        )
        tags = ["type:pattern", f"project:{project}", "source:signals", "commands"]
        source = f"signals/aggregate/commands/{project}"
        progress(f"  [{project}] Command pattern: {content[:120]}")
        if not dry_run:
            upsert_aggregate_memory(conn, content, tags, source, project, embedder)
        saved += 1

    # 3. Frequently accessed files
    hot_files = [f for f, c in file_paths.most_common(10) if c >= MIN_FILE_HITS]
    if hot_files:
        content = (
            f"Frequently accessed files in project '{project}': {', '.join(hot_files)}. "
            f"(each accessed {MIN_FILE_HITS}+ times across {sessions_found} sessions)"
        )
        tags = ["type:pattern", f"project:{project}", "source:signals", "files"]
        source = f"signals/aggregate/files/{project}"
        progress(f"  [{project}] File hotspots: {content[:120]}")
        if not dry_run:
            upsert_aggregate_memory(conn, content, tags, source, project, embedder)
        saved += 1

    progress(f"  [{project}] {saved} aggregate pattern memories {'would be saved' if dry_run else 'saved/updated'}")
    return saved


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Extract behavioral signals from Claude Code sessions into memories"
    )
    parser.add_argument("--project", help="Filter to sessions from this project")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview extractions without writing to DB")
    args = parser.parse_args()

    log.info("Loading embedding model...")
    embedder = SentenceTransformer("all-mpnet-base-v2")

    conn = get_db()
    ensure_migration(conn)

    sessions = get_pending_sessions(conn, args.project)

    if not sessions:
        progress("No sessions pending signal extraction.")
        conn.close()
        return

    mode = "[DRY RUN] " if args.dry_run else ""
    progress(f"{mode}=== Extracting signals from {len(sessions)} session(s) ===")

    projects_touched = set()
    total_preferences = 0

    for session in sessions:
        session_id = session["session_id"]
        project = session["project"] or "unknown"
        n = process_session(conn, session_id, project, embedder, args.dry_run)
        total_preferences += n
        projects_touched.add(project)

    # Refresh project-level aggregate patterns for every affected project
    progress(f"{mode}=== Updating aggregates for {len(projects_touched)} project(s) ===")
    for project in sorted(projects_touched):
        run_project_aggregate(conn, project, embedder, args.dry_run)

    conn.close()
    log.info(
        "%sDone. %d preference memories %s.",
        mode,
        total_preferences,
        "would be saved" if args.dry_run else "saved",
    )


if __name__ == "__main__":
    main()
