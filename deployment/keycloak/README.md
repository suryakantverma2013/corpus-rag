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
  quay.io/keycloak/keycloak:26.7.1 start-dev --import-realm
```

**Pin the tag; do not use `:latest` (T-724/T-725).** This recipe said `:latest` until 2026-08-27,
and that is how FR-AUT-11 linking broke with no code change: the dev container moved to **26.7.1**,
which refuses a `redirect_uri` carrying a reserved OIDC parameter, while `docker-compose.prod.yml`
pins **26.4**, which does not. The feature rotted in place and the two environments disagreed for
days — see T-725 for the fix and `app/services/cloud_links.py` for the constraint.

**Dev is deliberately AHEAD of production** (26.7.1 here, 26.4 there), because that gap is what
surfaced the break in dev first rather than in production. Keep it — but move it *deliberately*:
a version change is the trigger on **T-726**, since the client-initiated account-linking endpoint
this realm's `corpus-linking` client depends on is deprecated and slated for removal, and its
removal will break linking exactly the way 26.7.1's tightening did. Before bumping either tag,
re-run `backend/tests/test_cloud_import.py` and relink an account end to end.

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
- **Stored tokens readable.** ⚠ **`addReadTokenRoleOnCreate` does NOT grant the role here —
  proved live 2026-08-11.** It fires only when brokering *creates* an account, and
  `linkOnly: true` exists to forbid exactly that, so **the two settings are mutually
  exclusive in effect**. After a fully successful link the user held no `broker` roles and
  `broker_token()` raised `AccountNotLinkedError`, misreporting the cause — Keycloak answers
  403 both for "not linked" and for "missing read-token". **Every user would have hit this
  permanently.** The seeded admin now carries `clientRoles: {broker: [read-token]}` in the
  artifact. **Granting it to every user is settled: the backend does it after a successful
  link, in T-214's linking endpoint.** The realm-default-roles route was *tried and rejected
  on evidence* — making the `user` role a composite carrying `client: {broker: [read-token]}`
  fails the whole realm import with a bare `500`, because realm roles are created **before**
  Keycloak's built-in clients (`broker`, `account`) exist, so the reference cannot resolve.
  Per-user `clientRoles` works only because users are imported *after* clients. `read-token`
  is safe to hold: it reads *your own* brokered token and yields nothing without a link.
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

**The enforced mechanism is `"linkOnly": true` on the identity provider.** With it, Keycloak
hides the provider from the login page and it can be reached only from an
already-authenticated session — so the Google identity is linked to *that* user and the
unique-user branch is never taken.

**This was wrong in the committed realm until 2026-08-11.** The file shipped
`"linkOnly": false` while this section, FR-AUT-11 and R-63(3) all said link-only; the flow
alias still names Keycloak's built-in `first broker login`, whose first alternative is
`idp-create-user-if-unique`. So a Google identity whose email matched **no** Corpus user
would have had an account created for it. Existing users were unaffected, which is precisely
why it read as correct. It is now pinned by `backend/tests/test_account_linking.py`.

**Still owed:** a *custom* link-only first-broker-login flow (the built-in one with
`idp-create-user-if-unique` removed) as defence in depth, so the guard does not rest on one
boolean. Deliberately not committed unverified — a malformed `authenticationFlows` entry
breaks realm import for everyone, and there is no in-repo precedent for its exact JSON shape.
Build it against a running Keycloak, export, and let `test_auth_live.py` prove the import.

### 5. The seeded admin needs its roles granted explicitly

A user's `realmRoles` in a realm import **replaces** the defaults rather than adding to them.
Declaring `['admin', 'user']` therefore cost `admin@corpus.local` the `default-roles-<realm>`
composite — and with it `view-profile` and `manage-account`, so the Keycloak **account console
failed with a bare "Something went wrong"**. Every user created through Corpus's own
`POST /api/v1/users` gets that composite automatically, so the seeded admin was the only user
in the realm without it.

The artifact now grants them through the **`account` client** (`view-profile`,
`manage-account`) rather than by naming the composite: `default-roles-<realm>` embeds the realm
name, and `test_realm_artifact_imports_and_works` imports this file into a *renamed* throwaway
realm, where a hardcoded name resolves to nothing. The `account` client exists in every realm
under a fixed id, so the grant is portable — verified by importing into a renamed realm and
reading the effective roles back. `manage-account` carries `manage-account-links`, which is
what permits identity-provider linking.

Not restored, deliberately: `offline_access` and `uma_authorization`. Corpus requests no
offline tokens and enables no authorization services.

### 6. The linking flow is two legs, and both realm grants below are load-bearing

Built and verified live in T-214. The flow the backend drives is:

```
leg 1  GET  {issuer}/protocol/openid-connect/auth?client_id=corpus-linking&prompt=login…
       → Keycloak renders the password form → returns ?code=…&state=…&session_state=…
       → the backend exchanges the code and checks the authenticated `sub` matches
leg 2  GET  {issuer}/broker/google/link?client_id=corpus-linking&nonce=…&hash=…
       → Google consent → back to the backend → grant `read-token` → back to the GUI
```

**Why two legs, measured not assumed.** `/broker/{provider}/link` needs a browser SSO session;
with none it redirects straight back with `link_error=not_logged_in` (probed against this
realm). ROPC creates no browser session, so leg 1 exists to make one. The hash is
`base64url(sha256(nonce + session_state + client_id + provider))`, unpadded — a wrong order or
padded encoding returns `link_error=invalid_hash` and nothing else.

Two realm grants are required and **neither is discoverable from an error message**:

- **`view-clients`** on the backend's service account (`realm-management`). The `read-token`
  grant needs the `broker` client's internal uuid, and resolving it is
  `GET /clients?clientId=broker`. ⚠ The lighter **`query-clients` answers that call with
  `200 []` rather than `403`** — so an under-provisioned realm reports the `broker` client as
  *not existing*. `KeycloakClient.admin_get_client_uuid` raises on the empty list for exactly
  this reason.
- **`manage-account-links` in `corpus-linking`'s scope** (top-level `clientScopeMappings` in
  the artifact). The client ships `fullScopeAllowed: false`, which is deliberate — it grants no
  API access of its own — but that also strips the `account` role Keycloak requires for
  client-initiated linking. Without the mapping, leg 2 answers **`link_error=not_allowed`**,
  naming neither the client nor the role. Holding `manage-account` as a *user* is not enough;
  the **client** needs it in scope.

  ⚠ **Keycloak ignores realm-import keys it does not recognise, silently.** A
  `clientScopeMappings` block with the wrong shape imports cleanly and leaves linking broken,
  so `test_realm_artifact_imports_and_works` imports the artifact into a throwaway realm and
  **reads the mapping back** rather than trusting a `201`.

**Google's authorized redirect URI stays Keycloak's** broker endpoint (step 1) — Corpus's own
callbacks are `corpus-linking` redirect URIs, not Google ones:

```
http://localhost:8000/api/v1/cloud/links/google/callback     # leg 1 returns here
http://localhost:8000/api/v1/cloud/links/google/complete     # leg 2 returns here
```

Both must match a `redirectUris` entry on `corpus-linking` (`http://localhost:8000/*` covers
them) and the origin must match `CLOUD_CALLBACK_BASE_URL`. `CLOUD_RETURN_URL` is where the
browser finally lands, with `?link=linked|failed|denied`.

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
