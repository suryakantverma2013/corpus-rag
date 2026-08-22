#!/usr/bin/env bash
# Back up a running Corpus production stack: both databases and the object store (T-702).
#
# WHY THIS EXISTS. The stack keeps its state in two places that must be restored as a
# matched pair — PostgreSQL (`pgdata`, holding the `corpus` AND `keycloak` databases) and
# the object store (`minio-data`, holding every uploaded original). Restore one without the
# other and you get documents whose bytes are gone, or users who no longer exist.
#
# The other three volumes are deliberately NOT backed up, and that is not an oversight:
#
#   redis-data     the arq job queue and rate-limit counters — reconstructible; a lost
#                  queue costs in-flight ingestions, which are re-drivable from the GUI.
#   clamav-data    virus signatures — re-downloaded by freshclam on next start.
#   ocr-tessdata   language packs — re-seeded from the image while the volume is empty.
#
# CONSISTENCY. Upload writes the object BEFORE committing the row; delete purges the object
# BEFORE committing the row (R-39(9)). Those are opposite orders, so no ordering of a *hot*
# backup is safe against both: dump-then-mirror survives concurrent uploads but can capture
# a live row whose bytes a concurrent delete already purged, and mirror-then-dump does the
# reverse. So this script QUIESCES by default — it stops `api` and `worker`, takes the
# backup, and starts them again. Pass --hot to skip that at the cost above.
#
#   ./deployment/backup.sh                     # quiesced (default), to ./backups/
#   ./deployment/backup.sh --hot               # no downtime, may skew — see above
#   ./deployment/backup.sh --output /srv/bk    # somewhere else
#
# Restore with restore.sh. See docs/DEPLOYMENT.md §9.2.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-$HERE/docker-compose.prod.yml}"
ENV_FILE="${ENV_FILE:-$HERE/.env.prod}"
OUTPUT_ROOT="${OUTPUT_ROOT:-$HERE/../backups}"
QUIESCE=1

while [ $# -gt 0 ]; do
  case "$1" in
    --hot)      QUIESCE=0; shift ;;
    --output)   OUTPUT_ROOT="${2:?--output needs a directory}"; shift 2 ;;
    -h|--help)  sed -n '2,25p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *)          echo "backup: unknown argument '$1'" >&2; exit 2 ;;
  esac
done

[ -f "$ENV_FILE" ] || { echo "backup: no env file at $ENV_FILE" >&2; exit 1; }

# The env file is the same one compose reads. Sourcing it rather than re-parsing keeps one
# definition of the credentials; `set -a` exports without listing each name.
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

: "${POSTGRES_USER:?POSTGRES_USER missing from $ENV_FILE}"
: "${POSTGRES_DB:?POSTGRES_DB missing from $ENV_FILE}"
: "${MINIO_ROOT_USER:?MINIO_ROOT_USER missing from $ENV_FILE}"
: "${MINIO_ROOT_PASSWORD:?MINIO_ROOT_PASSWORD missing from $ENV_FILE}"
MINIO_BUCKET="${MINIO_BUCKET:-corpus}"

dc() { docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" "$@"; }

STAMP="$(date +%Y%m%d-%H%M%S)"
DEST="$OUTPUT_ROOT/backup-$STAMP"
mkdir -p "$DEST"

started_paused=0
resume() {
  if [ "$started_paused" = "1" ]; then
    echo "backup: restarting api and worker"
    dc start api worker >/dev/null
    started_paused=0
  fi
}
# Any exit path — success, failure or Ctrl-C — must bring the stack back up. A backup
# script that leaves the product down on error is worse than no backup script.
trap resume EXIT

# Read the api container's view of the world BEFORE quiescing — `docker compose exec` needs a
# RUNNING container, so gathering these after `stop api` fails the whole script. Reading them
# first is also the more correct answer: they describe the stack the dump came from.
echo "backup: reading schema and pipeline identity"
ALEMBIC_HEAD="$(dc exec -T api sh -c 'cd /app && alembic current 2>/dev/null' | awk '/^[0-9a-f]{6,}/ {print $1}' | tail -1)"
PIPELINE="$(dc exec -T api sh -c 'cd /app && python -c "
from app.ingestion.chunker import effective_chunking_version
from app.ingestion.parsers.base import PREPROCESSING_VERSION
from app.config import get_settings
s = get_settings()
print(\"chunking_version=\" + effective_chunking_version(s.chunker))
print(\"preprocessing_version=\" + PREPROCESSING_VERSION)
print(\"embedding_model=\" + s.openai.embedding_model)
" 2>/dev/null' | tr -d '\r')"
[ -n "$ALEMBIC_HEAD" ] || { echo "backup: could not read the Alembic head from the api container" >&2; exit 1; }

if [ "$QUIESCE" = "1" ]; then
  echo "backup: quiescing (stopping api and worker)"
  dc stop api worker >/dev/null
  started_paused=1
else
  echo "backup: HOT backup — api and worker keep running; see the header for the skew this admits"
fi

# ---- 1. the two databases ------------------------------------------------------------
# -Fc (custom format) so restore.sh can use pg_restore --clean and pick objects; -T on exec
# or docker allocates a TTY and corrupts the binary stream with CRLF translation.
echo "backup: dumping database '$POSTGRES_DB'"
dc exec -T postgres pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc > "$DEST/corpus.dump"

echo "backup: dumping database 'keycloak' (users, realm, credentials)"
dc exec -T postgres pg_dump -U "$POSTGRES_USER" -d keycloak -Fc > "$DEST/keycloak.dump"

for f in corpus keycloak; do
  [ -s "$DEST/$f.dump" ] || { echo "backup: $f.dump is empty — aborting" >&2; exit 1; }
done

# ---- 2. the object store --------------------------------------------------------------
# The image ships an alias named `local` with EMPTY credentials: it satisfies the
# `mc ready local` healthcheck but cannot list a bucket, so a mirror through it would
# succeed and copy nothing. Set our own alias from the root credentials instead.
echo "backup: mirroring bucket '$MINIO_BUCKET'"
dc exec -T minio sh -c "
  set -e
  mc alias set bk http://localhost:9000 '$MINIO_ROOT_USER' '$MINIO_ROOT_PASSWORD' >/dev/null
  rm -rf /tmp/bk-objects
  mc mirror --quiet bk/'$MINIO_BUCKET' /tmp/bk-objects >/dev/null
" >/dev/null

CID="$(dc ps -q minio)"
docker cp "$CID:/tmp/bk-objects" "$DEST/objects" >/dev/null
dc exec -T minio rm -rf /tmp/bk-objects >/dev/null

# ---- 3. the manifest ------------------------------------------------------------------
# This is the load-bearing artefact. A dump restored into a stack at a different Alembic
# head, or built by a different ingestion pipeline, fails silently — the schema mismatch
# surfaces as a runtime error much later, and a pipeline mismatch surfaces as nothing at
# all (every document simply keeps serving vectors nothing will re-derive). restore.sh
# refuses on a head mismatch, and §9.3 tells the operator what a pipeline change costs.
echo "backup: writing MANIFEST"

# postgres stays up through a quiesce, so these can be read at any point.
COUNTS="$(dc exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -F= -c "
  select 'documents', count(*) from documents
  union all select 'document_chunks', count(*) from document_chunks
  union all select 'conversations', count(*) from conversations
  union all select 'messages', count(*) from messages
  union all select 'users', count(*) from users
" | tr -d '\r')"

OBJECTS="$(find "$DEST/objects" -type f 2>/dev/null | wc -l | tr -d ' ')"

{
  echo "# Corpus backup manifest"
  echo "taken_at=$STAMP"
  echo "quiesced=$QUIESCE"
  echo "image_tag=${IMAGE_TAG:-local}"
  echo "alembic_head=$ALEMBIC_HEAD"
  echo "$PIPELINE"
  echo "$COUNTS"
  echo "objects=$OBJECTS"
} > "$DEST/MANIFEST"

resume
trap - EXIT

echo
echo "backup: complete -> $DEST"
sed -n '2,$p' "$DEST/MANIFEST" | sed 's/^/  /'
