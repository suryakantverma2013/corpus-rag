#!/bin/bash
# Keycloak's own database and role, inside the same PostgreSQL server (T-605).
#
# Mounted ONLY by docker-compose.prod.yml. The dev stack mounts 01-extension.sql alone and
# is unaffected — the dev deployment runs Keycloak natively with its own storage.
#
# One server rather than two is deliberate: one backup surface, one tuning surface, and the
# two schemas never join. They are separate DATABASES, so Keycloak's tables can never
# collide with Corpus's and a `pg_dump corpus` never sweeps up the realm.
#
# Like every file in /docker-entrypoint-initdb.d, this runs ONLY on the first
# initialisation of an empty data directory. Adding it to a populated volume does nothing;
# create the role and database by hand there (see docs/DEPLOYMENT.md).
#
# It is a .sh and not a .sql precisely so it can read the password from the environment
# instead of having it committed in a file.

set -euo pipefail

if [ -z "${KEYCLOAK_DB_PASSWORD:-}" ]; then
  echo "02-keycloak-db.sh: KEYCLOAK_DB_PASSWORD is unset — refusing to create a passwordless role" >&2
  exit 1
fi

# CREATE DATABASE cannot run inside a transaction block, so these are issued as separate
# autocommit statements (no -1 / --single-transaction).
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
	CREATE ROLE keycloak WITH LOGIN PASSWORD '${KEYCLOAK_DB_PASSWORD}';
	CREATE DATABASE keycloak OWNER keycloak;
EOSQL

echo "02-keycloak-db.sh: role and database 'keycloak' created"
