#!/bin/zsh
# Restore the claude-memory PostgreSQL database from a backup dump file.
# Usage: bash restore.sh <dump_file>
# WARNING: This DROPS and recreates the memory database. All current data will be lost.

set -euo pipefail

DUMP_FILE="${1:-}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DOCKER="$(command -v docker || echo /opt/homebrew/bin/docker)"
DB_CONTAINER="claude-memory-db-1"

if [[ -z "$DUMP_FILE" ]]; then
    echo "Usage: bash restore.sh <dump_file>"
    echo "Example: bash restore.sh backups/claude-memory-2026-03-08T12-00-00.pgdump"
    exit 1
fi

if [[ ! -f "$DUMP_FILE" ]]; then
    echo "❌ File not found: $DUMP_FILE"
    exit 1
fi

echo "⚠️  WARNING: This will ERASE the current claude-memory database and restore from:"
echo "   $DUMP_FILE"
echo ""
read -r "CONFIRM?Type YES to continue: "
if [[ "$CONFIRM" != "YES" ]]; then
    echo "Aborted."
    exit 0
fi

# Ensure the DB container is running
if ! "$DOCKER" ps --format '{{.Names}}' | grep -q "^${DB_CONTAINER}$"; then
    echo "Starting services..."
    cd "$SCRIPT_DIR"
    "$DOCKER" compose up -d db
    sleep 5
fi

echo "Restoring database..."

# Drop and recreate the database
"$DOCKER" exec "$DB_CONTAINER" \
    psql -U claude -d postgres -c "DROP DATABASE IF EXISTS memory;"
"$DOCKER" exec "$DB_CONTAINER" \
    psql -U claude -d postgres -c "CREATE DATABASE memory OWNER claude;"

# Restore from dump
"$DOCKER" exec -i "$DB_CONTAINER" \
    pg_restore -U claude -d memory --no-owner --role=claude \
    < "$DUMP_FILE"

echo "✅ Restore complete from: $DUMP_FILE"
echo ""
echo "Restart the MCP server to reconnect:"
echo "  docker compose restart mcp-server"
