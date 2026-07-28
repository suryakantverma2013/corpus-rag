"""Malware screening at the head of the ingestion worker (T-207, R-31 §8.12 / R-32 §8.13).

R-31 moved screening **out of** the synchronous upload path so a 50 MB scan cannot stall
FR-ING-02's `202`, and placed it here: before `PARSING`, with nothing parsing the file
until it passes. R-32 then made the default implementation real rather than a no-op seam.
Two screens run on the same pass:

1. **Structural** — cheap, in-process, no network, and covering things signature matching
   does not. A "DOCX" whose ZIP container holds `vbaProject.bin` is a macro-bearing DOCM in
   disguise; magic-byte sniffing (T-202) cannot separate the two because both are ZIP. PDFs
   are screened for active content.
2. **Signature** — ClamAV over INSTREAM (`app.services.clamav`).

Structural runs **first**: it is free, needs no daemon, and a positive means the ClamAV
round-trip is wasted work.

**Failure policy (R-31(1), R-32).** A detection from either screen terminates the job in
FR-ING-01 `FAILED` with a distinct reason code and purges the object — **no new document
state** (the enum stays at 11, so no migration and no FR-KBM-04 label). An *unreachable*
scanner is not a bypass: it fails the job closed and retryably, the deliberate opposite of
the fail-open rate limiter (T-105, NFR-SEC-07), because the risk runs the other way here.

**This module is worker-side only** — it imports PyMuPDF, and R-31 keeps parsing and
screening isolated from the API process. A guard test asserts no module under `app/api/`
imports it. The socket client lives in `app.services.clamav` precisely so the readiness
probe can reach clamd without importing this module.
"""

from __future__ import annotations

import enum
import io
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

import structlog

from app.config import Settings, get_settings
from app.services.clamav import ClamAVClient, get_clamav_client

log = structlog.get_logger(__name__)

# Reason codes written to `knowledge_jobs.error_code`. Kept distinct so an operator can
# tell a signature hit from a structural one without reading the message.
REASON_MALWARE = "MALWARE_DETECTED"
REASON_MACRO = "MACRO_DOCUMENT_DETECTED"
REASON_ACTIVE_CONTENT = "ACTIVE_CONTENT_DETECTED"

# The Office macro payload. Always `word/vbaProject.bin` in practice, but matched on the
# basename so a repackaged container cannot dodge the check by moving it.
_VBA_MEMBER = "vbaproject.bin"

# PDF action subtypes that *do something* when the document opens. `/GoTo` and friends only
# move the viewport, and an `/OpenAction` holding a bare destination array is navigation —
# treating those as findings would fail a large share of perfectly ordinary PDFs, since
# "open at page 1, fit width" is written by most producers.
_ACTIVE_PDF_ACTIONS = ("/JavaScript", "/JS", "/Launch", "/ImportData", "/SubmitForm")


class ScanVerdict(enum.StrEnum):
    CLEAN = "CLEAN"
    INFECTED = "INFECTED"


@dataclass(frozen=True, slots=True)
class ScanResult:
    """Outcome of the pre-`PARSING` screen.

    `reason_code` and `detail` are only meaningful when `verdict` is `INFECTED`; they map
    onto `knowledge_jobs.error_code` and `documents.error_message` respectively.
    """

    verdict: ScanVerdict
    reason_code: str | None = None
    signature: str | None = None
    detail: str | None = None

    @property
    def infected(self) -> bool:
        return self.verdict is ScanVerdict.INFECTED

    @classmethod
    def clean(cls) -> ScanResult:
        return cls(verdict=ScanVerdict.CLEAN)


@runtime_checkable
class Scanner(Protocol):
    """The R-31 seam. Implementations never raise on a *detection* — that is a `ScanResult`.

    They do raise when they cannot reach a verdict (an unreachable `clamd`), which is what
    makes the job fail closed rather than admitting an unscanned document.
    """

    async def scan(self, payload: bytes, *, filename: str) -> ScanResult: ...


class StructuralOnlyScanner:
    """Structural screens with no signature engine (`SCANNER_BACKEND=structural`).

    For a dev box that cannot host a ~2 GB `clamd`. Note this is *not* a no-op: the
    structural checks always run. R-38(7) deliberately provides no fully-clean backend, so
    there is no setting anywhere that disables screening outright.
    """

    async def scan(self, payload: bytes, *, filename: str) -> ScanResult:
        return _structural_scan(payload, filename=filename)


class MalwareScanner:
    """Structural screens followed by the ClamAV signature screen (`SCANNER_BACKEND=clamav`)."""

    def __init__(self, client: ClamAVClient) -> None:
        self._client = client

    async def scan(self, payload: bytes, *, filename: str) -> ScanResult:
        structural = _structural_scan(payload, filename=filename)
        if structural.infected:
            # Skip the network round-trip; the document is already rejected.
            return structural

        report = await self._client.scan(payload)
        if report.infected:
            log.warning(
                "scanner.signature_detection", filename=filename, signature=report.signature
            )
            return ScanResult(
                verdict=ScanVerdict.INFECTED,
                reason_code=REASON_MALWARE,
                signature=report.signature,
                detail=f"malware signature detected: {report.signature or 'unnamed'}",
            )
        return ScanResult.clean()


def build_scanner(
    settings: Settings | None = None, *, client: ClamAVClient | None = None
) -> Scanner:
    """Select the screening backend. Never inferred — `SCANNER_BACKEND` says so explicitly."""
    resolved = settings or get_settings()
    if resolved.scanner.backend == "structural":
        log.warning(
            "scanner.signature_screen_disabled",
            reason="SCANNER_BACKEND=structural — structural checks still run (R-38(7))",
        )
        return StructuralOnlyScanner()
    return MalwareScanner(client or get_clamav_client())


# ---- structural screens ------------------------------------------------------------


def _structural_scan(payload: bytes, *, filename: str) -> ScanResult:
    """Dispatch the structural screen on the (already magic-byte-validated) suffix."""
    suffix = Path(filename).suffix.lower()
    if suffix == ".docx":
        return _screen_docx(payload)
    if suffix == ".pdf":
        return _screen_pdf(payload)
    # CSV and Markdown are plain UTF-8 by the time they get here — T-202 validated the
    # whole payload for control bytes and binary markers, and neither format has a
    # container to hide anything in.
    return ScanResult.clean()


def _screen_docx(payload: bytes) -> ScanResult:
    """Detect a macro-bearing DOCM wearing a .docx extension (R-32(2)).

    Reads the **central directory only** — nothing is decompressed. That matters because
    this runs *before* T-203's decompression-bomb pre-flight, so inflating even a small
    member here (say, to inspect `[Content_Types].xml`) would hand a bomb the one execution
    window R-31(3) is designed to remove. Member *names* are enough for the check R-32
    specifies.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names = archive.namelist()
    except zipfile.BadZipFile, OSError, ValueError:
        # Not a readable ZIP. Deliberately *not* a scanner finding: the parser owns that
        # taxonomy and will report `CORRUPT_DOCUMENT` with a message the user can act on.
        # Pre-empting it here would relabel every corrupt upload as malware.
        return ScanResult.clean()

    for name in names:
        if name.rsplit("/", 1)[-1].lower() == _VBA_MEMBER:
            log.warning("scanner.macro_detection", member=name)
            return ScanResult(
                verdict=ScanVerdict.INFECTED,
                reason_code=REASON_MACRO,
                detail=(
                    f"file declares itself a DOCX but its container holds {name!r} — it is a "
                    "macro-enabled DOCM in disguise"
                ),
            )
    return ScanResult.clean()


def _screen_pdf(payload: bytes) -> ScanResult:
    """Flag PDF active content: `/JavaScript`, `/OpenAction`, `/EmbeddedFile` (R-32(2))."""
    import pymupdf  # imported lazily so the module cost is paid only for PDFs

    findings: list[str] = []
    try:
        with pymupdf.open(stream=payload, filetype="pdf") as doc:
            if doc.needs_pass:
                # Encrypted: nothing is readable, and the parser reports
                # `ENCRYPTED_DOCUMENT`. Same reasoning as the BadZipFile branch above.
                return ScanResult.clean()

            catalog = doc.pdf_catalog()

            if _key_present(doc, catalog, "Names/JavaScript"):
                findings.append("document-level /JavaScript")

            for key in ("OpenAction", "AA"):
                subtype = _active_action(doc, catalog, key)
                if subtype is not None:
                    findings.append(f"/{key} dispatching {subtype}")

            if doc.embfile_count() > 0:
                findings.append(f"/EmbeddedFile ({doc.embfile_count()} attachment(s))")
    except Exception as exc:  # noqa: BLE001 — see below
        # A PDF PyMuPDF cannot open is the parser's `CORRUPT_DOCUMENT`, not a detection.
        # Swallowing broadly is correct here: the screen is defence in depth, and ClamAV
        # still sees the same bytes.
        log.debug("scanner.pdf_screen_skipped", error=f"{type(exc).__name__}: {exc}")
        return ScanResult.clean()

    if not findings:
        return ScanResult.clean()

    log.warning("scanner.active_content_detection", findings=findings)
    return ScanResult(
        verdict=ScanVerdict.INFECTED,
        reason_code=REASON_ACTIVE_CONTENT,
        detail="PDF carries active content: " + "; ".join(findings),
    )


def _key_present(doc: object, xref: int, key: str) -> bool:
    kind, _ = doc.xref_get_key(xref, key)  # type: ignore[attr-defined]
    return kind not in ("null", None)


def _active_action(doc: object, xref: int, key: str) -> str | None:
    """Return the action subtype if `key` dispatches something executable, else None.

    An `/OpenAction` is only interesting when it *runs* something. Most PDFs carry one
    holding a destination array (`[3 0 R /XYZ 0 792 0]` — "open here"), and a handful of
    producers write a `/GoTo` dictionary; flagging those would reject a large fraction of
    ordinary documents while catching nothing. So the value is resolved and its `/S`
    subtype checked against the executable set.
    """
    kind, value = doc.xref_get_key(xref, key)  # type: ignore[attr-defined]
    if kind in ("null", None):
        return None
    if kind == "array":
        return None  # a bare destination — navigation only

    raw = value
    if kind == "xref":
        # `value` is "12 0 R"; resolve and re-read as a dictionary.
        try:
            target = int(str(value).split()[0])
        except ValueError, IndexError:
            return None
        raw = doc.xref_object(target, compressed=True)  # type: ignore[attr-defined]

    text = str(raw)
    for action in _ACTIVE_PDF_ACTIONS:
        if action in text:
            return action
    return None
