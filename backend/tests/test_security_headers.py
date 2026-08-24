"""The browser security headers the edge serves (T-719, R-95, NFR-SEC-11).

These read `deployment/` from a backend test, which `tests/test_env_templates.py` established:
the deployment is part of the product, and a configuration file nothing checks is a
configuration file that drifts.

**Every assertion here guards a failure that is silent in production.** A CSP does not fail
loudly — it makes a thing stop happening. A wrong script hash means the pre-paint theme script
never runs and a light-theme user gets a dark flash on every cold load; a missing `blob:` means
every FR-CIT-07 figure is blank; a policy that reached `/auth/` would break account linking.
None of those raise, none of them appear in a log, and none of them fail any other test.
"""

from __future__ import annotations

import base64
import hashlib
import pathlib
import re

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
NGINX = REPO_ROOT / "deployment" / "nginx"
SECURITY_INC = NGINX / "security.inc"
DEFAULT_CONF = NGINX / "default.conf"
INDEX_HTML = REPO_ROOT / "frontend" / "index.html"
FRONTEND_DOCKERFILE = REPO_ROOT / "frontend" / "Dockerfile"

#: The SPA locations. Each one either serves the entry document or a subresource of it, and
#: each must carry the headers — `add_header` is not inherited into a block that sets one of
#: its own, and two of these set `Cache-Control`.
_SPA_LOCATIONS = ("location /assets/", "location = /index.html", "location / ")

#: Locations that must NOT carry them. `/auth/` is the load-bearing one: Keycloak sends its
#: own CSP and a browser enforces the intersection of both.
_NON_SPA_LOCATIONS = ("location /api/", "location /health", "location /auth/")


def _conf(path: pathlib.Path) -> str:
    """A config file with its comments stripped, so a directive is never matched in prose."""
    return "\n".join(
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("#")
    )


def _policy() -> str:
    match = re.search(r'add_header\s+Content-Security-Policy\s+"([^"]+)"', _conf(SECURITY_INC))
    assert match, "no Content-Security-Policy add_header in security.inc"
    return match.group(1)


def _directive(name: str) -> list[str]:
    """The source expressions of one CSP directive."""
    for part in _policy().split(";"):
        tokens = part.split()
        if tokens and tokens[0] == name:
            return tokens[1:]
    return []


def _inline_scripts(html: pathlib.Path) -> list[bytes]:
    """Every inline `<script>` body, as the exact bytes a browser would hash."""
    return re.findall(rb"<script>(.*?)</script>", html.read_bytes(), re.S)


def _sha256(body: bytes) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(body).digest()).decode() + "'"


# ---- the inline script hash, which is the brittle one ---------------------------------


def test_the_policy_pins_the_hash_of_the_inline_theme_script() -> None:
    """R-58(1)'s pre-paint script is the one inline script in the product, and it is hashed.

    The failure this catches is invisible: edit that script — even reindent it — and the hash
    stops matching, the browser silently refuses to run it, and every user with a stored
    `light` preference sees a dark frame until React mounts. Nothing raises and nothing logs.

    If this fails after a checkout rather than after an edit, suspect line endings: the hash is
    over exact bytes and `.gitattributes` pins `*.html` to LF for precisely this reason.
    """
    bodies = _inline_scripts(INDEX_HTML)
    assert len(bodies) == 1, (
        f"{len(bodies)} inline scripts in index.html; the policy hashes exactly one. "
        "Add the new script's hash to security.inc, or make it an external file."
    )
    assert _sha256(bodies[0]) in _directive("script-src"), (
        f"script-src does not carry the hash of the inline script in {INDEX_HTML.name}.\n"
        f"    expected: {_sha256(bodies[0])}\n"
        f"    got:      {' '.join(_directive('script-src'))}\n"
        "Update the add_header line in deployment/nginx/security.inc."
    )


def test_the_hashed_script_carries_no_carriage_returns() -> None:
    """The hash is over bytes, so a CRLF checkout would change it and break the theme silently.

    Asserted separately from the hash comparison because the two fail for different reasons and
    the fix differs: this one is `.gitattributes` and a fresh checkout, not an edit to the conf.
    """
    assert b"\r" not in _inline_scripts(INDEX_HTML)[0], (
        "the inline script has CRLF line endings, so its hash differs from the one the policy "
        "pins. Check .gitattributes and re-check out frontend/index.html."
    )


# ---- what the policy must and must not permit ------------------------------------------


def test_the_policy_admits_the_figure_blob() -> None:
    """FR-CIT-07 renders an object URL, because the figure route is authenticated (NFR-SEC-10).

    `img-src 'self'` is the obvious thing to write and would blank every figure.
    """
    assert "blob:" in _directive("img-src"), (
        "img-src does not admit blob: — every FR-CIT-07 figure would render blank"
    )


def test_the_policy_admits_every_origin_the_document_actually_loads() -> None:
    """Whatever `index.html` reaches for must be in the policy, and this reads it rather than
    trusting a list — swapping the font provider without touching the CSP is otherwise a
    silent loss of NFR-VIS-03's typefaces, in production only.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")
    origins = {
        f"{m.group(1)}://{m.group(2)}" for m in re.finditer(r"(https?)://([^/\"'\s]+)", html)
    }
    policy = _policy()
    missing = sorted(o for o in origins if o not in policy)
    assert not missing, (
        f"index.html loads from {missing}, which the policy does not permit. "
        "Add the origin to the right directive, or stop loading from it."
    )


@pytest.mark.parametrize("unsafe", ["'unsafe-inline'", "'unsafe-eval'", "'strict-dynamic'"])
def test_the_policy_contains_no_escape_hatch(unsafe: str) -> None:
    """The measured position (T-719) is that none of these is needed, so none may appear.

    `'unsafe-inline'` in `script-src` would make the whole directive decorative;
    in `style-src` it is not needed either, because React writes its `style={{…}}` props
    through the CSSOM and CSP does not gate that. `'strict-dynamic'` is listed because it
    would silently neutralise the `'self'` allow-list if someone added it while debugging.
    """
    assert unsafe not in _policy(), f"{unsafe} in the Content-Security-Policy"


def test_the_policy_denies_by_default_and_pins_the_three_that_do_not_fall_back() -> None:
    """`base-uri`, `form-action` and `frame-ancestors` have no fallback to `default-src`.

    Without them a `default-src 'none'` policy reads as watertight and leaves base-tag
    injection, form exfiltration and framing wide open.
    """
    assert _directive("default-src") == ["'none'"]
    assert _directive("base-uri") == ["'none'"]
    assert _directive("form-action") == ["'self'"]
    assert _directive("frame-ancestors") == ["'none'"]


# ---- where the headers are applied, which is where nginx is easy to get wrong ----------


@pytest.mark.parametrize("location", _SPA_LOCATIONS)
def test_every_spa_location_includes_the_headers(location: str) -> None:
    """`add_header` is NOT inherited into a block that defines one of its own.

    Two of these set `Cache-Control`, so a `server`-level policy would be silently absent from
    both — including `= /index.html`, which is the block that actually serves the document,
    because `try_files $uri /index.html` performs an internal redirect back into location
    matching.
    """
    conf = _conf(DEFAULT_CONF)
    start = conf.index(location)
    block = conf[start : conf.index("}", start)]
    assert "security.inc" in block, f"{location.strip()} does not include security.inc"


@pytest.mark.parametrize("location", _NON_SPA_LOCATIONS)
def test_the_proxied_locations_do_not_carry_the_headers(location: str) -> None:
    """`/auth/` is the one that matters, and it is not a style preference.

    Keycloak sends its own `Content-Security-Policy` on its HTML pages (measured: `frame-src
    'self'; frame-ancestors 'self'; object-src 'none';`). A browser given two CSP headers
    enforces **both**, so ours would also apply to Keycloak's login page — whose inline
    scripts and styles are not in our hash list — and break the FR-AUT-11 account-linking
    journey, which is the only browser flow this product has through Keycloak.
    """
    conf = _conf(DEFAULT_CONF)
    start = conf.index(location)
    block = conf[start : conf.index("}", start)]
    assert "security.inc" not in block, (
        f"{location.strip()} includes security.inc. For /auth/ this doubles Keycloak's own "
        "CSP and breaks account linking; for /api/ and /health it puts a document policy on "
        "responses that are not documents."
    )


def test_the_image_ships_the_include() -> None:
    """A file nginx is told to include and the image does not carry makes nginx exit at start.

    Loud rather than silent — but it fails the whole deployment, not just the header, so it is
    worth one line here rather than a rollback.
    """
    assert "security.inc" in FRONTEND_DOCKERFILE.read_text(encoding="utf-8")


def test_the_supporting_headers_are_present_and_hsts_is_not() -> None:
    """The three that ride along, and the one deliberately withheld.

    HSTS is withheld because this stack listens on plain HTTP and `docs/DEPLOYMENT.md` makes
    TLS the responsibility of whatever terminates it. Sending it from here would be a promise
    the deployment cannot keep — and an operator who tested on a real hostname without TLS
    would pin their own browser away from their own deployment, irreversibly for a year.
    """
    conf = _conf(SECURITY_INC)
    for header in ("X-Content-Type-Options", "Referrer-Policy", "Permissions-Policy"):
        assert f"add_header {header} " in conf, f"{header} is not set"
    assert "Strict-Transport-Security" not in conf, (
        "HSTS is deliberately not sent from this edge — see the note in security.inc"
    )


def test_every_header_survives_an_error_response() -> None:
    """Without `always`, nginx omits `add_header` on anything outside 2xx/3xx.

    A policy that lapses on the error page is a policy with a hole in exactly the responses an
    attacker can most easily provoke.
    """
    missing = [
        line.strip()
        for line in _conf(SECURITY_INC).splitlines()
        if line.strip().startswith("add_header") and not line.rstrip().endswith("always;")
    ]
    assert not missing, f"add_header without `always`: {missing}"
