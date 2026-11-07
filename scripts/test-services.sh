#!/usr/bin/env bash
# Start (or stop) throwaway Postgres + MinIO for the integration tests. Used by CI and locally.
#
#   scripts/test-services.sh up     # then: eval "$(scripts/test-services.sh env)"; cd services/api; pytest
#   scripts/test-services.sh down
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PG_IMAGE="pgvector/pgvector:pg16"
# Same digest as docker-compose.yml; bump both together.
MINIO_IMAGE="cgr.dev/chainguard/minio@sha256:aea6c163749e1caca37f907976dcf337177ddf5f558808f2aa591618fd3a6eef"
PG_PORT=55432
S3_PORT=59000

case "${1:-}" in
  up)
    docker run -d --rm --name callsight-testdb -p "$PG_PORT:5432" \
      -e POSTGRES_PASSWORD=test -e POSTGRES_DB=callsight \
      -v "$ROOT/db/schema.sql:/docker-entrypoint-initdb.d/001-schema.sql:ro" "$PG_IMAGE" >/dev/null
    docker run -d --rm --name callsight-testminio -p "$S3_PORT:9000" \
      -e MINIO_ROOT_USER=test -e MINIO_ROOT_PASSWORD=testtesttest "$MINIO_IMAGE" server /data >/dev/null
    for _ in $(seq 60); do
      # The schema is applied after Postgres first reports ready, so wait for a table, not pg_isready.
      if docker exec callsight-testdb psql -U postgres -d callsight -tAc "SELECT 1 FROM calls LIMIT 0" >/dev/null 2>&1 \
        && docker exec callsight-testminio mc ready local >/dev/null 2>&1; then
        echo "test services ready"
        exit 0
      fi
      sleep 1
    done
    echo "test services did not become ready" >&2
    docker logs callsight-testdb >&2 || true
    exit 1
    ;;
  env)
    echo "export CALLSIGHT_TEST_DATABASE_URL=postgresql://postgres:test@localhost:$PG_PORT/callsight"
    echo "export CALLSIGHT_TEST_S3_ENDPOINT=localhost:$S3_PORT"
    echo "export CALLSIGHT_TEST_S3_ACCESS_KEY=test"
    echo "export CALLSIGHT_TEST_S3_SECRET_KEY=testtesttest"
    ;;
  down)
    docker rm -f callsight-testdb callsight-testminio >/dev/null 2>&1 || true
    ;;
  *)
    echo "usage: $0 up|env|down" >&2
    exit 2
    ;;
esac
