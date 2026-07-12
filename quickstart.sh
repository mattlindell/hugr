#!/bin/zsh
# claude-memory quickstart — complete setup in one script
# Usage: bash quickstart.sh
#
# What this does:
#   1. Starts Docker services (PostgreSQL + ollama + MCP server)
#   2. Imports your existing Claude Code session history
#   3. Pulls the distillation model (qwen2.5:7b) into the in-stack ollama service
#   4. Distills sessions into durable memories via in-stack ollama
#   5. Extracts behavioral signals (workflow patterns, preferences) without an LLM
#   6. Registers the MCP server with Claude Code (user scope — all projects)
#   7. Optionally installs the auto-import LaunchAgent (every 30 min, macOS)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== claude-memory quickstart ==="
echo ""

# ── 1. Start services ──────────────────────────────────────────────────────────
echo "▶ Starting Docker services..."
docker compose up -d

echo "  Waiting for DB to be healthy..."
for i in {1..20}; do
  if docker compose exec -T db pg_isready -U claude -d memory &>/dev/null; then
    echo "  DB ready."
    break
  fi
  sleep 2
done

# ── 2. Import Claude Code session history ─────────────────────────────────────
if [[ -d "$HOME/.claude/projects" ]]; then
  echo ""
  echo "▶ Importing Claude Code session history..."
  docker compose run --rm -T \
    -v "$HOME/.claude/projects:/root/.claude/projects:ro" \
    -v "$SCRIPT_DIR/import_memories.py:/app/import_memories.py:ro" \
    mcp-server \
    python /app/import_memories.py --claude-code
else
  echo ""
  echo "  ~/.claude/projects not found — skipping session import."
fi

# ── 3. Pull distillation model ────────────────────────────────────────────────
echo ""
echo "▶ Waiting for in-stack ollama to be ready..."
for i in {1..30}; do
  if docker compose exec -T ollama ollama list &>/dev/null; then
    break
  fi
  if [[ $i -eq 30 ]]; then
    echo "ERROR: ollama did not become ready within 60 s — check 'docker compose logs ollama'" >&2
    exit 1
  fi
  sleep 2
done
echo "▶ Pulling distillation model into in-stack ollama (one-time, ~4.7 GB)..."
if ! docker compose exec -T ollama ollama list | grep -q 'qwen2.5:7b'; then
  docker compose exec -T ollama ollama pull qwen2.5:7b
else
  echo "  qwen2.5:7b already present — skipping pull."
fi

# ── 4. Distill sessions ────────────────────────────────────────────────────────
echo ""
echo "▶ Distilling sessions into durable memories (via in-stack ollama)..."
docker compose run --rm -T \
  -e OLLAMA_URL="http://ollama:11434/v1" \
  -v "$SCRIPT_DIR/distill_sessions.py:/app/distill_sessions.py:ro" \
  mcp-server \
  python /app/distill_sessions.py

# ── 5. Extract behavioral signals ─────────────────────────────────────────────
echo ""
echo "▶ Extracting behavioral signals (workflow patterns, preferences)..."
docker compose run --rm -T \
  -v "$HOME/.claude/projects:/root/.claude/projects:ro" \
  -v "$SCRIPT_DIR/extract_signals.py:/app/extract_signals.py:ro" \
  mcp-server \
  python /app/extract_signals.py || true

# ── 6. Register with Claude Code ───────────────────────────────────────────────
echo ""
echo "▶ Registering with Claude Code (user scope)..."
if claude mcp get claude-memory &>/dev/null 2>&1; then
  echo "  Already registered."
else
  claude mcp add --scope user --transport sse claude-memory http://localhost:3333/sse
  echo "  ✅ Registered."
fi

# ── 7. LaunchAgent (optional, macOS only) ─────────────────────────────────────
if [[ "$(uname)" == "Darwin" ]]; then
  echo ""
  printf "▶ Install auto-import LaunchAgent (runs every 30 min)? [y/N]: "
  read -r INSTALL_LA
  if [[ "${INSTALL_LA:-N}" =~ ^[Yy]$ ]]; then
    bash "$SCRIPT_DIR/setup-launchagent.sh"
  else
    echo "  Skipped. Run 'bash setup-launchagent.sh' any time to install it."
  fi
fi

# ── Done ───────────────────────────────────────────────────────────────────────
echo ""
echo "✅ claude-memory is ready."
echo ""
echo "  Web UI:   http://localhost:3333/ui"
echo ""
echo "  Start a new claude session and try:"
echo "    list_memories"
echo "    semantic_search \"your query here\""
echo "    get_stats"
echo ""
echo "  Backup:   bash backup.sh"
echo "  Restore:  bash restore.sh <dump_file>"
echo "  Logs:     docker compose logs -f mcp-server"
echo ""
echo "  ⭐  If this is useful: https://github.com/daringanitch/claude-memory"
