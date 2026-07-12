#!/usr/bin/env python3
"""
Distill raw Claude Code session messages into durable knowledge memories.
Uses a local Ollama LLM — no API key required.

Usage:
  python distill_sessions.py                      # distill all pending sessions
  python distill_sessions.py --project osint      # filter by project
  python distill_sessions.py --dry-run            # preview without writing
  python distill_sessions.py --workers 4          # parallel sessions (default: 4)
  python distill_sessions.py --model llama3.2:3b  # model override
"""

import argparse
import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

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
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("distill")

# Silence third-party INFO chatter that drowns out real per-session progress.
# httpx logs every HTTP request (one per LLM call), and sentence-transformers
# emits a model-load chunk on import. Both are noise at the user level — keep
# WARNING+ so genuine problems still surface.
for noisy in ("httpx", "httpcore", "openai", "urllib3", "sentence_transformers"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


def progress(msg):
    """User-facing progress line — bypasses LOGLEVEL filtering so it stays
    visible at any -Verbosity. Use this for per-session heartbeats, section
    headers, and run summaries. Reserve log.info() for diagnostic events
    that the user may want suppressed at low verbosity."""
    import time
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')}  {msg}", flush=True)

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://claude:memory_pass@localhost:5432/memory")
# Default points at the host-side published port (11737). When invoked via `docker compose run`,
# callers must pass -e OLLAMA_URL=http://ollama:11434/v1 to reach the in-stack service.
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11737/v1")
DEFAULT_MODEL = os.environ.get("DISTILL_MODEL", "qwen2.5:7b")
DEFAULT_WORKERS = int(os.environ.get("DISTILL_WORKERS", "4"))
MAX_TRANSCRIPT_CHARS = 80_000  # ~20k tokens
DISTILL_FAILURE_CAP = 3       # sessions that fail this many times are permanently skipped
MIN_MESSAGE_COUNT = 5         # sessions with fewer messages are skipped as non-substantive
DISTILL_DEDUP_THRESHOLD = 0.85  # cosine similarity above which a new memory is skipped as near-duplicate

DISTILL_PROMPT = """\
You are extracting durable knowledge from a Claude Code session transcript.

## Part A — Knowledge extraction
Identify every reusable fact, decision, preference, bug fix, discovered pattern, or \
architectural insight. Ignore greetings, navigation commands, file listings, and \
ephemeral details (e.g. "let me check X").

## Part B — Behavioral extraction (high bar — omit if nothing qualifies)
Include a behavioral memory ONLY when you observe a pattern that meets ALL of these:
1. Specific to this developer — not true of most developers
2. Supported by at least two instances in the transcript, OR an explicit statement/correction
3. Actionable — would change how Claude collaborates with them in a future session

Self-check before including: "Could this describe ANY developer? Would a senior engineer find this obvious?" If yes to either, skip it.

Do NOT capture:
- One-off actions (typed /exit, said thanks, gave a single terse reply)
- Generic habits (breaks work into steps, iterates on features, checks output)
- Neutral observations (used git, ran tests, opened a file, checked Docker)
- Session bookkeeping (asked what we were working on, reconnected MCP)

Strong examples that DO qualify:
- "This developer explicitly rejects post-task summaries and interrupts them mid-response — observed in multiple sessions. Skip trailing recaps."
- "The user always opens a feature branch before starting work and self-corrects when they forget — confirmed across 3+ sessions."
- "This developer uses brew for all Python installs and rejected pip install twice when Claude suggested it."

Tag behavioral memories with "type:behavior".

Return ONLY a JSON array. Each element must have:
- "content": a self-contained memory (2-4 sentences). Write as if the reader has zero context. \
  For behavioral memories, start with "The user..." or "This developer...".
- "tags": list of 2-6 lowercase tags. Use "type:behavior" for behavioral observations, \
  "preference" for explicit preferences, "decision" for architectural choices, \
  "bug" or "fix" for defect resolutions.

Example output:
[
  {{"content": "All Python package installation on this Mac uses brew (e.g. 'brew install pytest'), not pip install directly. The system Python on this Mac is managed by Homebrew; direct pip installs are blocked by PEP 668.", "tags": ["preference", "python", "brew", "macos"]}},
  {{"content": "FastMCP.run() does not accept host/port kwargs — they must be passed to the FastMCP() constructor instead. Passing them to .run() raises a TypeError at startup.", "tags": ["bug", "fastmcp", "pattern", "server"]}},
  {{"content": "The user consistently opens feature branches before starting any work and pushes a PR when the feature is complete, never committing directly to main. They catch this themselves when reminded mid-session.", "tags": ["type:behavior", "preference", "git", "workflow"]}},
  {{"content": "This developer prefers terse Claude responses — they skip summaries and ask for direct diffs rather than explanations. They interrupted two summaries in this session.", "tags": ["type:behavior", "preference", "communication"]}}
]

If nothing durable was learned in Part A AND no behavioral patterns were observable, return: []

Project: {project}
Session ID: {session_id}

Transcript:
{transcript}"""

_embed_lock = threading.Lock()


def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    register_vector(conn)
    return conn


def embed_batch(texts, embedder):
    """Batch-embed a list of texts. Thread-safe via lock."""
    with _embed_lock:
        return embedder.encode(texts, normalize_embeddings=True, batch_size=64, show_progress_bar=False)


def filter_near_dupes(conn, contents, vectors, session_prefix, threshold=DISTILL_DEDUP_THRESHOLD):
    """Return (contents, vectors) with near-duplicates of existing memories removed."""
    keep_contents, keep_vectors = [], []
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        for content, vector in zip(contents, vectors):
            cur.execute(
                "SELECT id, content, ROUND((1 - (embedding <=> %s::vector))::numeric, 4) AS sim "
                "FROM memories WHERE deleted_at IS NULL "
                "ORDER BY embedding <=> %s::vector LIMIT 1",
                (vector, vector),
            )
            row = cur.fetchone()
            if row and row["sim"] >= threshold:
                log.info(
                    "  [%s] dedup skip (sim=%.3f vs #%d): %s...",
                    session_prefix, row["sim"], row["id"], content[:60],
                )
            else:
                keep_contents.append(content)
                keep_vectors.append(vector)
    return keep_contents, keep_vectors


def get_pending_sessions(conn, project_filter=None):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        if project_filter:
            cur.execute(
                "SELECT session_id, project, message_count, distill_failures FROM imported_sessions "
                "WHERE distilled = FALSE AND distill_failures < %s AND message_count >= %s AND project ILIKE %s ORDER BY imported_at",
                (DISTILL_FAILURE_CAP, MIN_MESSAGE_COUNT, f"%{project_filter}%",)
            )
        else:
            cur.execute(
                "SELECT session_id, project, message_count, distill_failures FROM imported_sessions "
                "WHERE distilled = FALSE AND distill_failures < %s AND message_count >= %s ORDER BY imported_at",
                (DISTILL_FAILURE_CAP, MIN_MESSAGE_COUNT,)
            )
        return cur.fetchall()


def get_raw_messages(conn, session_id_prefix):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT id, content FROM memories WHERE source = %s ORDER BY created_at",
            (f"claude-code/{session_id_prefix}",)
        )
        return cur.fetchall()


def build_transcript(messages):
    parts = [msg["content"].strip() for msg in messages if msg["content"].strip()]
    transcript = "\n\n---\n\n".join(parts)
    if len(transcript) > MAX_TRANSCRIPT_CHARS:
        transcript = transcript[:MAX_TRANSCRIPT_CHARS] + "\n\n[transcript truncated]"
    return transcript


def call_ollama(client, model, project, session_id, transcript):
    prompt = DISTILL_PROMPT.format(project=project, session_id=session_id, transcript=transcript)
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=4096,
    )
    return response.choices[0].message.content


# Bounds for validated memory items. The distillation prompt asks for
# 2-4 sentence content and 2-6 short lowercase tags; these caps reject items
# that fall well outside that contract (which can happen if the model glitches
# or if a malicious transcript tries to inject an oversized payload).
_MAX_CONTENT_CHARS = 4000
_MAX_TAGS = 16
_MAX_TAG_LEN = 80


def _valid_memory_item(item):
    """Return True if item conforms to the {content, tags?} schema.

    Defence-in-depth against prompt-injected output from distillation: a
    malicious transcript could instruct the model to emit JSON, but the items
    still have to pass this filter before they reach the database. Invalid
    items are dropped rather than crashing the whole session.
    """
    if not isinstance(item, dict):
        return False
    content = item.get("content")
    if not isinstance(content, str):
        return False
    stripped = content.strip()
    if not stripped or len(stripped) > _MAX_CONTENT_CHARS:
        return False
    tags = item.get("tags", [])
    if not isinstance(tags, list) or len(tags) > _MAX_TAGS:
        return False
    for t in tags:
        if not isinstance(t, str) or not t or len(t) > _MAX_TAG_LEN:
            return False
    return True


def parse_distilled(response_text):
    """Extract a JSON array of validated memory items from an LLM response.

    Uses json.JSONDecoder.raw_decode() to locate and parse the first valid
    JSON array of objects, stopping exactly at the array's closing bracket.
    This correctly handles:
    - Trailing text after the array, even when it contains brackets such as
      "[1] memory extracted" or "stored in [memories] table" (fixes the
      rfind(']') bug that caused persistent JSONDecodeError on 4 sessions)
    - Markdown code fences (```json...```) — find('[') skips the fence prefix
    - Leading prose containing bracket chars like "[transcript]" — the loop
      advances past each failed parse attempt to find the real array

    Each item is then schema-validated via _valid_memory_item — non-conforming
    items are dropped (with a log entry) so prompt-injected or malformed
    output cannot reach the database.
    """
    text = response_text.strip()
    pos = 0
    decoder = json.JSONDecoder()
    raw = None
    while True:
        start = text.find("[", pos)
        if start == -1:
            return []
        try:
            value, _ = decoder.raw_decode(text, start)
            if isinstance(value, list) and (not value or isinstance(value[0], dict)):
                raw = value
                break
        except json.JSONDecodeError:
            pass
        pos = start + 1

    if not raw:
        return []
    valid = [item for item in raw if _valid_memory_item(item)]
    dropped = len(raw) - len(valid)
    if dropped:
        log.warning("Dropped %d invalid memory item(s) from LLM output", dropped)
    return valid


def _increment_failures(conn, session_id, session_prefix):
    """Increment distill_failures counter. Warns when the cap is reached."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE imported_sessions SET distill_failures = distill_failures + 1 "
                "WHERE session_id = %s RETURNING distill_failures",
                (session_id,)
            )
            row = cur.fetchone()
        conn.commit()
        if row and row[0] >= DISTILL_FAILURE_CAP:
            log.warning(
                "  [%s] Failure cap reached (%d/%d) — session will be permanently skipped",
                session_prefix, row[0], DISTILL_FAILURE_CAP,
            )
    except Exception as e:
        log.error("  [%s] Failed to increment distill_failures: %s", session_prefix, e)
        conn.rollback()


def distill_session(embedder, client, model, session, dry_run=False):
    """Process one session. Opens its own DB connection for thread safety."""
    session_id = session["session_id"]
    project = session["project"] or "unknown"
    session_prefix = session_id[:8]

    conn = get_db()
    try:
        raw_messages = get_raw_messages(conn, session_prefix)
        if not raw_messages:
            progress(f"  [{session_prefix}] No raw messages — marking distilled")
            if not dry_run:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE imported_sessions SET distilled = TRUE WHERE session_id = %s",
                        (session_id,)
                    )
                conn.commit()
            return 0

        transcript = build_transcript(raw_messages)
        progress(f"  [{session_prefix}] Calling {model} ({len(transcript)} chars, {len(raw_messages)} messages)...")

        try:
            response = call_ollama(client, model, project, session_prefix, transcript)
            memories = parse_distilled(response)
        except json.JSONDecodeError as e:
            # Not a true ERROR — graceful handling: increment failure-cap, keep raws,
            # session will be retried on next run (unless cap reached). WARNING so the
            # event is visible without implying user action is needed.
            log.warning("  [%s] JSON parse error: %s — keeping raws", session_prefix, e)
            _increment_failures(conn, session_id, session_prefix)
            return 0
        except Exception as e:
            log.warning("  [%s] LLM error: %s — keeping raws", session_prefix, e)
            _increment_failures(conn, session_id, session_prefix)
            return 0

        progress(f"  [{session_prefix}] → {len(memories)} memories extracted")

        if dry_run:
            for i, m in enumerate(memories, 1):
                log.info("    [%d] %s | tags: %s", i, m["content"][:100], m.get("tags", []))
            return len(memories)

        if not memories:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM memories WHERE source = %s", (f"claude-code/{session_prefix}",))
                cur.execute(
                    "UPDATE imported_sessions SET distilled = TRUE WHERE session_id = %s",
                    (session_id,)
                )
            conn.commit()
            return 0

        # Batch embed all contents at once — much faster than one at a time
        valid = [(m["content"].strip(), m.get("tags", [])) for m in memories if isinstance(m, dict) and m.get("content", "").strip()]
        if not valid:
            return 0

        contents, all_tags = zip(*valid)
        vectors = embed_batch(list(contents), embedder)

        # Drop new memories that are near-duplicates of what's already stored
        deduped_contents, deduped_vectors = filter_near_dupes(conn, list(contents), list(vectors), session_prefix)
        if not deduped_contents:
            progress(f"  [{session_prefix}] All memories were near-dupes — nothing new to store")
            with conn.cursor() as cur:
                cur.execute("DELETE FROM memories WHERE source = %s", (f"claude-code/{session_prefix}",))
                cur.execute("UPDATE imported_sessions SET distilled = TRUE WHERE session_id = %s", (session_id,))
            conn.commit()
            return 0

        # Rebuild all_tags aligned to deduped contents
        tag_by_content = dict(zip(contents, all_tags))
        deduped_tags = [tag_by_content[c] for c in deduped_contents]

        rows = []
        for content, item_tags, vector in zip(deduped_contents, deduped_tags, deduped_vectors):
            tags = ["distilled", f"project:{project}"] + [t for t in item_tags if t != "distilled"]
            rows.append((content, tags, f"distilled/{session_prefix}", project, vector))

        try:
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(
                    cur,
                    "INSERT INTO memories (content, tags, source, project, embedding) VALUES %s "
                    "ON CONFLICT (content_hash) DO NOTHING",
                    rows,
                )
                cur.execute("DELETE FROM memories WHERE source = %s", (f"claude-code/{session_prefix}",))
                cur.execute(
                    "UPDATE imported_sessions SET distilled = TRUE WHERE session_id = %s",
                    (session_id,)
                )
            conn.commit()
            progress(f"  [{session_prefix}] Done: {len(rows)} memories stored")
            return len(rows)
        except psycopg2.Error as e:
            conn.rollback()
            log.error("  [%s] DB error: %s — keeping raws", session_prefix, e)
            return 0
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Distill Claude Code sessions into curated memories via local LLM")
    parser.add_argument("--project", help="Filter to sessions from this project")
    parser.add_argument("--dry-run", action="store_true", help="Preview extractions without writing to DB")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                        help=f"Parallel sessions (default: {DEFAULT_WORKERS})")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help=f"Ollama model (default: {DEFAULT_MODEL})")
    parser.add_argument("--ollama-url", default=OLLAMA_URL,
                        help=f"Ollama base URL (default: {OLLAMA_URL})")
    parser.add_argument("--reset-failures", metavar="SESSION_ID", nargs="?", const="ALL",
                        help="Reset distill_failures to 0 so capped sessions can be retried. "
                             "Pass a session ID to reset one session, or omit the value to reset all capped sessions.")
    args = parser.parse_args()

    if args.reset_failures is not None:
        conn = get_db()
        with conn.cursor() as cur:
            if args.reset_failures == "ALL":
                cur.execute(
                    "UPDATE imported_sessions SET distill_failures = 0 WHERE distill_failures >= %s",
                    (DISTILL_FAILURE_CAP,)
                )
                log.info("Reset distill_failures for %d capped session(s)", cur.rowcount)
            else:
                cur.execute(
                    "UPDATE imported_sessions SET distill_failures = 0 WHERE session_id = %s",
                    (args.reset_failures,)
                )
                if cur.rowcount:
                    log.info("Reset distill_failures for session %s", args.reset_failures)
                else:
                    log.warning("Session not found: %s", args.reset_failures)
        conn.commit()
        conn.close()
        return

    log.info("Loading embedding model...")
    embedder = SentenceTransformer("all-mpnet-base-v2")

    client = OpenAI(base_url=args.ollama_url, api_key="ollama")

    conn = get_db()
    sessions = get_pending_sessions(conn, args.project)
    conn.close()

    if not sessions:
        progress("No pending sessions to distill.")
        return

    mode = "[DRY RUN] " if args.dry_run else ""
    progress(f"{mode}=== Distilling {len(sessions)} session(s) | workers={args.workers} | model={args.model} ===")

    total = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(distill_session, embedder, client, args.model, s, args.dry_run): s
            for s in sessions
        }
        for future in as_completed(futures):
            s = futures[future]
            try:
                total += future.result()
            except Exception as e:
                log.error("Session %s failed: %s", s["session_id"][:8], e)

    progress(f"{mode}Done. {total} distilled memories {'would be ' if args.dry_run else ''}stored.")


if __name__ == "__main__":
    main()
