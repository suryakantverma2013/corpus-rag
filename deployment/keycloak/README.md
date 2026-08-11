# Keycloak realm bootstrap (Corpus auth, R-28)

Auth is **Keycloak (OIDC), backend-mediated ROPC** (ruling R-28). The backend
exchanges credentials with the realm token endpoint and validates realm-signed
**RS256** JWTs against the realm JWKS. This directory seeds the realm.

`corpus-realm.json` provisions everything T-103 needs:

- realm **`corpus`**, `loginWithEmailAllowed = true` (identity is keyed on email);
- realm roles **`admin`** / **`user`** — the two-role model (spec §4.9); new users
  default to `user`;
- a **declarative user profile** in which `firstName`/`lastName` are **not required**
  (`email` still is) — see "The ROPC constraint" below, and T-110;
- confidential client **`corpus-backend`** with **Direct Access Grants** (ROPC)
  and **Service Accounts** enabled, plus an **audience mapper** so issued access
  tokens carry `aud: corpus-backend` (the backend verifies `aud` **and** `azp`);
- the client's **service account** granted `realm-management` →
  **`manage-users`** (self-service change-password's admin `reset-password` call)
  plus **`view-realm`**, **`view-users`** and **`query-users`** — the last three are
  what `GET /api/v1/users` needs to read realm roles. `manage-users` alone answers
  **403** there, which the client maps to a misleading `503` (T-110);
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

Either import path works; there is also a REST route (create the realm by POSTing
this file to `/admin/realms` with a master-realm admin token), which is how the
realm on the current dev box was created.

> **Port note:** the examples above use Keycloak's default `8080`. The current dev
> box runs Keycloak on **`8081`** — keep `KEYCLOAK_SERVER_URL` in `backend/.env` in
> step with whatever it actually listens on, and read `8080` below as "your port".

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

## Google Drive linking (FR-AUT-11 / FR-KBM-10, ruling R-63) — T-214

Corpus imports files from Google Drive, and **Keycloak owns that OAuth entirely**. The
backend has no Google client and stores no Google credential: Keycloak brokers the
exchange, keeps the provider tokens (`storeToken`), refreshes them, and the backend reads
the current one from `/realms/corpus/broker/google/token`. **The Google client secret lives
only in Keycloak** — never in `backend/.env`, never in this repo.

### 1. Register an OAuth client with Google (one time)

Google requires every OAuth client to be registered; no Keycloak setting removes this.

1. Google Cloud Console → a project → **enable the Google Drive API**.
2. **OAuth consent screen.** Choose the user type deliberately, because it cannot be
   switched freely once the app has users:
   - **Internal** (Google Workspace domain only) — no app verification and no security
     assessment for the restricted `drive.readonly` scope, and refresh tokens do not
     expire. This is what R-63(7) assumes.
   - **External** (personal `@gmail.com`) — stays in *Testing*: up to 100 test users, no
     verification, but **refresh tokens expire after 7 days**, so you re-link about
     weekly. Fine for development; note it invalidates R-63(7)'s assumption for anything
     beyond it.
3. **Credentials → Create OAuth client ID → Web application.** Authorized redirect URI:
   ```
   http://localhost:8081/realms/corpus/broker/google/endpoint
   ```
   (Keycloak's broker endpoint — match your actual port.) Keep the client ID and secret.

### 2. Fill in the placeholders in Keycloak

The committed realm carries `CHANGE_ME_google-oauth-client-id` / `-client-secret`, exactly
as `corpus-backend`'s secret is a placeholder. In the admin console → Identity providers →
`google`, set the real **Client ID** and **Client Secret**, then verify:

- **Store tokens** is ON — without it the broker endpoint returns 200 with no token, and
  the backend raises a configuration error rather than pretending it is an outage.
- **Stored tokens readable** / `read-token` is granted on link (`addReadTokenRoleOnCreate`).
- **Scopes** include `https://www.googleapis.com/auth/drive.readonly`.

### 3. What the user sees, and the one wart

Linking is **opt-in and separate from login**: ROPC login is untouched, and only a user who
clicks "Add from cloud drive" ever sees this. The journey is: Corpus → Keycloak
(`corpus-linking` client) → Google consent → back.

**Expect one Keycloak-rendered password prompt during linking, and do not treat it as a
bug.** ROPC issues tokens but creates **no browser session**, so when the linking redirect
arrives Keycloak sees an unauthenticated browser. After Google returns, first-broker-login
must prove the user owns the existing Corpus account — and because brokering is
**link-only** (auto-creation would be a self-service signup route FR-USR-02/03 forbids),
that means re-authenticating. It happens once per user. It is unavoidable while login is
ROPC: client-initiated linking skips re-auth only when a browser SSO session already
exists, and token exchange can *read* a brokered token but cannot *create* the link.

### 4. Do not let brokering become a login path

`google` must stay **link-only**: Keycloak must never create a Corpus account from a Google
identity. FR-USR-02/03 make account creation administrator-only, so an auto-creating first
broker login flow would silently add self-service signup to a product that does not have it.

## The ROPC constraint — read before changing realm config (T-110)

Auth is **backend-mediated ROPC**: there is no browser in the login flow, so
**Keycloak cannot ask the user for anything**. Any *required action* is therefore
unsatisfiable, and a user carrying one is locked out permanently — the token
endpoint answers `400 invalid_grant` / *"Account is not fully set up"* and Corpus
has no recovery surface (FR-AUT-* offers change-password, which needs a login
first).

That is not hypothetical: it is the defect T-110 found. Keycloak's default user
profile marks `firstName` and `lastName` **required**, `VERIFY_PROFILE` is enabled,
and `POST /api/v1/users` treats `display_name` as optional — so a user created with
just an email could authenticate never. Measured: no name → locked out; a
single-word display name (`lastName` empty) → locked out; a two-word one → fine.
The realm now makes both attributes optional.

Consequences to respect:

- **Do not enable a required action as a default** (`UPDATE_PASSWORD`,
  `VERIFY_EMAIL`, `CONFIGURE_TOTP`, `TERMS_AND_CONDITIONS`) unless a browser flow
  ships first — it will brick every affected account.
- **Do not set a required action on an individual user** from the console for the
  same reason. An admin password reset via `PATCH /api/v1/users/{id}` is the
  supported path, because it sets a non-temporary credential.
- Re-requiring `firstName`/`lastName` means `POST /api/v1/users` must start sending
  both, or it goes back to minting dead accounts.

`backend/tests/test_auth_live.py::test_realm_artifact_imports_and_works` imports
this file into a throwaway realm and asserts both fixes, so a regression here fails
a test rather than surfacing as "the new user can't log in".

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
