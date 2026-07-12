#!/bin/zsh
# Auto-import and distill Claude Code sessions into memory DB
# Run by LaunchAgent every 30 minutes
#
# Pipeline:
#   1. import_memories.py   — import new sessions from ~/.claude/projects
#   2. distill_sessions.py  — extract durable memories via Ollama
#   3. extract_signals.py   — behavioral signals without an LLM
#   4. behavioral_pass.py   — LLM behavioral extraction over distilled sessions

LOG=/tmp/claude-memory-import.log
ERR=/tmp/claude-memory-import-error.log

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Ensure services are running
DOCKER=$(which docker || echo /usr/local/bin/docker)

# Load API keys from ~/.claude/.env
set -a && source "$HOME/.claude/.env" && set +a

$DOCKER compose up -d >> "$ERR" 2>&1
sleep 8

# Step 1: Import new sessions (skips already-distilled sessions)
$DOCKER compose run --rm -T \
  -v "$HOME/.claude/projects:/root/.claude/projects:ro" \
  -v "$SCRIPT_DIR/import_memories.py:/app/import_memories.py:ro" \
  mcp-server \
  python /app/import_memories.py --claude-code >> "$LOG" 2>&1

echo "[$(date)] Import complete" >> "$LOG"

# Step 2: Distill new sessions into curated memories (via local Ollama)
$DOCKER compose run --rm -T \
  -e OLLAMA_URL="http://ollama:11434/v1" \
  -v "$SCRIPT_DIR/distill_sessions.py:/app/distill_sessions.py:ro" \
  mcp-server \
  python /app/distill_sessions.py >> "$LOG" 2>&1

echo "[$(date)] Distillation complete" >> "$LOG"

# Step 3: Extract behavioral signals (corrections, tool patterns, file hotspots)
$DOCKER compose run --rm -T \
  -v "$HOME/.claude/projects:/root/.claude/projects:ro" \
  -v "$SCRIPT_DIR/extract_signals.py:/app/extract_signals.py:ro" \
  mcp-server \
  python /app/extract_signals.py >> "$LOG" 2>&1

echo "[$(date)] Signal extraction complete" >> "$LOG"

# Step 4: Behavioral pass — LLM extraction of HOW the user works (type:behavior memories)
$DOCKER compose run --rm -T \
  -e OLLAMA_URL="http://ollama:11434/v1" \
  -v "$HOME/.claude/projects:/root/.claude/projects:ro" \
  -v "$SCRIPT_DIR/behavioral_pass.py:/app/behavioral_pass.py:ro" \
  mcp-server \
  python /app/behavioral_pass.py >> "$LOG" 2>&1

echo "[$(date)] Behavioral pass complete" >> "$LOG"

# Step 5: Generate user profile at ~/.claude/user.md
"$SCRIPT_DIR/venv/bin/python" "$SCRIPT_DIR/generate_user_profile.py" >> "$LOG" 2>&1

echo "[$(date)] User profile generation complete" >> "$LOG"
