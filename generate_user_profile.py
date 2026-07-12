#!/usr/bin/env python3
"""
Generate ~/.claude/user.md from distilled memories in PostgreSQL.

Usage:
  python generate_user_profile.py                        # write to ~/.claude/user.md (CLAUDE.md not touched)
  python generate_user_profile.py --patch-claude-md      # also prepend User Profile section to CLAUDE.md
  python generate_user_profile.py --dry-run              # print to stdout, no files written
  python generate_user_profile.py --output /custom/path.md

Security note (H4):
  CLAUDE.md is injected into every Claude Code session at startup. Automatically
  patching it creates a persistent prompt-injection path if memories have been
  poisoned (e.g. via distilled transcript content). --patch-claude-md must be
  passed explicitly so this write never happens by accident or via a cron job.
"""
import argparse
import logging
import os
from datetime import date
from pathlib import Path

import psycopg2
import psycopg2.extras

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%Y-%m-%dT%H:%M:%S")
log = logging.getLogger("generate_user_profile")

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://claude:memory_pass@localhost:5432/memory")
OUTPUT_PATH  = Path.home() / ".claude" / "user.md"
CLAUDE_MD_PATH = Path.home() / ".claude" / "CLAUDE.md"

TECH_TAGS = {
    "python", "react", "typescript", "javascript", "docker", "flask",
    "fastapi", "k8s", "kubernetes", "terraform", "golang", "go",
    "postgresql", "postgres", "redis", "neo4j", "nodejs", "brew",
    "bash", "rust", "java", "nginx", "aws", "gcp", "azure",
}

CLAUDE_MD_MARKER  = "## User Profile"
CLAUDE_MD_SECTION = (
    "## User Profile\n"
    "See `~/.claude/user.md` for your generated profile — preferences, working style, "
    "active projects, and tooling. Regenerated automatically every 30 minutes by claude-memory.\n\n"
)


def query_identity(conn):
    """Return (top_projects, stack_tags) counted from all active memory tags."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT unnest(tags) AS tag, COUNT(*) AS cnt "
            "FROM memories WHERE deleted_at IS NULL "
            "GROUP BY tag ORDER BY cnt DESC"
        )
        rows = cur.fetchall()
    projects, stack = [], []
    for row in rows:
        tag = row["tag"]
        if tag.startswith("project:"):
            name = tag[len("project:"):]
            if name not in projects:
                projects.append(name)
        elif tag.lower() in TECH_TAGS:
            normalized = tag.capitalize() if tag.islower() else tag
            if normalized not in stack:
                stack.append(normalized)
    return projects[:6], stack[:8]


def build_identity_section(projects, stack):
    """Return markdown ## Identity section, or None if no data."""
    lines = []
    if projects:
        lines.append(f"- **Active projects:** {', '.join(projects)}")
    if stack:
        lines.append(f"- **Stack:** {', '.join(stack)}")
    if not lines:
        return None
    return "## Identity\n" + "\n".join(lines)


def _first_substantive_line(content):
    """Extract the first non-title, non-bold line from a memory (handles auto-memory format).

    Auto-memory files have a short title paragraph followed by the actual preference.
    Skip single-paragraph lines that look like titles (no sentence-ending punctuation,
    no code markers, and shorter than 40 chars).
    """
    paras = [p.strip() for p in content.split("\n\n") if p.strip()]
    # If there are multiple paragraphs, the first may be a title — skip it
    # when it looks like a heading (short, no backticks, no sentence punctuation)
    start = 0
    if len(paras) > 1:
        candidate = paras[0].split("\n")[0].strip()
        is_title = (
            not candidate.startswith("**")
            and not candidate.startswith("#")
            and "`" not in candidate
            and not candidate.endswith((".", "!", "?", ";", ":"))
            and len(candidate) < 40
        )
        if is_title:
            start = 1
    for para in paras[start:]:
        if para.startswith("**") or para.startswith("#"):
            continue
        first_line = para.split("\n")[0].strip()
        if len(first_line) > 10:
            return first_line
    return content.split("\n")[0].strip()


def query_preferences(conn):
    """Return content strings for type:preference memories, auto-memory rows first."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        # Boolean expression in ORDER BY: True > False in Postgres, so DESC puts auto-memory rows first
        cur.execute(
            "SELECT content FROM memories "
            "WHERE 'type:preference' = ANY(tags) AND deleted_at IS NULL "
            "ORDER BY ('source:auto-memory' = ANY(tags)) DESC, created_at DESC "
            "LIMIT 10"
        )
        return [r["content"] for r in cur.fetchall()]


def build_preferences_section(contents):
    """Return markdown ## Preferences section, or None if empty."""
    items = [_first_substantive_line(c) for c in contents if c.strip()]
    items = [i[:160] for i in items if i]
    if not items:
        return None
    return "## Preferences\n" + "\n".join(f"- {item}" for item in items)


def query_working_style(conn):
    """Return content strings for type:behavior memories."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT content FROM memories "
            "WHERE 'type:behavior' = ANY(tags) AND deleted_at IS NULL "
            "ORDER BY created_at DESC LIMIT 8"
        )
        return [r["content"] for r in cur.fetchall()]


def build_working_style_section(contents):
    """Return markdown ## Working Style section, or None if empty."""
    items = [_first_substantive_line(c)[:160] for c in contents if c.strip()]
    items = [i for i in items if i]
    if not items:
        return None
    return "## Working Style\n" + "\n".join(f"- {item}" for item in items)


def query_active_projects(conn):
    """Return content strings for type:project memories."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT content FROM memories "
            "WHERE 'type:project' = ANY(tags) AND deleted_at IS NULL "
            "ORDER BY created_at DESC LIMIT 5"
        )
        return [r["content"] for r in cur.fetchall()]


def build_active_projects_section(contents):
    """Return markdown ## Active Projects section, or None if empty."""
    items = [_first_substantive_line(c)[:200] for c in contents if c.strip()]
    items = [i for i in items if i]
    if not items:
        return None
    return "## Active Projects\n" + "\n".join(f"- {item}" for item in items)


def query_tooling(conn):
    """Return signal rows tagged commands, files, or workflow."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT content, tags FROM memories "
            "WHERE 'source:signals' = ANY(tags) AND deleted_at IS NULL "
            "AND (tags && ARRAY['commands', 'files', 'workflow']) "
            "ORDER BY created_at DESC LIMIT 10"
        )
        return cur.fetchall()


def _extract_after_colon(text):
    """Return text after the first colon, stripped."""
    if ":" in text:
        return text.split(":", 1)[1].strip().rstrip(".")
    return text.strip()


def build_tooling_section(rows):
    """Return markdown ## Tooling section, or None if empty."""
    commands_line = files_line = workflow_line = None
    for row in rows:
        tags = row["tags"] or []
        first_line = row["content"].split("\n")[0]
        if "commands" in tags and commands_line is None:
            commands_line = _extract_after_colon(first_line)
        elif "files" in tags and files_line is None:
            files_line = _extract_after_colon(first_line)
        elif "workflow" in tags and workflow_line is None:
            # Extract the part after "tool categories —" if present
            after_colon = _extract_after_colon(first_line)
            if "tool categories" in after_colon:
                workflow_line = after_colon.split("tool categories")[-1].lstrip(" —").strip()
            else:
                workflow_line = after_colon

    lines = []
    if commands_line:
        lines.append(f"- **Common commands:** {commands_line}")
    if files_line:
        lines.append(f"- **Frequent files:** {files_line}")
    if workflow_line:
        lines.append(f"- **Workflow:** {workflow_line}")
    if not lines:
        return None
    return "## Tooling\n" + "\n".join(lines)


def render_profile(sections):
    """Assemble the full user.md content from a list of section strings (None entries skipped)."""
    today = date.today().isoformat()
    header = f"# User Profile\n_Generated {today} · auto-updated every 30 min · do not edit_\n"
    body_parts = [s for s in sections if s]
    if not body_parts:
        return header + "\n_No memories found yet. Save some memories first._\n"
    return header + "\n" + "\n\n".join(body_parts) + "\n"


def patch_claude_md(path=None):
    """Prepend User Profile section to CLAUDE.md if not already present. Idempotent."""
    target = Path(path) if path is not None else CLAUDE_MD_PATH
    if target.exists():
        existing = target.read_text(encoding="utf-8")
        if CLAUDE_MD_MARKER in existing:
            return
        new_content = CLAUDE_MD_SECTION + existing
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        new_content = CLAUDE_MD_SECTION
    target.write_text(new_content, encoding="utf-8")
    log.info("Patched %s with User Profile section", target)


def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    return conn


def main():
    parser = argparse.ArgumentParser(description="Generate ~/.claude/user.md from distilled memories")
    parser.add_argument("--dry-run", action="store_true", help="Print to stdout instead of writing file")
    parser.add_argument("--output", default=str(OUTPUT_PATH), help=f"Output path (default: {OUTPUT_PATH})")
    parser.add_argument(
        "--patch-claude-md",
        action="store_true",
        help=(
            "Prepend a '## User Profile' reference section to ~/.claude/CLAUDE.md. "
            "Off by default — CLAUDE.md is only modified when this flag is explicitly passed. "
            "Review user.md output (--dry-run) before using this flag."
        ),
    )
    args = parser.parse_args()

    log.info("generate_user_profile starting (dry_run=%s)", args.dry_run)

    conn = get_db()
    try:
        projects, stack    = query_identity(conn)
        preferences        = query_preferences(conn)
        working_style      = query_working_style(conn)
        active_projects    = query_active_projects(conn)
        tooling_rows       = query_tooling(conn)
    finally:
        conn.close()

    sections = [
        build_identity_section(projects, stack),
        build_preferences_section(preferences),
        build_working_style_section(working_style),
        build_active_projects_section(active_projects),
        build_tooling_section(tooling_rows),
    ]

    content = render_profile(sections)

    if args.dry_run:
        print(content)
        return

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")
    log.info("Written %d bytes to %s", len(content), output_path)

    # H4: CLAUDE.md write requires explicit opt-in — never runs by default
    if args.patch_claude_md:
        patch_claude_md()
        log.info("Done (CLAUDE.md patched)")
    else:
        log.info("Done (CLAUDE.md not modified — pass --patch-claude-md to update it)")


if __name__ == "__main__":
    main()
