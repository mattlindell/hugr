#!/usr/bin/env python3
"""
Behavioral re-pass: run targeted behavioral extraction over distilled sessions,
reading transcripts from the original JSONL files (raw messages are deleted after distillation).

Usage:
  python behavioral_pass.py               # all distilled sessions
  python behavioral_pass.py --dry-run     # preview without writing
  python behavioral_pass.py --project workspace
  python behavioral_pass.py --force       # re-run even if behavioral memories exist
"""
import argparse
import json
import logging
import os
from pathlib import Path

import psycopg2
import psycopg2.extras
from openai import OpenAI
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
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("behavioral_pass")

# Silence third-party INFO chatter that drowns out real per-session progress.
# httpx logs every HTTP request (one per LLM call), and sentence-transformers
# emits a model-load chunk on import. Both are noise at the user level — keep
# WARNING+ so genuine problems still surface.
for noisy in ("httpx", "httpcore", "openai", "urllib3", "sentence_transformers"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


def progress(msg):
    """User-facing progress line — bypasses LOGLEVEL filtering so it stays
    visible at any -Verbosity. Use this for per-session heartbeats and run
    summaries. Reserve log.info() for diagnostic events that the user may
    want suppressed at low verbosity."""
    import time
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')}  {msg}", flush=True)

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://claude:memory_pass@localhost:5432/memory")
# Default points at the host-side published port (11737). When invoked via `docker compose run`,
# callers must pass -e OLLAMA_URL=http://ollama:11434/v1 to reach the in-stack service.
OLLAMA_URL   = os.environ.get("OLLAMA_URL", "http://localhost:11737/v1")
MODEL        = os.environ.get("DISTILL_MODEL", "qwen2.5:7b")
MAX_CHARS    = 20_000
MIN_MESSAGE_COUNT = 10  # sessions with fewer messages can't show behavioral patterns
CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"

BEHAVIORAL_PROMPT = """\
Analyze this Claude Code session. Extract behavioral observations about HOW the user works.

High bar — only include a memory if ALL three conditions are met:
1. Specific to this developer (not true of most developers)
2. Supported by at least two instances in the transcript OR an explicit statement/correction
3. Actionable — would change how Claude collaborates with them in a future session

Self-check: "Could this describe ANY developer? Would a senior engineer find this obvious?"
If yes to either, skip it.

Do NOT capture:
- One-off actions (typed /exit, gave a single terse reply, said thanks)
- Generic habits (breaks tasks into steps, iterates, checks output before committing)
- Neutral tool use (used git, ran tests, opened a file)
- Session bookkeeping (asked what we were working on, reconnected a service)

Strong examples that DO qualify:
- "This developer interrupts Claude's trailing summaries and has asked to skip them in multiple sessions."
- "The user consistently opens a feature branch before starting and self-corrects when they forget — seen 3+ times."
- "This developer uses brew for Python installs and rejected pip install twice when Claude suggested it."

Return ONLY a JSON array. Each element must cite specific evidence from the transcript.
{{"content": "The user... [2-3 sentences, cite specific evidence]", "tags": ["type:behavior", ...]}}

If no qualifying patterns are observable, return: []

Project: {project}

Transcript:
{transcript}"""


def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    register_vector(conn)
    return conn


def extract_text(content):
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "\n".join(b["text"] for b in content if isinstance(b, dict) and b.get("type") == "text").strip()
    return ""


def find_jsonl(session_id):
    """Locate the JSONL file for a given session_id across all project directories."""
    if not CLAUDE_PROJECTS_DIR.exists():
        return None
    for proj_dir in CLAUDE_PROJECTS_DIR.iterdir():
        if not proj_dir.is_dir():
            continue
        candidate = proj_dir / f"{session_id}.jsonl"
        if candidate.exists():
            return candidate
    return None


def build_transcript_from_jsonl(path, min_length=30):
    messages = []
    with open(path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("type") not in ("user", "assistant"):
                continue
            msg = record.get("message", {})
            role = msg.get("role")
            if role not in ("user", "assistant"):
                continue
            text = extract_text(msg.get("content", ""))
            if len(text) >= min_length:
                messages.append(f"[{role.upper()}]\n{text}")
    transcript = "\n\n---\n\n".join(messages)
    if len(transcript) > MAX_CHARS:
        transcript = transcript[:MAX_CHARS] + "\n\n[truncated]"
    return transcript


def already_has_behavioral(conn, session_prefix):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM memories WHERE source = %s LIMIT 1",
            (f"behavioral/{session_prefix}",)
        )
        return cur.fetchone() is not None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--project", default=None)
    parser.add_argument("--force", action="store_true", help="Re-run even if behavioral memories already exist")
    args = parser.parse_args()

    log.info("Loading embedding model...")
    embedder = SentenceTransformer("all-mpnet-base-v2")
    client   = OpenAI(base_url=OLLAMA_URL, api_key="ollama")

    conn = get_db()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        if args.project:
            cur.execute(
                "SELECT session_id, project, message_count FROM imported_sessions "
                "WHERE distilled = TRUE AND message_count >= %s AND project ILIKE %s ORDER BY imported_at",
                (MIN_MESSAGE_COUNT, f"%{args.project}%",)
            )
        else:
            cur.execute(
                "SELECT session_id, project, message_count FROM imported_sessions "
                "WHERE distilled = TRUE AND message_count >= %s ORDER BY imported_at",
                (MIN_MESSAGE_COUNT,)
            )
        sessions = cur.fetchall()

    progress(f"Found {len(sessions)} distilled sessions")
    total_written = 0

    for session in sessions:
        session_id = session["session_id"]
        project    = session["project"] or "unknown"
        prefix     = session_id[:8]

        if not args.force and already_has_behavioral(conn, prefix):
            progress(f"  [{prefix}] already processed — skip (--force to redo)")
            continue

        jsonl_path = find_jsonl(session_id)
        if jsonl_path is None:
            progress(f"  [{prefix}] JSONL not found on disk — skipping")
            continue

        transcript = build_transcript_from_jsonl(jsonl_path)
        if not transcript.strip():
            progress(f"  [{prefix}] empty transcript — skipping")
            continue

        progress(f"  [{prefix}] {project} — {len(transcript)} chars, calling {MODEL}...")

        prompt = BEHAVIORAL_PROMPT.format(project=project, session_id=prefix, transcript=transcript)
        try:
            response = client.chat.completions.create(
                model=MODEL,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=2048,
            )
            raw_text = response.choices[0].message.content.strip()
            start, end = raw_text.find("["), raw_text.rfind("]")
            if start == -1 or end == -1:
                progress(f"  [{prefix}] no JSON array in response — skipping")
                continue
            memories = json.loads(raw_text[start:end + 1])
        except Exception as e:
            # Not a true ERROR — graceful continue; LLM may return non-JSON occasionally.
            # WARNING so the event is visible without implying user action is needed.
            log.warning("  [%s] LLM/parse error: %s", prefix, e)
            continue

        progress(f"  [{prefix}] → {len(memories)} behavioral observations")

        if args.dry_run:
            for m in memories:
                log.info("    • [%s] %s", m.get("tags", []), m.get("content", "")[:120])
            continue

        if not memories:
            continue

        valid = [(m["content"].strip(), m.get("tags", [])) for m in memories if m.get("content", "").strip()]
        if not valid:
            continue

        contents, all_tags = zip(*valid)
        vectors = embedder.encode(list(contents), normalize_embeddings=True, batch_size=32, show_progress_bar=False)

        with conn.cursor() as cur:
            for content, tags, vector in zip(contents, all_tags, vectors):
                final_tags = ["distilled", "type:behavior", f"project:{project}"]
                for t in tags:
                    if t not in final_tags:
                        final_tags.append(t)
                cur.execute(
                    "INSERT INTO memories (content, tags, source, project, embedding) "
                    "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (content_hash) DO NOTHING",
                    (content, final_tags, f"behavioral/{prefix}", project, vector)
                )
        conn.commit()
        total_written += len(valid)
        progress(f"  [{prefix}] wrote {len(valid)} behavioral memories")

    conn.close()
    progress(f"Done — {total_written} total behavioral memories written")


if __name__ == "__main__":
    main()
