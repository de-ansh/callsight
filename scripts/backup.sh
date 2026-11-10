#!/usr/bin/env bash
# Back up Callsight: a Postgres dump plus every recording, into backups/<timestamp>/.
#   scripts/backup.sh                      # with the default compose files
#   COMPOSE="docker compose -f docker-compose.yml -f docker-compose.prod.yml" scripts/backup.sh
set -euo pipefail
COMPOSE=${COMPOSE:-docker compose}
OUT=${1:-backups/$(date -u +%Y%m%dT%H%M%SZ)}
mkdir -p "$OUT"

$COMPOSE exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -Fc "$POSTGRES_DB"' > "$OUT/database.dump"

# Mirror recordings at the object level (not MinIO's on-disk format), then copy them out.
$COMPOSE exec -T minio sh -c '
  mc alias set backup http://localhost:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null
  rm -rf /tmp/callsight-backup && mkdir -p /tmp/callsight-backup
  if mc ls backup/recordings >/dev/null 2>&1; then mc mirror --quiet backup/recordings /tmp/callsight-backup >/dev/null; fi'
$COMPOSE cp minio:/tmp/callsight-backup "$OUT/recordings" >/dev/null
$COMPOSE exec -T minio rm -rf /tmp/callsight-backup

echo "backup written to $OUT ($(du -sh "$OUT" | cut -f1)); $(find "$OUT/recordings" -type f | wc -l | tr -d ' ') recording file(s)"
