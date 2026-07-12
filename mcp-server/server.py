import os, json, logging, threading, time, inspect, traceback
from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime, timezone

import psycopg2, psycopg2.extras, psycopg2.pool
from pgvector.psycopg2 import register_vector
from sentence_transformers import SentenceTransformer
from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse, HTMLResponse
import pathlib

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("claude-memory")

mcp = FastMCP("claude-memory", host="0.0.0.0", port=3333)
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://claude:memory_pass@localhost:5432/memory")

# ── Write guard thresholds ─────────────────────────────────────────────────────
GUARD_NOOP_THRESHOLD   = float(os.environ.get("GUARD_NOOP_THRESHOLD",   "0.85"))  # identical meaning → skip
GUARD_UPDATE_THRESHOLD = float(os.environ.get("GUARD_UPDATE_THRESHOLD", "0.75"))  # very similar → suggest update instead

# Mirrors distill_sessions.py MIN_MESSAGE_COUNT so get_stats reports the same
# "pending" set the distiller will actually process. Sessions below this are
# reported separately as below_min_messages, not as pending_distill.
DISTILL_MIN_MESSAGES = int(os.environ.get("DISTILL_MIN_MESSAGES", "5"))
# Mirrors distill_sessions.py DISTILL_FAILURE_CAP (hardcoded 3 there). Sessions
# at/above this failure count are reported as capped, not pending.
DISTILL_FAILURE_CAP = 3

# ── Search cache ───────────────────────────────────────────────────────────────
CACHE_MAX_SIZE    = 500   # max entries across all queries
CACHE_TTL_SECONDS = 600   # 10 minutes

_search_cache: OrderedDict = OrderedDict()  # key → (result_str, monotonic_timestamp)
_cache_lock = threading.Lock()

log.info("Loading embedding model...")
embedder = SentenceTransformer("all-mpnet-base-v2")
log.info("Connecting to database...")

# Register the pgvector type once per physical connection — at connection
# creation rather than on every checkout — so MCP tool calls don't each pay a
# SELECT round-trip to look up the vector type OID.
#
# Guarded with inspect.isclass because the test suite replaces psycopg2 with a
# MagicMock, which cannot be subclassed; under that mocked import we fall back
# to the (also-mocked) base pool class.
if inspect.isclass(psycopg2.pool.ThreadedConnectionPool):
    class _VectorPool(psycopg2.pool.ThreadedConnectionPool):
        def _connect(self, key=None):
            conn = super()._connect(key)
            register_vector(conn)
            return conn
    _PoolClass = _VectorPool
else:  # pragma: no cover — only hit under the mocked test import
    _PoolClass = psycopg2.pool.ThreadedConnectionPool

_pool = _PoolClass(1, 5, DATABASE_URL)
log.info("Ready.")

_DB_UNREACHABLE = (
    "Database unreachable. Ensure the stack is running (`docker compose up -d`). "
    "If the mcp-server container was just recreated, reconnect this Claude Code "
    "session: /exit, then `claude --continue`."
)
_DB_BUSY = (
    "All database connections are busy (pool exhausted). Retry in a moment; "
    "if this persists, check for stuck long-running queries."
)


@contextmanager
def db_conn():
    """Check out a live pooled connection, recovering from stale ones.

    pgvector registration happens at connection creation (see _VectorPool), so
    there is no per-checkout SELECT. A connection found dead at checkout is
    transparently replaced; one that dies mid-use raises a clear DB-unreachable
    error instead of the misleading -32602 it used to surface.
    """
    conn = None
    for _ in range(2):
        try:
            candidate = _pool.getconn()
        except psycopg2.pool.PoolError as e:
            raise RuntimeError(_DB_BUSY) from e
        if candidate.closed:
            _pool.putconn(candidate, close=True)
            continue
        try:
            candidate.rollback()
        except psycopg2.OperationalError:
            _pool.putconn(candidate, close=True)
            continue
        conn = candidate
        break
    if conn is None:
        raise RuntimeError(_DB_UNREACHABLE)

    broken = False
    try:
        yield conn
    except psycopg2.OperationalError as e:
        broken = True
        log.error("db_conn lost its connection: %s", e)
        raise RuntimeError(_DB_UNREACHABLE) from e
    finally:
        if broken:
            _pool.putconn(conn, close=True)
        else:
            try:
                conn.rollback()
            except psycopg2.OperationalError:
                _pool.putconn(conn, close=True)
            else:
                _pool.putconn(conn)


def embed(text):
    return embedder.encode(text, normalize_embeddings=True, show_progress_bar=False)


def _parse_dt(value: str, name: str):
    """Parse an ISO date/datetime string. Returns (datetime, None) or (None, error_str)."""
    if not value:
        return None, None
    try:
        return datetime.fromisoformat(value), None
    except ValueError:
        return None, f"❌ Invalid {name} date '{value}'. Use ISO format: YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS"


# ── Search cache helpers ───────────────────────────────────────────────────────

def _cache_get(key: tuple):
    """Return cached result string, or None if missing or expired."""
    with _cache_lock:
        if key not in _search_cache:
            return None
        result, ts = _search_cache[key]
        if time.monotonic() - ts > CACHE_TTL_SECONDS:
            del _search_cache[key]
            return None
        _search_cache.move_to_end(key)  # mark as recently used
        return result


def _cache_set(key: tuple, result: str):
    """Store result in cache, evicting the oldest entry if at capacity."""
    with _cache_lock:
        if key in _search_cache:
            _search_cache.move_to_end(key)
        _search_cache[key] = (result, time.monotonic())
        while len(_search_cache) > CACHE_MAX_SIZE:
            _search_cache.popitem(last=False)  # evict LRU


def _cache_invalidate():
    """Clear all cached search results. Call after any write operation."""
    with _cache_lock:
        _search_cache.clear()
    log.debug("Search cache invalidated")


# ── Health check endpoint ──────────────────────────────────────────────────────

@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> JSONResponse:
    """Liveness/readiness probe. Returns 200 OK when healthy, 503 when DB is unreachable."""
    db_ok = False
    db_error = None
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        db_ok = True
    except Exception as e:
        db_error = str(e)

    payload = {
        "status": "ok" if db_ok else "degraded",
        "db": "ok" if db_ok else "error",
        "db_error": db_error,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    return JSONResponse(payload, status_code=200 if db_ok else 503)


@mcp.custom_route("/cache/invalidate", methods=["POST"])
async def cache_invalidate_endpoint(request: Request) -> JSONResponse:
    """Force-clear the in-process search cache.
    Call this after running import_memories.py or distill_sessions.py so results
    reflect newly written memories without waiting for the 10-minute TTL."""
    _cache_invalidate()
    log.info("Search cache cleared via HTTP endpoint")
    return JSONResponse({"status": "ok", "message": "Search cache cleared"})


# ── REST API helpers (called by HTTP route handlers below) ────────────────────

def _api_projects() -> list:
    """Distinct projects with memory counts, ordered by count desc."""
    with db_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT project, COUNT(*) AS count FROM memories "
                "WHERE deleted_at IS NULL GROUP BY project ORDER BY count DESC"
            )
            return [dict(r) for r in cur.fetchall()]


def _api_tags() -> list:
    """All tags with counts across active memories, ordered by count desc."""
    with db_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT tag, COUNT(*) AS count FROM memories, unnest(tags) AS tag "
                "WHERE deleted_at IS NULL GROUP BY tag ORDER BY count DESC"
            )
            return [dict(r) for r in cur.fetchall()]


def _api_stats() -> dict:
    """Aggregate stats: counts, project count, and estimated storage.

    Storage estimate: 3072 bytes/embedding (768 floats × 4 bytes) + avg content length + 200 bytes metadata overhead. Not from pg_relation_size — treat as approximate."""
    with db_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT "
                "  COUNT(*) FILTER (WHERE deleted_at IS NULL) AS active, "
                "  COUNT(*) FILTER (WHERE deleted_at IS NOT NULL) AS deleted, "
                "  COUNT(DISTINCT project) FILTER (WHERE deleted_at IS NULL) AS projects, "
                "  COALESCE(AVG(LENGTH(content)) FILTER (WHERE deleted_at IS NULL), 0)::int AS avg_content_len "
                "FROM memories"
            )
            row = dict(cur.fetchone())
    # Rough storage estimate: each embedding is 768 floats × 4 bytes = 3072 bytes
    # Plus avg content length. Multiply by active memory count.
    active = row["active"] or 0
    embedding_bytes = active * 3072
    content_bytes = active * (row["avg_content_len"] or 0)
    metadata_bytes = active * 200  # tags, timestamps, id overhead estimate
    total_bytes = embedding_bytes + content_bytes + metadata_bytes
    row["storage_mb"] = round(total_bytes / 1_048_576, 1)
    row["storage_breakdown"] = {
        "embeddings_mb": round(embedding_bytes / 1_048_576, 1),
        "content_mb": round(content_bytes / 1_048_576, 1),
        "metadata_mb": round(metadata_bytes / 1_048_576, 1),
    }
    return row


def _api_list_memories(project: str = None, tag: str = None,
                        since: str = None, before: str = None,
                        limit: int = 50, offset: int = 0) -> list:
    """Paginated list of active memories with derived 'title' and 'content_length' fields."""
    conditions = ["m.deleted_at IS NULL"]
    params = []
    if project:
        conditions.append("m.project = %s")
        params.append(project)
    if tag:
        conditions.append("%s = ANY(m.tags)")
        params.append(tag)
    since_dt, err = _parse_dt(since, "since")
    if err:
        raise ValueError(err)
    before_dt, err = _parse_dt(before, "before")
    if err:
        raise ValueError(err)
    if since_dt:
        conditions.append("m.created_at >= %s")
        params.append(since_dt)
    if before_dt:
        conditions.append("m.created_at < %s")
        params.append(before_dt)
    where = " AND ".join(conditions)
    params.extend([limit, offset])
    with db_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"SELECT id, content, tags, source, project, created_at, updated_at "
                f"FROM memories m WHERE {where} "
                f"ORDER BY created_at DESC LIMIT %s OFFSET %s",
                params
            )
            rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        c = r["content"]
        r["title"] = (c[:72] + "…") if len(c) > 72 else c
        r["content_length"] = len(c)
        if r.get("created_at"):
            r["created_at"] = r["created_at"].isoformat() if hasattr(r["created_at"], "isoformat") else r["created_at"]
        if r.get("updated_at"):
            r["updated_at"] = r["updated_at"].isoformat() if hasattr(r["updated_at"], "isoformat") else r["updated_at"]
    return rows


def _api_get_memory(memory_id: int) -> dict | None:
    """Fetch single memory by id. Returns None if not found."""
    with db_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, content, tags, source, project, created_at, updated_at, deleted_at "
                "FROM memories WHERE id = %s",
                (memory_id,)
            )
            row = cur.fetchone()
    if not row:
        return None
    r = dict(row)
    r["title"] = (r["content"][:72] + "…") if len(r["content"]) > 72 else r["content"]
    r["content_length"] = len(r["content"])
    for f in ("created_at", "updated_at", "deleted_at"):
        if r.get(f) and hasattr(r[f], "isoformat"):
            r[f] = r[f].isoformat()
    return r


def _api_related_memories(memory_id: int, limit: int = 3) -> list:
    """Return up to `limit` nearest-neighbor memories to the given memory id."""
    source = _api_get_memory(memory_id)
    if not source:
        return []
    vec = embed(source["content"])
    with db_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "WITH q AS (SELECT %s::vector AS vec) "
                "SELECT id, content, tags, project, created_at, "
                "ROUND((1 - (embedding <=> q.vec))::numeric, 4) AS sim "
                "FROM memories, q WHERE deleted_at IS NULL AND id != %s "
                "ORDER BY embedding <=> q.vec LIMIT %s",
                (vec, memory_id, limit)
            )
            rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        r["title"] = (r["content"][:72] + "…") if len(r["content"]) > 72 else r["content"]
        if r.get("created_at") and hasattr(r["created_at"], "isoformat"):
            r["created_at"] = r["created_at"].isoformat()
        r["sim"] = float(r["sim"])
    return rows


def _api_recall(query: str, threshold: float = 0.78, limit: int = 20) -> list:
    """Semantic search. Returns ranked list with score and snippet."""
    if not query.strip():
        return []
    vec = embed(query)
    with db_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "WITH q AS (SELECT %s::vector AS vec) "
                "SELECT id, content, tags, project, created_at, "
                "ROUND((1 - (embedding <=> q.vec))::numeric, 4) AS sim "
                "FROM memories, q WHERE deleted_at IS NULL "
                "AND (1 - (embedding <=> q.vec)) >= %s "
                "ORDER BY embedding <=> q.vec LIMIT %s",
                (vec, threshold, limit)
            )
            rows = [dict(r) for r in cur.fetchall()]
    results = []
    for r in rows:
        title = (r["content"][:72] + "…") if len(r["content"]) > 72 else r["content"]
        snippet = r["content"][:200]
        if r.get("created_at") and hasattr(r["created_at"], "isoformat"):
            r["created_at"] = r["created_at"].isoformat()
        results.append({
            "id": r["id"],
            "title": title,
            "snippet": snippet,
            "tags": r["tags"],
            "project": r["project"],
            "created_at": r["created_at"],
            "sim": float(r["sim"]),
        })
    return results


def _api_preferences() -> list:
    """Return behavioral preferences grouped by category.

    Sources (in display order):
      1. type:preference — explicit written preferences (auto-memory imports)
      2. type:pattern + source:signals — mechanical behavioral signals (extract_signals.py)
      3. distilled + (preference|decision|type:behavior) — implicit patterns from session distillation
    """
    from datetime import datetime, timezone
    with db_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, content, tags, project, created_at, updated_at "
                "FROM memories WHERE deleted_at IS NULL "
                "AND ("
                "  'type:preference' = ANY(tags) "
                "  OR ('type:pattern' = ANY(tags) AND 'source:signals' = ANY(tags)) "
                "  OR ('distilled' = ANY(tags) AND ("
                "        'preference' = ANY(tags) OR 'decision' = ANY(tags)"
                "        OR 'type:behavior' = ANY(tags)"
                "  ))"
                ") "
                "ORDER BY updated_at DESC"
            )
            rows = [dict(r) for r in cur.fetchall()]
    now = datetime.now(timezone.utc)
    groups: dict[str, list] = {}
    for r in rows:
        tags = r["tags"] or []
        # Determine display category
        if "type:preference" in tags and "source:auto-memory" in tags:
            cat = next((t.split("category:")[1] for t in tags if t.startswith("category:")), "explicit")
        elif "source:signals" in tags:
            cat = "signals"
        elif "distilled" in tags:
            cat = next((t.split("category:")[1] for t in tags if t.startswith("category:")), "inferred")
        else:
            cat = next((t.split("category:")[1] for t in tags if t.startswith("category:")), "general")

        updated = r["updated_at"]
        if isinstance(updated, str):
            try:
                updated = datetime.fromisoformat(updated)
            except ValueError:
                updated = None
        if updated is not None and hasattr(updated, "tzinfo") and updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        age_days = (now - updated).days if updated is not None else 999
        confidence = 0.95 if age_days <= 7 else (0.80 if age_days <= 30 else 0.65)
        source_tag = next((t for t in tags if t.startswith("source:")), "")
        source = source_tag.replace("source:", "") if source_tag else r["project"] or "unknown"
        item = {"text": r["content"], "confidence": confidence, "source": source}
        groups.setdefault(cat, []).append(item)

    # Fixed display order: explicit → signals → inferred → everything else
    ORDER = ["explicit", "feedback", "signals", "inferred"]
    ordered = {k: groups[k] for k in ORDER if k in groups}
    ordered.update({k: v for k, v in groups.items() if k not in ORDER})
    return [{"category": cat, "items": items} for cat, items in ordered.items()]


def _api_bulk_delete(project: str = None, tag: str = None, dry_run: bool = True) -> dict:
    """Soft-delete memories matching project and/or tag filter."""
    if not project and not tag:
        return {"error": "At least one filter (project or tag) is required"}
    conditions = ["deleted_at IS NULL"]
    params = []
    if project:
        conditions.append("project = %s")
        params.append(project)
    if tag:
        conditions.append("%s = ANY(tags)")
        params.append(tag)
    where = " AND ".join(conditions)
    with db_conn() as conn:
        with conn.cursor() as cur:
            if dry_run:
                cur.execute(f"SELECT COUNT(*) FROM memories WHERE {where}", params)
                count = cur.fetchone()[0]
            else:
                cur.execute(f"UPDATE memories SET deleted_at = NOW() WHERE {where}", params)
                count = cur.rowcount
                conn.commit()
                _cache_invalidate()
    return {"deleted": count, "dry_run": dry_run, "project": project, "tag": tag}


# ── Write guard ────────────────────────────────────────────────────────────────

def _write_guard(content: str, vector) -> dict:
    """Check what save_memory would do without writing.
    Returns action=ADD|UPDATE|NOOP with similarity and nearest-match details."""
    try:
        with db_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "WITH q AS (SELECT %s::vector AS vec) "
                    "SELECT id, content, ROUND((1 - (embedding <=> q.vec))::numeric, 4) AS sim "
                    "FROM memories, q WHERE deleted_at IS NULL AND embedding IS NOT NULL "
                    "ORDER BY embedding <=> q.vec LIMIT 1",
                    (vector,)
                )
                row = cur.fetchone()
        if not row:
            return {"action": "ADD", "reason": "No memories exist yet"}
        sim = float(row["sim"])
        if sim >= GUARD_NOOP_THRESHOLD:
            return {"action": "NOOP", "similarity": sim, "target_id": row["id"],
                    "target_preview": row["content"][:120],
                    "reason": f"Near-duplicate at similarity {sim} — would be skipped"}
        elif sim >= GUARD_UPDATE_THRESHOLD:
            return {"action": "UPDATE", "similarity": sim, "target_id": row["id"],
                    "target_preview": row["content"][:120],
                    "reason": f"Similar memory (ID {row['id']}, similarity {sim}) — consider update_memory instead"}
        else:
            return {"action": "ADD", "similarity": sim,
                    "reason": f"Nearest match is {sim} — below thresholds, would be saved as new memory"}
    except Exception as e:
        log.error("_write_guard failed: %s", e)
        return {"action": "ADD", "reason": f"Guard check failed ({e}) — defaulting to ADD"}


# ── MCP tools ─────────────────────────────────────────────────────────────────

@mcp.tool()
def save_memory(content: str, tags: list[str] = [], source: str = "claude-code", project: str = "", force: bool = False) -> str:
    """Save a thought, request, note, or piece of information to persistent memory.

    On a near-duplicate, returns a NOOP message listing all three paths:
      - NOOP: skip (default; the existing memory already captures this)
      - UPDATE: call update_memory(<id>, content=..., tags=...) to merge new detail
      - ADD: call save_memory again with force=True to save as a separate memory
    """
    vector = embed(content)
    try:
        with db_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                if not force:
                    # CTE binds vector once: pgvector's psycopg2 adapter doesn't safely
                    # reuse a single vector across multiple %s placeholders in one call.
                    cur.execute(
                        "WITH q AS (SELECT %s::vector AS vec) "
                        "SELECT id, content, ROUND((1 - (embedding <=> q.vec))::numeric, 4) AS sim "
                        "FROM memories, q WHERE (1 - (embedding <=> q.vec)) >= %s AND deleted_at IS NULL "
                        "ORDER BY embedding <=> q.vec LIMIT 1",
                        (vector, GUARD_NOOP_THRESHOLD)
                    )
                    dup = cur.fetchone()
                    if dup:
                        return (
                            f"⚠️ NOOP — near-duplicate of memory ID {dup['id']} "
                            f"(similarity {dup['sim']}): {dup['content'][:80]}...\n"
                            f"Three options:\n"
                            f"  • NOOP   — skip saving (the existing memory already captures this)\n"
                            f"  • UPDATE — call update_memory({dup['id']}, content=..., tags=...) to merge new detail into the existing memory\n"
                            f"  • ADD    — call save_memory again with force=True to save this as a separate memory anyway"
                        )
                # ON CONFLICT on content_hash handles exact-duplicate races atomically.
                # If the conflicting row was soft-deleted, un-delete it (restore).
                # If it's an active row the DO UPDATE WHERE is false → RETURNING returns nothing.
                cur.execute(
                    "INSERT INTO memories (content, tags, source, project, embedding) "
                    "VALUES (%s, %s, %s, %s, %s) "
                    "ON CONFLICT (content_hash) DO UPDATE "
                    "  SET deleted_at = NULL, updated_at = NOW() "
                    "  WHERE memories.deleted_at IS NOT NULL "
                    "RETURNING id, created_at, deleted_at",
                    (content, tags, source, project, vector)
                )
                row = cur.fetchone()
            conn.commit()
        if row is None:
            return "Duplicate (exact match already stored)."
        _cache_invalidate()
        log.info("Memory saved id=%s project=%s", row['id'], project or "(none)")
        return f"✅ Memory saved (ID: {row['id']}, created: {row['created_at']})"
    except Exception as e:
        log.error("save_memory failed: %s", e)
        return f"❌ Error: {e}"


@mcp.tool()
def check_memory(content: str) -> str:
    """Dry-run check: see what save_memory would do without actually writing.
    Returns ADD (new memory), UPDATE (similar exists — consider update_memory),
    or NOOP (near-duplicate — would be skipped)."""
    vector = embed(content)
    result = _write_guard(content, vector)
    return json.dumps(result, indent=2)


@mcp.tool()
def semantic_search(query: str, limit: int = 10, min_similarity: float = 0.3,
                    project: str = None, since: str = None, before: str = None) -> str:
    """Search memories by MEANING using vector similarity. Filter by project, since, or before (ISO dates)."""
    since_dt, err = _parse_dt(since, "since")
    if err:
        return err
    before_dt, err = _parse_dt(before, "before")
    if err:
        return err

    cache_key = ("semantic", query, limit, min_similarity, project or "", since or "", before or "")
    cached = _cache_get(cache_key)
    if cached is not None:
        log.debug("semantic_search cache hit query=%r", query)
        return cached

    vector = embed(query)
    try:
        with db_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # CTE binds vector once: pgvector's psycopg2 adapter doesn't safely
                # reuse a single vector across multiple %s placeholders in one call.
                conditions = ["embedding IS NOT NULL", "deleted_at IS NULL", "(1 - (embedding <=> q.vec)) >= %s"]
                cond_params = [min_similarity]
                if project:
                    conditions.append("project = %s")
                    cond_params.append(project)
                if since_dt:
                    conditions.append("created_at >= %s")
                    cond_params.append(since_dt)
                if before_dt:
                    conditions.append("created_at < %s")
                    cond_params.append(before_dt)

                sql = (
                    "WITH q AS (SELECT %s::vector AS vec) "
                    "SELECT id, content, tags, source, project, created_at, "
                    "ROUND((1 - (embedding <=> q.vec))::numeric, 4) AS similarity "
                    f"FROM memories, q WHERE {' AND '.join(conditions)} "
                    "ORDER BY embedding <=> q.vec LIMIT %s"
                )
                params = [vector] + cond_params + [limit]
                cur.execute(sql, params)
                rows = cur.fetchall()
        result = json.dumps([dict(r) for r in rows], indent=2, default=str) if rows else f"No similar memories found for: '{query}'"
        _cache_set(cache_key, result)
        return result
    except Exception as e:
        log.error("semantic_search failed: %s", e)
        return f"❌ Error: {e}"


@mcp.tool()
def search_memories(query: str, limit: int = 10, project: str = None,
                    since: str = None, before: str = None) -> str:
    """Search memories by exact keyword or phrase. Filter by project, since, or before (ISO dates)."""
    since_dt, err = _parse_dt(since, "since")
    if err:
        return err
    before_dt, err = _parse_dt(before, "before")
    if err:
        return err

    cache_key = ("keyword", query, limit, project or "", since or "", before or "")
    cached = _cache_get(cache_key)
    if cached is not None:
        log.debug("search_memories cache hit query=%r", query)
        return cached

    try:
        with db_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                conditions = [
                    "deleted_at IS NULL",
                    "(to_tsvector('english', content) @@ plainto_tsquery('english', %s) OR content ILIKE %s)",
                ]
                params = [query, f"%{query}%"]
                if project:
                    conditions.append("project = %s")
                    params.append(project)
                if since_dt:
                    conditions.append("created_at >= %s")
                    params.append(since_dt)
                if before_dt:
                    conditions.append("created_at < %s")
                    params.append(before_dt)
                params.append(limit)
                sql = (
                    "SELECT id, content, tags, source, project, created_at FROM memories "
                    f"WHERE {' AND '.join(conditions)} ORDER BY created_at DESC LIMIT %s"
                )
                cur.execute(sql, params)
                rows = cur.fetchall()
        result = json.dumps([dict(r) for r in rows], indent=2, default=str) if rows else f"No memories found for: '{query}'"
        _cache_set(cache_key, result)
        return result
    except Exception as e:
        log.error("search_memories failed: %s", e)
        return f"❌ Error: {e}"


@mcp.tool()
def hybrid_search(query: str, limit: int = 10, keyword_weight: float = 0.7,
                  semantic_weight: float = 0.3, min_semantic_similarity: float = 0.1,
                  project: str = None, since: str = None, before: str = None) -> str:
    """Search memories by combining keyword (ts_rank) and semantic (vector) scores.
    keyword_weight + semantic_weight must equal 1.0 (defaults: 0.7 / 0.3).
    Each result includes keyword_score, semantic_score, and hybrid_score."""
    if abs(keyword_weight + semantic_weight - 1.0) > 1e-6:
        return "❌ keyword_weight + semantic_weight must equal 1.0"
    if keyword_weight < 0 or semantic_weight < 0:
        return "❌ weights must be non-negative"

    since_dt, err = _parse_dt(since, "since")
    if err:
        return err
    before_dt, err = _parse_dt(before, "before")
    if err:
        return err

    vector = embed(query)

    extra_conditions = []
    extra_params = []
    if project:
        extra_conditions.append("project = %s")
        extra_params.append(project)
    if since_dt:
        extra_conditions.append("created_at >= %s")
        extra_params.append(since_dt)
    if before_dt:
        extra_conditions.append("created_at < %s")
        extra_params.append(before_dt)

    extra_where = (" AND " + " AND ".join(extra_conditions)) if extra_conditions else ""

    # CTE q binds vector once: pgvector's psycopg2 adapter doesn't safely
    # reuse a single vector across multiple %s placeholders in one call.
    sql = f"""
WITH q AS (SELECT %s::vector AS vec),
candidates AS (
  SELECT id, content, tags, source, project, created_at,
    COALESCE(ts_rank(to_tsvector('english', content), plainto_tsquery('english', %s)), 0) AS kw_score,
    CASE WHEN embedding IS NOT NULL
         THEN GREATEST(1 - (embedding <=> q.vec), 0)
         ELSE 0 END AS sem_score
  FROM memories, q
  WHERE deleted_at IS NULL
    AND (
      to_tsvector('english', content) @@ plainto_tsquery('english', %s)
      OR content ILIKE %s
      OR (embedding IS NOT NULL AND (1 - (embedding <=> q.vec)) >= %s)
    )
    {extra_where}
)
SELECT id, content, tags, source, project, created_at,
  ROUND(kw_score::numeric, 4)  AS keyword_score,
  ROUND(sem_score::numeric, 4) AS semantic_score,
  ROUND((%s * kw_score + %s * sem_score)::numeric, 4) AS hybrid_score
FROM candidates
ORDER BY hybrid_score DESC
LIMIT %s
"""
    # params order: vector (WITH q), kw_score ts_rank query, WHERE ts @@ query,
    #               WHERE ILIKE, WHERE sem >= threshold, [extra], kw_weight, sem_weight, limit
    params = [vector, query, query, f"%{query}%", min_semantic_similarity] + extra_params + [keyword_weight, semantic_weight, limit]

    try:
        with db_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
        if not rows:
            return f"No memories found for: '{query}'"
        return json.dumps([dict(r) for r in rows], indent=2, default=str)
    except Exception as e:
        log.error("hybrid_search failed: %s", e)
        return f"❌ Error: {e}"


@mcp.tool()
def list_memories(limit: int = 20, offset: int = 0, tag: str = None, project: str = None,
                  since: str = None, before: str = None) -> str:
    """List recent memories with pagination. Returns rows plus total count matching the filters.
    Use offset to page through results (e.g. offset=20 for page 2 with limit=20).
    Optionally filter by tag, project, and/or date range (ISO dates)."""
    since_dt, err = _parse_dt(since, "since")
    if err:
        return err
    before_dt, err = _parse_dt(before, "before")
    if err:
        return err

    try:
        with db_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                conditions = ["deleted_at IS NULL"]
                params = []
                if tag:
                    conditions.append("%s = ANY(tags)")
                    params.append(tag)
                if project:
                    conditions.append("project = %s")
                    params.append(project)
                if since_dt:
                    conditions.append("created_at >= %s")
                    params.append(since_dt)
                if before_dt:
                    conditions.append("created_at < %s")
                    params.append(before_dt)
                where = ' AND '.join(conditions)
                # Count total matching rows, then fetch the page
                cur.execute(f"SELECT COUNT(*) FROM memories WHERE {where}", params)
                total = cur.fetchone()["count"]
                cur.execute(
                    f"SELECT id, content, tags, source, project, created_at FROM memories "
                    f"WHERE {where} ORDER BY created_at DESC LIMIT %s OFFSET %s",
                    params + [limit, offset]
                )
                rows = cur.fetchall()
        if not rows and offset == 0:
            return "No memories stored yet."
        return json.dumps({
            "total": total,
            "limit": limit,
            "offset": offset,
            "memories": [dict(r) for r in rows],
        }, indent=2, default=str)
    except Exception as e:
        log.error("list_memories failed: %s", e)
        return f"❌ Error: {e}"


@mcp.tool()
def get_memory(memory_id: int) -> str:
    """Fetch a single memory by ID with full content. Returns the memory even if soft-deleted (deleted_at will be set)."""
    try:
        with db_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT id, content, tags, source, project, created_at, updated_at, deleted_at FROM memories WHERE id = %s",
                    (memory_id,)
                )
                row = cur.fetchone()
        return json.dumps(dict(row), indent=2, default=str) if row else f"❌ No memory with ID {memory_id}"
    except Exception as e:
        log.error("get_memory id=%s failed: %s", memory_id, e)
        return f"❌ Error: {e}"


@mcp.tool()
def recent_context(project: str = None, limit: int = 10) -> str:
    """Return recent distilled memories — ideal for session start context recall.
    Falls back to the most recent non-distilled memories if no distilled ones exist yet.
    Filter by project for focused recall."""
    try:
        with db_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                project_cond = "AND project = %s " if project else ""
                project_param = (project,) if project else ()
                cur.execute(
                    "SELECT id, content, tags, source, project, created_at FROM memories "
                    f"WHERE 'distilled' = ANY(tags) AND deleted_at IS NULL {project_cond}"
                    "ORDER BY created_at DESC LIMIT %s",
                    project_param + (limit,)
                )
                rows = cur.fetchall()
                if not rows:
                    # Fallback: most recent active memories regardless of distilled tag
                    cur.execute(
                        "SELECT id, content, tags, source, project, created_at FROM memories "
                        f"WHERE deleted_at IS NULL {project_cond}"
                        "ORDER BY created_at DESC LIMIT %s",
                        project_param + (limit,)
                    )
                    rows = cur.fetchall()
                    if rows:
                        log.info("recent_context: no distilled memories, returning %d recent memories", len(rows))
        if not rows:
            return "No memories found. Save some memories first."
        result = [dict(r) for r in rows]
        distilled = all("distilled" in (r.get("tags") or []) for r in result)
        return json.dumps({
            "distilled": distilled,
            "memories": result,
        }, indent=2, default=str)
    except Exception as e:
        log.error("recent_context failed: %s", e)
        return f"❌ Error: {e}"


@mcp.tool()
def startup_context(project: str = None) -> str:
    """Return a compact session-start context block — call this at the start of every session.
    Combines behavioral signals (workflow patterns, command habits, file hotspots, preferences)
    with recent distilled memories into a single snapshot, no search query required.
    Optionally filter by project name."""
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        header = f"## Memory context [{project or 'all projects'}] — {today}\n"
        sections = []

        with db_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:

                # ── Behavioral signals ────────────────────────────────────────
                project_cond = "AND project = %s " if project else ""
                project_param = (project,) if project else ()

                cur.execute(
                    "SELECT content, tags FROM memories "
                    f"WHERE 'source:signals' = ANY(tags) AND deleted_at IS NULL {project_cond}"
                    "ORDER BY created_at DESC LIMIT 20",
                    project_param,
                )
                signal_rows = cur.fetchall()

                patterns = [r["content"] for r in signal_rows if "type:pattern" in (r["tags"] or [])]
                preferences = [r["content"] for r in signal_rows if "type:preference" in (r["tags"] or [])]

                if patterns:
                    # Truncate each pattern to a single sentence for compactness
                    compact = []
                    for p in patterns[:3]:
                        first_sentence = p.split(". ")[0] + "."
                        compact.append(f"  • {first_sentence}")
                    sections.append("**Patterns:**\n" + "\n".join(compact))

                if preferences:
                    pref_lines = [f"  • {p[:120]}" for p in preferences[:3]]
                    sections.append("**Preferences:**\n" + "\n".join(pref_lines))

                # ── Recent distilled memories ─────────────────────────────────
                cur.execute(
                    "SELECT content, created_at FROM memories "
                    f"WHERE 'distilled' = ANY(tags) AND deleted_at IS NULL {project_cond}"
                    "ORDER BY created_at DESC LIMIT 5",
                    project_param,
                )
                distilled_rows = cur.fetchall()

                if not distilled_rows:
                    # Fallback: most recent active memories
                    cur.execute(
                        "SELECT content, created_at FROM memories "
                        f"WHERE deleted_at IS NULL {project_cond}"
                        "ORDER BY created_at DESC LIMIT 5",
                        project_param,
                    )
                    distilled_rows = cur.fetchall()

                if distilled_rows:
                    lines = []
                    for r in distilled_rows:
                        text = r["content"][:120].rstrip()
                        if len(r["content"]) > 120:
                            text += "…"
                        lines.append(f"  • {text}")
                    sections.append("**Recent work:**\n" + "\n".join(lines))

        if not sections:
            return f"{header}\nNo context found for project '{project}'. Save some memories first."

        return header + "\n" + "\n\n".join(sections)

    except Exception as e:
        log.error("startup_context failed: %s", e)
        return f"❌ Error: {e}"


@mcp.tool()
def update_memory(memory_id: int, content: str = None, tags: list[str] = None, force: bool = False) -> str:
    """Update content and/or tags. Re-embeds if content changes.
    Returns a warning (without saving) if new content is above GUARD_NOOP_THRESHOLD similar to an existing memory.
    Pass force=True to bypass the duplicate check and save anyway."""
    if not content and tags is None:
        return "❌ Provide at least one of: content, tags"
    try:
        with db_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                if content and not force:
                    new_vector = embed(content)
                    # Check for near-duplicates, excluding the memory being updated and deleted memories
                    cur.execute(
                        "WITH q AS (SELECT %s::vector AS vec) "
                        "SELECT id, content, ROUND((1 - (embedding <=> q.vec))::numeric, 4) AS sim "
                        "FROM memories, q "
                        "WHERE (1 - (embedding <=> q.vec)) >= %s "
                        "AND id != %s "
                        "AND deleted_at IS NULL "
                        "ORDER BY embedding <=> q.vec LIMIT 1",
                        (new_vector, GUARD_NOOP_THRESHOLD, memory_id)
                    )
                    dup = cur.fetchone()
                    if dup:
                        return (
                            f"⚠️ Near-duplicate detected: memory ID {dup['id']} "
                            f"(similarity {dup['sim']}): {dup['content'][:80]}...\n"
                            f"Update not saved. Call update_memory again with force=True to override."
                        )

            with conn.cursor() as cur:
                if content:
                    new_vector = embed(content)
                    if tags is not None:
                        cur.execute(
                            "UPDATE memories SET content=%s, tags=%s, embedding=%s WHERE id=%s AND deleted_at IS NULL",
                            (content, tags, new_vector, memory_id)
                        )
                    else:
                        cur.execute(
                            "UPDATE memories SET content=%s, embedding=%s WHERE id=%s AND deleted_at IS NULL",
                            (content, new_vector, memory_id)
                        )
                else:
                    cur.execute(
                        "UPDATE memories SET tags=%s WHERE id=%s AND deleted_at IS NULL",
                        (tags, memory_id)
                    )
                updated = cur.rowcount
                if not updated:
                    cur.execute("SELECT deleted_at FROM memories WHERE id=%s", (memory_id,))
                    exists = cur.fetchone()
            conn.commit()
        if updated:
            _cache_invalidate()
            log.info("Memory updated id=%s", memory_id)
            return f"✅ Memory {memory_id} updated."
        elif exists is None:
            return f"❌ Memory ID {memory_id} not found."
        else:
            return (f"❌ Memory {memory_id} is soft-deleted — use restore_memory({memory_id}) "
                    f"to restore it first, then update.")
    except Exception as e:
        log.error("update_memory id=%s failed: %s\n%s", memory_id, e, traceback.format_exc())
        return f"❌ Error: {e}"


@mcp.tool()
def delete_memory(memory_id: int) -> str:
    """Soft-delete a memory by ID. The memory is hidden but not permanently removed.
    Use restore_memory to undo, or purge_memory to permanently delete."""
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE memories SET deleted_at = NOW() WHERE id = %s AND deleted_at IS NULL",
                    (memory_id,)
                )
                deleted = cur.rowcount
            conn.commit()
        if deleted:
            _cache_invalidate()
        log.info("Memory soft-deleted id=%s", memory_id)
        return f"✅ Memory {memory_id} deleted." if deleted else f"❌ No active memory with ID {memory_id}"
    except Exception as e:
        log.error("delete_memory id=%s failed: %s", memory_id, e)
        return f"❌ Error: {e}"


@mcp.tool()
def restore_memory(memory_id: int) -> str:
    """Restore a previously soft-deleted memory, making it active again."""
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE memories SET deleted_at = NULL WHERE id = %s AND deleted_at IS NOT NULL",
                    (memory_id,)
                )
                restored = cur.rowcount
            conn.commit()
        if restored:
            _cache_invalidate()
        log.info("Memory restored id=%s", memory_id)
        return f"✅ Memory {memory_id} restored." if restored else f"❌ No deleted memory with ID {memory_id}"
    except Exception as e:
        log.error("restore_memory id=%s failed: %s", memory_id, e)
        return f"❌ Error: {e}"


@mcp.tool()
def purge_memory(memory_id: int) -> str:
    """Permanently delete a memory. The memory must already be soft-deleted (call delete_memory first).
    This is irreversible — the row is removed from the database."""
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM memories WHERE id = %s AND deleted_at IS NOT NULL",
                    (memory_id,)
                )
                purged = cur.rowcount
            conn.commit()
        if purged:
            _cache_invalidate()
        log.info("Memory purged id=%s", memory_id)
        return (
            f"✅ Memory {memory_id} permanently purged."
            if purged else
            f"❌ Memory {memory_id} not found or not soft-deleted (call delete_memory first)"
        )
    except Exception as e:
        log.error("purge_memory id=%s failed: %s", memory_id, e)
        return f"❌ Error: {e}"


@mcp.tool()
def list_tags() -> str:
    """List all unique tags with counts."""
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT tag, COUNT(*) AS count FROM memories, unnest(tags) AS tag "
                    "WHERE deleted_at IS NULL GROUP BY tag ORDER BY count DESC"
                )
                rows = cur.fetchall()
        return json.dumps([{"tag": r[0], "count": r[1]} for r in rows], indent=2) if rows else "No tags found."
    except Exception as e:
        log.error("list_tags failed: %s", e)
        return f"❌ Error: {e}"


@mcp.tool()
def get_stats() -> str:
    """Return memory counts broken down by project and source, plus session import status."""
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                # Single CTE covers all memory aggregates in one round-trip
                cur.execute("""
                    WITH mem AS (
                        SELECT
                            COUNT(*) FILTER (WHERE deleted_at IS NULL)     AS total,
                            COUNT(*) FILTER (WHERE deleted_at IS NOT NULL) AS deleted
                        FROM memories
                    ),
                    by_proj AS (
                        SELECT COALESCE(NULLIF(project,''), '(none)') AS project, COUNT(*) AS cnt
                        FROM memories WHERE deleted_at IS NULL
                        GROUP BY project ORDER BY cnt DESC
                    ),
                    by_src AS (
                        SELECT source, COUNT(*) AS cnt
                        FROM memories WHERE deleted_at IS NULL
                        GROUP BY source ORDER BY cnt DESC LIMIT 10
                    ),
                    sess AS (
                        SELECT
                            COUNT(*)                                    AS total,
                            COUNT(*) FILTER (WHERE distilled = TRUE)    AS distilled,
                            COUNT(*) FILTER (WHERE distill_failures >= %s) AS capped,
                            COUNT(*) FILTER (
                                WHERE distilled = FALSE
                                  AND distill_failures < %s
                                  AND message_count < %s
                            )                                           AS below_min
                        FROM imported_sessions
                    )
                    SELECT
                        (SELECT total    FROM mem)      AS total_memories,
                        (SELECT deleted  FROM mem)      AS deleted_memories,
                        (SELECT json_agg(row_to_json(by_proj)) FROM by_proj) AS by_project,
                        (SELECT json_agg(row_to_json(by_src))  FROM by_src)  AS by_source,
                        (SELECT total    FROM sess)     AS sessions_total,
                        (SELECT distilled FROM sess)    AS sessions_distilled,
                        (SELECT capped   FROM sess)     AS sessions_capped,
                        (SELECT below_min FROM sess)    AS sessions_below_min
                """, (DISTILL_FAILURE_CAP, DISTILL_FAILURE_CAP, DISTILL_MIN_MESSAGES))
                row = cur.fetchone()
        total, deleted, by_project_json, by_source_json, s_total, s_distilled, s_capped, s_below_min = row
        with _cache_lock:
            cache_size = len(_search_cache)
        return json.dumps({
            "total_memories": total,
            "deleted_memories": deleted,
            "by_project": by_project_json or [],
            "top_sources": by_source_json or [],
            "sessions": {
                "total": s_total,
                "distilled": s_distilled,
                "capped": s_capped,
                "below_min_messages": s_below_min,
                "pending_distill": s_total - s_distilled - s_capped - s_below_min,
            },
            "search_cache": {
                "entries": cache_size,
                "max_size": CACHE_MAX_SIZE,
                "ttl_seconds": CACHE_TTL_SECONDS,
            }
        }, indent=2)
    except Exception as e:
        log.error("get_stats failed: %s", e)
        return f"❌ Error: {e}"


@mcp.tool()
def export_memories(project: str = None, tag: str = None, since: str = None,
                    before: str = None, output_format: str = "json") -> str:
    """Export memories as JSON or markdown. Filter by project, tag, and/or date range (ISO dates).
    output_format: 'json' (default) or 'markdown'."""
    since_dt, err = _parse_dt(since, "since")
    if err:
        return err
    before_dt, err = _parse_dt(before, "before")
    if err:
        return err
    if output_format not in ("json", "markdown"):
        return "❌ output_format must be 'json' or 'markdown'"

    try:
        with db_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                conditions = ["deleted_at IS NULL"]
                params = []
                if tag:
                    conditions.append("%s = ANY(tags)")
                    params.append(tag)
                if project:
                    conditions.append("project = %s")
                    params.append(project)
                if since_dt:
                    conditions.append("created_at >= %s")
                    params.append(since_dt)
                if before_dt:
                    conditions.append("created_at < %s")
                    params.append(before_dt)
                cur.execute(
                    f"SELECT id, content, tags, source, project, created_at, updated_at FROM memories WHERE {' AND '.join(conditions)} ORDER BY created_at ASC",
                    params
                )
                rows = cur.fetchall()

        if not rows:
            return "No memories found matching the given filters."

        records = [dict(r) for r in rows]
        log.info("Exporting %d memories format=%s", len(records), output_format)

        now = datetime.now(timezone.utc)
        if output_format == "json":
            return json.dumps({"exported_at": now.isoformat(), "count": len(records), "memories": records}, indent=2, default=str)

        # Markdown format. Memory section separator is `----` (4+ dashes) so that
        # any `---` line inside memory content does not break out of its section.
        # Inside content, any line that is purely `---`, `***`, or `___` would be
        # parsed by Markdown as a horizontal rule, so we escape such lines by
        # prefixing with a backslash (`\---`) which renders as the literal text.
        lines = [f"# Memory Export", f"*Exported: {now.strftime('%Y-%m-%d %H:%M UTC')} — {len(records)} memories*", ""]
        for r in records:
            lines.append(f"## [{r['id']}] {r['created_at']}")
            if r.get("project"):
                lines.append(f"**Project:** {r['project']}  **Source:** {r['source']}")
            lines.append(f"**Tags:** {', '.join(r['tags']) if r['tags'] else '(none)'}")
            lines.append("")
            content = r["content"] or ""
            escaped = "\n".join(
                ("\\" + ln) if ln.strip() in ("---", "***", "___") else ln
                for ln in content.split("\n")
            )
            lines.append(escaped)
            lines.append("")
            lines.append("----")
            lines.append("")
        return "\n".join(lines)

    except Exception as e:
        log.error("export_memories failed: %s", e)
        return f"❌ Error: {e}"


@mcp.tool()
def find_duplicates(threshold: float = 0.85, limit: int = 20, project: str = None,
                    scan_limit: int = 500) -> str:
    """Find near-duplicate memory pairs above a similarity threshold.
    Returns pairs ordered by similarity descending — useful for database hygiene after bulk imports.
    threshold: minimum cosine similarity to report (default 0.85; must be between 0.5 and 1.0)
    limit: max number of pairs to return (default 20)
    scan_limit: only consider the most recent N memories (default 500) to keep the
                self-join bounded. Increase for deeper scans on large databases."""
    if not (0.5 <= threshold <= 1.0):
        return "❌ threshold must be between 0.5 and 1.0"
    if scan_limit < 10:
        return "❌ scan_limit must be at least 10"

    try:
        with db_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                project_cond = "AND project = %s" if project else ""
                project_params = [project] if project else []
                # Bound the self-join to the most recent scan_limit rows so the
                # O(n²) comparison stays manageable on large databases.
                cur.execute(
                    f"""
                    WITH recent AS (
                        SELECT id, content, embedding, created_at
                        FROM memories
                        WHERE deleted_at IS NULL AND embedding IS NOT NULL
                        {project_cond}
                        ORDER BY created_at DESC
                        LIMIT %s
                    )
                    SELECT
                        a.id   AS id_a,
                        b.id   AS id_b,
                        ROUND((1 - (a.embedding <=> b.embedding))::numeric, 4) AS similarity,
                        LEFT(a.content, 120) AS content_a,
                        LEFT(b.content, 120) AS content_b,
                        a.created_at AS created_a,
                        b.created_at AS created_b
                    FROM recent a
                    JOIN recent b ON b.id > a.id
                    WHERE (1 - (a.embedding <=> b.embedding)) >= %s
                    ORDER BY similarity DESC
                    LIMIT %s
                    """,
                    project_params + [scan_limit, threshold, limit]
                )
                rows = cur.fetchall()
        if not rows:
            return f"No duplicate pairs found above similarity {threshold}."
        return json.dumps([dict(r) for r in rows], indent=2, default=str)
    except Exception as e:
        log.error("find_duplicates failed: %s", e)
        return f"❌ Error: {e}"


@mcp.tool()
def bulk_delete(tag: str = None, project: str = None, source: str = None,
                dry_run: bool = True) -> str:
    """Soft-delete multiple memories matching ALL supplied filters (tag, project, source).
    At least one filter is required. Defaults to dry_run=True — set dry_run=False to apply.
    Returns the count and a preview of affected memories."""
    if not any([tag, project, source]):
        return "❌ Provide at least one filter: tag, project, or source"

    conditions = ["deleted_at IS NULL"]
    params: list = []
    if tag:
        conditions.append("%s = ANY(tags)")
        params.append(tag)
    if project:
        conditions.append("project = %s")
        params.append(project)
    if source:
        conditions.append("source = %s")
        params.append(source)
    where = " AND ".join(conditions)

    try:
        with db_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    f"SELECT id, LEFT(content, 80) AS preview, tags, project, source "
                    f"FROM memories WHERE {where} ORDER BY created_at DESC LIMIT 50",
                    params
                )
                preview_rows = cur.fetchall()
                cur.execute(f"SELECT COUNT(*) FROM memories WHERE {where}", params)
                total = cur.fetchone()["count"]

            if not dry_run and total > 0:
                with conn.cursor() as cur:
                    cur.execute(
                        f"UPDATE memories SET deleted_at = NOW() WHERE {where}",
                        params
                    )
                conn.commit()
                _cache_invalidate()
                log.info("bulk_delete: soft-deleted %d memories tag=%s project=%s source=%s",
                         total, tag, project, source)

        action = "Would delete" if dry_run else "Deleted"
        note = " (dry_run=True — pass dry_run=False to apply)" if dry_run else ""
        return json.dumps({
            "action": action + note,
            "total": total,
            "preview": [dict(r) for r in preview_rows],
        }, indent=2, default=str)
    except Exception as e:
        log.error("bulk_delete failed: %s", e)
        return f"❌ Error: {e}"

# ── REST HTTP route handlers ───────────────────────────────────────────────────

@mcp.custom_route("/ui", methods=["GET"])
async def serve_ui(request: Request) -> HTMLResponse | JSONResponse:
    """Serve the single-file React UI."""
    ui_path = pathlib.Path(__file__).parent / "ui.html"
    if not ui_path.exists():
        return JSONResponse({"error": "ui.html not found"}, status_code=404)
    return HTMLResponse(ui_path.read_text(encoding="utf-8"))


@mcp.custom_route("/api/projects", methods=["GET"])
async def api_projects(request: Request) -> JSONResponse:
    try:
        return JSONResponse(_api_projects())
    except Exception as e:
        log.error("GET /api/projects failed: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/tags", methods=["GET"])
async def api_tags(request: Request) -> JSONResponse:
    try:
        return JSONResponse(_api_tags())
    except Exception as e:
        log.error("GET /api/tags failed: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/stats", methods=["GET"])
async def api_stats(request: Request) -> JSONResponse:
    try:
        return JSONResponse(_api_stats())
    except Exception as e:
        log.error("GET /api/stats failed: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/memories", methods=["GET"])
async def api_memories_list(request: Request) -> JSONResponse:
    try:
        q = request.query_params
        try:
            limit  = int(q.get("limit", 50))
            offset = int(q.get("offset", 0))
        except ValueError:
            return JSONResponse({"error": "limit and offset must be integers"}, status_code=400)
        rows = _api_list_memories(
            project=q.get("project"),
            tag=q.get("tag"),
            since=q.get("since"),
            before=q.get("before"),
            limit=limit,
            offset=offset,
        )
        return JSONResponse(rows)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        log.error("GET /api/memories failed: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/memories/{id}", methods=["GET"])
async def api_memory_get(request: Request) -> JSONResponse:
    try:
        memory_id = int(request.path_params["id"])
        row = _api_get_memory(memory_id)
        if row is None:
            return JSONResponse({"error": "Not found"}, status_code=404)
        return JSONResponse(row)
    except (ValueError, KeyError):
        return JSONResponse({"error": "Invalid id"}, status_code=400)
    except Exception as e:
        log.error("GET /api/memories/%s failed: %s", request.path_params.get("id"), e)
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/memories/{id}/related", methods=["GET"])
async def api_memory_related(request: Request) -> JSONResponse:
    try:
        memory_id = int(request.path_params["id"])
        limit = int(request.query_params.get("limit", 3))
        return JSONResponse(_api_related_memories(memory_id, limit=limit))
    except (ValueError, KeyError):
        return JSONResponse({"error": "Invalid id"}, status_code=400)
    except Exception as e:
        log.error("GET /api/memories/%s/related failed: %s", request.path_params.get("id"), e)
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/recall", methods=["POST"])
async def api_recall(request: Request) -> JSONResponse:
    try:
        body = await request.json()
        query = body.get("query", "")
        threshold = float(body.get("threshold", 0.78))
        return JSONResponse(_api_recall(query, threshold=threshold))
    except Exception as e:
        log.error("POST /api/recall failed: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/preferences", methods=["GET"])
async def api_preferences(request: Request) -> JSONResponse:
    try:
        return JSONResponse(_api_preferences())
    except Exception as e:
        log.error("GET /api/preferences failed: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/memories", methods=["DELETE"])
async def api_memories_delete(request: Request) -> JSONResponse:
    try:
        q = request.query_params
        project = q.get("project")
        tag = q.get("tag")
        # Safe-by-default: omitting dry_run param is a preview (dry_run=True).
        # Pass dry_run=false explicitly to perform the actual deletion.
        dry_run = q.get("dry_run", "true").lower() != "false"
        result = _api_bulk_delete(project=project, tag=tag, dry_run=dry_run)
        if "error" in result:
            return JSONResponse(result, status_code=400)
        return JSONResponse(result)
    except Exception as e:
        log.error("DELETE /api/memories failed: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


if __name__ == "__main__":
    mcp.run(transport="sse")
