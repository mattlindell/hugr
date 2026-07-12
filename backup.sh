#!/bin/zsh
# Backup the claude-memory PostgreSQL database to a compressed dump file.
# Usage: bash backup.sh [output_dir]
# Default output: ./backups/claude-memory-YYYY-MM-DDTHH-MM-SS.pgdump

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OUTPUT_DIR="${1:-$SCRIPT_DIR/backups}"
TIMESTAMP="$(date +%Y-%m-%dT%H-%M-%S)"
DUMP_FILE="$OUTPUT_DIR/claude-memory-${TIMESTAMP}.pgdump"

DOCKER="$(command -v docker || echo /opt/homebrew/bin/docker)"
DB_CONTAINER="claude-memory-db-1"

mkdir -p "$OUTPUT_DIR"

echo "Backing up claude-memory database..."
echo "  Output: $DUMP_FILE"

# Ensure the DB container is running
if ! "$DOCKER" ps --format '{{.Names}}' | grep -q "^${DB_CONTAINER}$"; then
    echo "Starting services..."
    cd "$SCRIPT_DIR"
    "$DOCKER" compose up -d db
    sleep 5
fi

"$DOCKER" exec "$DB_CONTAINER" \
    pg_dump -U claude -d memory --format=custom --compress=9 \
    > "$DUMP_FILE"

SIZE="$(du -sh "$DUMP_FILE" | cut -f1)"
echo "✅ Backup complete: $DUMP_FILE ($SIZE)"
echo ""
echo "To restore: bash restore.sh $DUMP_FILE"
