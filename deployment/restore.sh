#!/usr/bin/env bash
# Restore a Corpus production stack from a backup.sh directory (T-702).
#
# WHY THIS EXISTS. Rolling back across a migration is a RESTORE, not `alembic downgrade`:
# several downgrades in this project destroy data (`turn_telemetry`, `model_overrides`) and
# one is only valid on a corpus where nothing has been deleted and re-uploaded. See
# docs/DEPLOYMENT.md §9.3. This is the other half of that sentence.
#
# THIS OVERWRITES BOTH DATABASES AND THE BUCKET. It requires --yes for that reason.
#
#   ./deployment/restore.sh backups/backup-20260821-1430 --yes
#   ./deployment/restore.sh <dir> --yes --force     # accept an Alembic head mismatch
#
# THE HEAD CHECK IS THE POINT. Restoring a dump taken at schema X into a stack running
# code that expects schema Y fails silently here — the restore succeeds, and the mismatch
# surfaces later as a runtime error far from its cause. So a mismatch is refused by
# default. --force is for the deliberate case: you are rolling the CODE back too.
#
# The stack must already be running (the containers are how this reaches Postgres and
# MinIO). To restore onto a clean host, bring the stack up first so the initdb scripts
# create the `vector` extension and the `keycloak` role, then run this. See §9.2.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-$HERE/docker-compose.prod.yml}"
ENV_FILE="${ENV_FILE:-$HERE/.env.prod}"
SRC=""
ASSUME_YES=0
FORCE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --yes)     ASSUME_YES=1; shift ;;
    --force)   FORCE=1; shift ;;
    -h|--help) sed -n '2,22p' "${BASH_SOURCE[0]}"; exit 0 ;;
    -*)        echo "restore: unknown argument '$1'" >&2; exit 2 ;;
    *)         SRC="$1"; shift ;;
  esac
done

[ -n "$SRC" ]              || { echo "restore: give me a backup directory" >&2; exit 2; }
[ -f "$SRC/MANIFEST" ]     || { echo "restore: no MANIFEST in $SRC — is that a backup.sh directory?" >&2; exit 1; }
[ -s "$SRC/corpus.dump" ]  || { echo "restore: $SRC/corpus.dump is missing or empty" >&2; exit 1; }
[ -s "$SRC/keycloak.dump" ]|| { echo "restore: $SRC/keycloak.dump is missing or empty" >&2; exit 1; }
[ -f "$ENV_FILE" ]         || { echo "restore: no env file at $ENV_FILE" >&2; exit 1; }

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

manifest_value() { awk -F= -v k="$1" '$1==k {print $2; exit}' "$SRC/MANIFEST"; }

BACKUP_HEAD="$(manifest_value alembic_head)"
BACKUP_TAKEN="$(manifest_value taken_at)"
BACKUP_QUIESCED="$(manifest_value quiesced)"

echo "restore: source      $SRC"
echo "restore: taken at    $BACKUP_TAKEN (quiesced=$BACKUP_QUIESCED)"
echo "restore: schema head $BACKUP_HEAD"

# ---- the head check -------------------------------------------------------------------
CURRENT_HEAD="$(dc exec -T api sh -c 'cd /app && alembic current 2>/dev/null' \
                | awk '/^[0-9a-f]{6,}/ {print $1}' | tail -1)"
echo "restore: stack head  ${CURRENT_HEAD:-<unreadable>}"

if [ -n "$CURRENT_HEAD" ] && [ "$CURRENT_HEAD" != "$BACKUP_HEAD" ]; then
  if [ "$FORCE" != "1" ]; then
    echo >&2
    echo "restore: REFUSING — the backup was taken at schema '$BACKUP_HEAD' but this stack is" >&2
    echo "         at '$CURRENT_HEAD'. Restoring across that gap leaves the database and the" >&2
    echo "         running code disagreeing, and nothing will report it at restore time." >&2
    echo "         Roll the code back to the matching release first, or pass --force if that" >&2
    echo "         is exactly what you are doing. See docs/DEPLOYMENT.md §9.3." >&2
    exit 1
  fi
  echo "restore: head mismatch accepted via --force"
fi

if [ "$BACKUP_QUIESCED" = "0" ]; then
  echo "restore: NOTE — this was a hot backup; a document ingested or deleted while it ran may"
  echo "         be inconsistent between the database and the bucket (see §9.2)."
fi

if [ "$ASSUME_YES" != "1" ]; then
  echo >&2
  echo "restore: REFUSING — this overwrites both databases and the '$MINIO_BUCKET' bucket." >&2
  echo "         Re-run with --yes once you mean it." >&2
  exit 1
fi

# ---- quiesce --------------------------------------------------------------------------
# api and worker must be down so nothing writes during the restore; keycloak must be down
# because it pools connections to the database being replaced underneath it and caches the
# realm in memory, so it would keep serving the pre-restore state.
echo "restore: stopping api, worker and keycloak"
dc stop api worker keycloak >/dev/null

resume() {
  echo "restore: restarting keycloak, api and worker"
  dc start keycloak api worker >/dev/null || true
}
trap resume EXIT

# ---- 1. the two databases -------------------------------------------------------------
# --clean --if-exists so a restore over a populated database replaces it rather than
# colliding; --no-password because credentials come from the container's own environment.
echo "restore: restoring database '$POSTGRES_DB'"
dc exec -T postgres pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  --clean --if-exists --no-password < "$SRC/corpus.dump"

echo "restore: restoring database 'keycloak'"
dc exec -T postgres pg_restore -U "$POSTGRES_USER" -d keycloak \
  --clean --if-exists --no-password < "$SRC/keycloak.dump"

# ---- 2. the object store --------------------------------------------------------------
# --overwrite replaces changed objects; --remove deletes objects the backup does not have,
# which is what makes the bucket MATCH the dump rather than merely contain it. Without
# --remove a restore leaves behind every object uploaded after the backup, and those are
# exactly the ones whose database rows no longer exist.
if [ -d "$SRC/objects" ]; then
  echo "restore: restoring bucket '$MINIO_BUCKET'"
  CID="$(dc ps -q minio)"
  dc exec -T minio rm -rf /tmp/rs-objects >/dev/null
  docker cp "$SRC/objects" "$CID:/tmp/rs-objects" >/dev/null
  dc exec -T minio sh -c "
    set -e
    mc alias set rs http://localhost:9000 '$MINIO_ROOT_USER' '$MINIO_ROOT_PASSWORD' >/dev/null
    mc mb --ignore-existing rs/'$MINIO_BUCKET' >/dev/null
    mc mirror --overwrite --remove /tmp/rs-objects rs/'$MINIO_BUCKET'
    rm -rf /tmp/rs-objects
  " >/dev/null
else
  echo "restore: no objects/ directory in the backup — bucket left untouched"
fi

resume
trap - EXIT

echo
echo "restore: complete. Verify with:"
echo "  curl -s localhost:\${PUBLIC_PORT:-8088}/health/ready"
echo "  curl -s localhost:\${PUBLIC_PORT:-8088}/health/ready/worker"
