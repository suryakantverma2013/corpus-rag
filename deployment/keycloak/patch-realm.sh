#!/usr/bin/env bash
# Replace the corpus-realm.json placeholders after import (T-605). Idempotent.
#
# WHY THIS EXISTS. `corpus-realm.json` is a committed artifact in a public repository, so
# it ships placeholders rather than credentials. Two of them are LIVE once imported:
#
#   corpus-backend .secret        = CHANGE_ME_dev-client-secret
#   admin@corpus.local password   = CHANGE_ME_admin-password
#
# A stack that boots without this step is a stack whose administrator password is published
# on GitHub. The remaining two (the Google OAuth pair) are optional — FR-KBM-10 cloud
# import is the only thing that needs them.
#
# Run automatically by the `keycloak-init` service in docker-compose.prod.yml, and safe to
# re-run by hand:
#   docker compose -f deployment/docker-compose.prod.yml run --rm keycloak-init

set -euo pipefail

KCADM=/opt/keycloak/bin/kcadm.sh

: "${KC_URL:?KC_URL is required (e.g. http://keycloak:8080/auth)}"
: "${CORPUS_REALM:?CORPUS_REALM is required}"
: "${CORPUS_CLIENT_ID:?CORPUS_CLIENT_ID is required}"
: "${CORPUS_CLIENT_SECRET:?CORPUS_CLIENT_SECRET is required}"
: "${CORPUS_ADMIN_EMAIL:?CORPUS_ADMIN_EMAIL is required}"
: "${CORPUS_ADMIN_PASSWORD:?CORPUS_ADMIN_PASSWORD is required}"

# --noquotes still emits a trailing newline, and on a Windows-checked-out volume possibly a
# CR too; strip both or the id lands in a URL path as `abc%0D`.
clean() { tr -d '\r\n'; }

echo "patch-realm: authenticating against ${KC_URL} (master realm)"
"$KCADM" config credentials \
  --server "$KC_URL" \
  --realm master \
  --user "${KC_BOOTSTRAP_ADMIN_USERNAME:?}" \
  --password "${KC_BOOTSTRAP_ADMIN_PASSWORD:?}"

# ---- 1. the confidential client's secret ------------------------------------------
backend_uuid=$("$KCADM" get clients -r "$CORPUS_REALM" \
  -q "clientId=${CORPUS_CLIENT_ID}" --fields id --format csv --noquotes | clean)
if [ -z "$backend_uuid" ]; then
  echo "patch-realm: client ${CORPUS_CLIENT_ID} not found in realm ${CORPUS_REALM}" >&2
  exit 1
fi
"$KCADM" update "clients/${backend_uuid}" -r "$CORPUS_REALM" -s "secret=${CORPUS_CLIENT_SECRET}"
echo "patch-realm: ${CORPUS_CLIENT_ID} secret set"

# ---- 2. the seeded administrator's password ---------------------------------------
# NOT temporary, and this is the single most important line in the file.
#
# A temporary credential attaches the UPDATE_PASSWORD required action. Authentication here
# is backend-mediated ROPC (grant_type=password) — there is no browser anywhere in the flow
# — so nothing can ever satisfy that action and the account is bricked PERMANENTLY, with
# Keycloak reporting only "Account is not fully set up". The same reasoning forbids
# enabling any required action as a realm default. See deployment/keycloak/README.md.
"$KCADM" set-password -r "$CORPUS_REALM" \
  --username "$CORPUS_ADMIN_EMAIL" \
  --new-password "$CORPUS_ADMIN_PASSWORD"
echo "patch-realm: password set for ${CORPUS_ADMIN_EMAIL} (non-temporary)"

# ---- 3. corpus-linking redirect URIs ----------------------------------------------
# The artifact lists only the dev origins (localhost:5173, localhost:8000). The FR-AUT-11
# linking flow returns the browser by full page load, so the deployment's own origin has to
# be registered or Keycloak refuses the redirect. Dev origins are kept so one realm export
# still serves a developer.
if [ -n "${PUBLIC_ORIGIN:-}" ]; then
  link_uuid=$("$KCADM" get clients -r "$CORPUS_REALM" \
    -q "clientId=corpus-linking" --fields id --format csv --noquotes | clean)
  if [ -n "$link_uuid" ]; then
    "$KCADM" update "clients/${link_uuid}" -r "$CORPUS_REALM" \
      -s "redirectUris=[\"${PUBLIC_ORIGIN}/*\",\"http://localhost:5173/*\",\"http://localhost:8000/*\"]"
    echo "patch-realm: corpus-linking redirect URIs include ${PUBLIC_ORIGIN}"
  fi
fi

# ---- 4. Google identity provider, only if supplied --------------------------------
# Optional: cloud import (FR-KBM-10 / FR-AUT-11) is the only feature that needs it, and the
# credentials belong to Keycloak alone — the backend stores no third-party credential.
if [ -n "${GOOGLE_CLIENT_ID:-}" ] && [ -n "${GOOGLE_CLIENT_SECRET:-}" ]; then
  "$KCADM" update "identity-provider/instances/google" -r "$CORPUS_REALM" \
    -s "config.clientId=${GOOGLE_CLIENT_ID}" \
    -s "config.clientSecret=${GOOGLE_CLIENT_SECRET}"
  echo "patch-realm: google identity provider configured"
else
  echo "patch-realm: google identity provider left unconfigured (cloud import disabled)"
fi

echo "realm patched"
