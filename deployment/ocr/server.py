#!/usr/bin/env python3
"""The OCR sidecar's HTTP contract (T-217, R-88 §8.78, NFR-SEC-09).

Stdlib only, and deliberately thin. Everything this process does is: bound the request,
hand the bytes to `tesseract` under fixed flags, and translate its two output files into
JSON. It holds no state, opens no outbound connection, and writes nothing outside a
per-request temporary directory.

**Why a server of ours rather than a community wrapper image.** Two properties decide it,
and no third-party wrapper offers both: the invocation flags must be fixed by us (R-88(1)
— determinism), and the response must carry **per-word confidence**, which R-88(8)'s floor
needs and a plain text output does not return. TSV is the only Tesseract output mode that
carries it.

**What is deliberately NOT here.** No engine parameter is settable over the wire. `--oem`,
`--psm` and the DPI the caller rendered at are the inputs that change recognised text, so
exposing them would make the sidecar's output a function of the request rather than of the
pinned image — the exact property R-88(1) forbids. The one caller-supplied value is the
language, and it is validated against a character class before it reaches `argv`, because
it does reach `argv`.

**The aggregation rule is NOT here either.** The mean confidence R-88(8) thresholds on is
computed by `app.services.ocr` in the backend, where it is unit-tested. This process reports
per-word confidences and nothing derived from them.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BIND_HOST = os.environ.get("OCR_BIND_HOST", "0.0.0.0")  # noqa: S104 - container-internal
BIND_PORT = int(os.environ.get("OCR_BIND_PORT", "8884"))

#: Refused with 413. The client refuses first, on its own ceiling; this is the backstop for
#: anything that reaches the port without going through it.
MAX_IMAGE_BYTES = int(os.environ.get("OCR_MAX_IMAGE_BYTES", str(32 * 1024 * 1024)))

#: Wall-clock ceiling on one `tesseract` run. A page that exceeds it answers 504 rather than
#: holding the connection until the caller's own timeout fires.
SUBPROCESS_TIMEOUT_SECONDS = float(os.environ.get("OCR_SUBPROCESS_TIMEOUT_SECONDS", "120"))

DEFAULT_LANGUAGES = os.environ.get("OCR_DEFAULT_LANGUAGES", "eng")

#: How much of a rejected body to read and discard before answering.
#:
#: A server may answer before it has read the request body, and this one does — but closing
#: the socket with the body still in flight makes the peer's write fail, and the reply is lost
#: with it: the caller then reports "unreachable" for what is really "too large". That is the
#: same early-reply/hang-up trap `app.services.clamav` documents from the other side, and the
#: diagnosis it produces sends an operator hunting a network fault. So a refused request is
#: drained first — bounded, because draining is the attacker's lever if it is not.
DRAIN_LIMIT_BYTES = 128 * 1024 * 1024
_DRAIN_CHUNK = 64 * 1024

# Fixed recognition parameters (see the module docstring). `--oem 1` is the LSTM engine;
# `--psm 3` is fully automatic page segmentation without orientation detection, which is
# the right default for a rendered PDF page whose orientation the producer already fixed.
OEM = "1"
PSM = "3"

LANGUAGE_HEADER = "X-Ocr-Lang"

#: Tesseract language codes are alphanumeric with underscores (`eng`, `chi_sim`, `deu_frak`)
#: and combine with `+`. Validated because the value reaches `argv`.
_LANGUAGES_RE = re.compile(r"\A[A-Za-z0-9_]{1,32}(?:\+[A-Za-z0-9_]{1,32})*\Z")

_TSV_LEVEL_WORD = "5"


class EngineError(RuntimeError):
    """`tesseract` failed, or answered with something unusable."""


def _run(args: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell, validated language
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _engine_version() -> str:
    proc = _run(["tesseract", "--version"], timeout=15)
    output = proc.stdout or proc.stderr
    first = output.splitlines()[0] if output else ""
    # "tesseract 5.5.0" -> "5.5.0"
    return first.split(" ", 1)[1].strip() if " " in first else first.strip()


def _installed_languages() -> list[str]:
    proc = _run(["tesseract", "--list-langs"], timeout=15)
    lines = (proc.stdout or proc.stderr).splitlines()
    # The first line is a header ("List of available languages ...").
    return sorted(line.strip() for line in lines[1:] if line.strip())


def _tessdata_digests() -> dict[str, str]:
    """Short SHA-256 of every loaded traineddata file.

    This exists because the language pack is mounted as a named volume, and a Docker named
    volume seeds itself from the image **only while it is empty**. So rebuilding the image
    with a different pinned pack leaves an existing deployment on the old one, silently —
    and R-88(1) makes "silently recognising differently" the failure this whole design is
    built to prevent. Reporting the digest turns that into something a probe can see.
    """
    prefix = pathlib.Path(os.environ.get("TESSDATA_PREFIX", "/usr/share/tesseract-ocr/5/tessdata"))
    digests: dict[str, str] = {}
    try:
        for path in sorted(prefix.glob("*.traineddata")):
            digests[path.stem] = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    except OSError as exc:  # pragma: no cover - unreadable mount
        print(f"ocr.tessdata_unreadable path={prefix} error={exc}", file=sys.stderr)
    return digests


# Resolved once at startup: the values describe the image and its mounts, not the request,
# and hashing a few megabytes on every 30-second healthcheck would be waste.
ENGINE_VERSION = _engine_version()
ENGINE_LANGUAGES = _installed_languages()
TESSDATA_DIGESTS = _tessdata_digests()


def _parse_tsv(tsv: str) -> list[dict[str, object]]:
    """Word rows from Tesseract's TSV, in reading order.

    Only `level == 5` rows are words; the rest describe pages, blocks, paragraphs and lines
    and carry `conf = -1` with empty text. Blank text is dropped here so the caller's mean is
    taken over things that were actually recognised.
    """
    words: list[dict[str, object]] = []
    lines = tsv.splitlines()
    for line in lines[1:]:  # row 0 is the header
        fields = line.split("\t")
        if len(fields) < 12 or fields[0] != _TSV_LEVEL_WORD:
            continue
        # `text` is the last column; join defensively in case it ever contains a tab.
        text = "\t".join(fields[11:]).strip()
        if not text:
            continue
        try:
            confidence = float(fields[10])
        except ValueError:
            continue
        words.append({"text": text, "conf": confidence})
    return words


def recognize(image: bytes, languages: str) -> dict[str, object]:
    """Recognise one page raster. Returns the `/recognize` response body."""
    with tempfile.TemporaryDirectory(prefix="corpus-ocr-") as tmp:
        source = os.path.join(tmp, "page")
        base = os.path.join(tmp, "out")
        with open(source, "wb") as handle:
            handle.write(image)

        # One recognition pass, two outputs: `txt` is Tesseract's own reading-order text and
        # `tsv` carries the per-word confidences. Asking for both here rather than running
        # the engine twice keeps them consistent *by construction* — two passes could in
        # principle disagree, and reconciling them would be a heuristic.
        proc = _run(
            [
                "tesseract",
                source,
                base,
                "--oem",
                OEM,
                "--psm",
                PSM,
                "-l",
                languages,
                "txt",
                "tsv",
            ],
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
        if proc.returncode != 0:
            # The exit code crosses the wire; the engine's own words do NOT. Tesseract falls
            # back to reading an unrecognised file as a *list of image paths*, so its stderr
            # quotes the input back — and the input is a page raster, i.e. document content.
            # Echoing it would put document-derived bytes into the backend's logs, which
            # R-43(5)'s "no payload text, ever" forbids one requirement along. The detail
            # stays inside the container, where `docker logs` reaches it and the trust
            # boundary already is.
            print(
                f"ocr.engine_failed exit={proc.returncode} stderr={proc.stderr.strip()[:500]!r}",
                file=sys.stderr,
            )
            raise EngineError(f"tesseract exited {proc.returncode}")

        try:
            text = pathlib.Path(f"{base}.txt").read_text(encoding="utf-8")
            tsv = pathlib.Path(f"{base}.tsv").read_text(encoding="utf-8")
        except OSError as exc:
            raise EngineError(f"tesseract produced no output files: {exc}") from exc

    return {
        "text": text,
        "words": _parse_tsv(tsv),
        "engine": {
            "tesseract": ENGINE_VERSION,
            "langs": ENGINE_LANGUAGES,
            "tessdata": TESSDATA_DIGESTS,
        },
    }


class Handler(BaseHTTPRequestHandler):
    # Keep-alive, so the worker's pooled client is not reconnecting once per page.
    protocol_version = "HTTP/1.1"
    server_version = "corpus-ocr"
    sys_version = ""

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's contract
        if self.path.split("?", 1)[0] != "/health":
            self._send_json(404, {"error": "not found"})
            return
        self._send_json(
            200,
            {
                "status": "ok",
                "tesseract": ENGINE_VERSION,
                "langs": ENGINE_LANGUAGES,
                "tessdata": TESSDATA_DIGESTS,
            },
        )

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's contract
        if self.path.split("?", 1)[0] != "/recognize":
            self._send_json(404, {"error": "not found"})
            return

        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            # No streaming path: the body has to be bounded before it is read, and a chunked
            # upload cannot be.
            self._send_json(411, {"error": "Content-Length required"})
            return
        try:
            length = int(raw_length)
        except ValueError:
            self._send_json(400, {"error": "malformed Content-Length"})
            return
        if length <= 0:
            self._send_json(400, {"error": "empty body"})
            return
        if length > MAX_IMAGE_BYTES:
            self._drain(length)
            self._send_json(413, {"error": f"image exceeds {MAX_IMAGE_BYTES} bytes"}, close=True)
            return

        languages = self.headers.get(LANGUAGE_HEADER, DEFAULT_LANGUAGES).strip()
        if not _LANGUAGES_RE.fullmatch(languages):
            self._drain(length)
            self._send_json(400, {"error": f"malformed {LANGUAGE_HEADER}"}, close=True)
            return

        image = self.rfile.read(length)
        if len(image) != length:
            self._send_json(400, {"error": "truncated body"})
            return

        try:
            body = recognize(image, languages)
        except subprocess.TimeoutExpired:
            self._send_json(504, {"error": "recognition timed out"})
            return
        except EngineError as exc:
            self._send_json(500, {"error": str(exc)})
            return
        self._send_json(200, body)

    def _drain(self, length: int) -> None:
        """Read and discard a rejected body so the reply survives (see DRAIN_LIMIT_BYTES)."""
        remaining = min(length, DRAIN_LIMIT_BYTES)
        while remaining > 0:
            chunk = self.rfile.read(min(_DRAIN_CHUNK, remaining))
            if not chunk:
                return
            remaining -= len(chunk)

    def _send_json(self, status: int, body: dict[str, object], *, close: bool = False) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        if close:
            # A refused request may have left unread bytes on the wire (a body larger than
            # DRAIN_LIMIT_BYTES); reusing that connection would read them as the next request.
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args: object) -> None:
        # One line per request on stdout, so `docker logs` is useful without being a firehose.
        sys.stdout.write("ocr.request " + (fmt % args) + "\n")


def main() -> None:
    print(
        f"ocr.ready tesseract={ENGINE_VERSION} langs={','.join(ENGINE_LANGUAGES)} "
        f"tessdata={TESSDATA_DIGESTS} bind={BIND_HOST}:{BIND_PORT}"
    )
    ThreadingHTTPServer((BIND_HOST, BIND_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
