# Keycloak realm bootstrap (Corpus auth, R-28)

Auth is **Keycloak (OIDC), backend-mediated ROPC** (ruling R-28). The backend
exchanges credentials with the realm token endpoint and validates realm-signed
**RS256** JWTs against the realm JWKS. This directory seeds the realm.

`corpus-realm.json` provisions everything T-103 needs:

- realm **`corpus`**, `loginWithEmailAllowed = true` (identity is keyed on email);
- realm roles **`admin`** / **`user`** — the two-role model (spec §4.9); new users
  default to `user`;
- confidential client **`corpus-backend`** with **Direct Access Grants** (ROPC)
  and **Service Accounts** enabled, plus an **audience mapper** so issued access
  tokens carry `aud: corpus-backend` (the backend verifies `aud` **and** `azp`);
- the client's **service account** granted `realm-management` → **`manage-users`**
  (needed for self-service change-password's admin `reset-password` call);
- a default **Administrator** user `admin@corpus.local` with the `admin` role
  (FR-USR-02 — replaces the old DB seed).

## Import

Fresh container (import on start):

```bash
docker run --rm -p 8080:8080 \
  -e KC_BOOTSTRAP_ADMIN_USERNAME=admin -e KC_BOOTSTRAP_ADMIN_PASSWORD=admin \
  -v "$PWD/deployment/keycloak:/opt/keycloak/data/import:ro" \
  quay.io/keycloak/keycloak:latest start-dev --import-realm
```

Into a running instance:

```bash
/opt/keycloak/bin/kc.sh import --file /opt/keycloak/data/import/corpus-realm.json
```

## After import — required manual steps

The committed JSON carries **placeholder secrets**. In the admin console
(`http://localhost:8080`, realm `corpus`):

1. **Client secret** — Clients → `corpus-backend` → Credentials → *Regenerate*,
   then set `KEYCLOAK_CLIENT_SECRET` in `backend/.env` to the new value.
2. **Admin password** — Users → `admin@corpus.local` → Credentials → set a real
   password (replace `CHANGE_ME_admin-password`).
3. **Verify** Clients → `corpus-backend` → Service account roles includes
   `manage-users`, and Client scopes carries the `corpus-backend-audience` mapper.

`backend/.env` must match the realm:

```
KEYCLOAK_SERVER_URL=http://localhost:8080
KEYCLOAK_REALM=corpus
KEYCLOAK_CLIENT_ID=corpus-backend
KEYCLOAK_CLIENT_SECRET=<regenerated secret>
```

## Notes

- **Brute-force protection is on** (`bruteForceProtected: true`); a locked account
  surfaces from the token endpoint as `400 invalid_grant` with a "temporarily
  disabled" description, which the backend best-effort-maps to **429** (FR-AUT-04).
  The authoritative app-level login throttle is **T-105 (slowapi)**.
- Password policy, lockout thresholds, and session/inactivity timeouts are
  **realm configuration** here (spec §8.4 TBDs delegated to Keycloak by R-28) —
  tune `failureFactor` / `maxFailureWaitSeconds` and add a Password Policy as
  those values are decided.
- User management (create/list/patch/delete) is **T-104** via the Keycloak Admin
  API (`python-keycloak`).
