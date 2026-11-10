#!/usr/bin/env bash
# Restore a backup made by scripts/backup.sh. REPLACES the current database contents.
#   scripts/restore.sh backups/20261110T120000Z
set -euo pipefail
COMPOSE=${COMPOSE:-docker compose}
IN=${1:?usage: scripts/restore.sh <backup directory>}
[ -f "$IN/database.dump" ] || { echo "no database.dump in $IN" >&2; exit 1; }

$COMPOSE exec -T db sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner' < "$IN/database.dump"

$COMPOSE cp "$IN/recordings" minio:/tmp/callsight-restore >/dev/null
$COMPOSE exec -T minio sh -c '
  mc alias set backup http://localhost:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null
  mc mb --ignore-existing backup/recordings >/dev/null
  mc mirror --quiet --overwrite /tmp/callsight-restore backup/recordings >/dev/null'
# `compose cp` copies files in as root; MinIO runs as a non-root user, so clean up as root.
$COMPOSE exec -T -u 0 minio rm -rf /tmp/callsight-restore
echo "restored from $IN"
