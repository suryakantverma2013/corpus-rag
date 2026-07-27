"""Upload content validation — magic-byte type detection (T-202, R-31(3)).

R-31 (§8.12) moved malware scanning out of the synchronous upload path to the head of
the ingestion worker (T-207), and in exchange made two controls mandatory *here*:
file-type validation by **magic-byte sniffing rather than the declared extension**, and
pre-storage size enforcement (that half lives in the upload service).

Detection is two-phase, because a ZIP central directory sits at the *end* of a file but
an ``.exe`` must be rejected without first reading 50 MB of it:

* :func:`sniff_family` runs on the leading few hundred bytes — enough to accept a PDF,
  recognise a ZIP container, or reject an outright binary.
* :func:`detect_file_type` runs on the complete payload and returns the authoritative
  :class:`DetectedType`, confirming the DOCX container and validating text.

**Why CSV and MD need a different rule.** Neither has magic bytes — there is no signature
to match, so a positive test is impossible and pretending otherwise would be security
theatre. The rule is instead *negative plus strict-decode*: the head must not match any
known binary or markup container (:data:`_BINARY_SIGNATURES`), and the whole payload must
decode as UTF-8 with no NUL and no C0 control characters beyond ordinary whitespace. Real
binary payloads fail that within their first bytes, and the textual smuggling vectors
(shebang scripts, HTML/SVG/XML) are on the deny-list. What survives is text — exactly what
the T-203 CSV/MD parsers are built to eat — and it is still streamed to ClamAV before
``PARSING`` (R-32, §8.13).

CSV and MD are deliberately **not** discriminated from each other: the declared extension
picks which parser T-203 dispatches. Delimiter-counting heuristics would reject valid
files for no security gain — a CSV parsed as Markdown is a cosmetic bug, not a
vulnerability. The extension is never the security boundary; it selects a parser among
content already validated as text, and a payload whose *family* contradicts its extension
is rejected outright.

UTF-16 is not accepted. Its ASCII range is NUL-padded, so it cannot pass the control-byte
rule, and no CSV/MD ingestion path needs it.

No new dependency: `python-magic` needs a libmagic binary (awkward on the Windows dev
box), and `filetype`/`puremagic` can tell neither a DOCX from any other ZIP nor anything
at all about CSV/MD — the two cases that actually matter. Same reasoning as R-32's
in-tree ClamAV client.
"""

from __future__ import annotations

import enum
import io
import zipfile

from pydantic import BaseModel

# --- accepted formats (FR-ERR-03, fixed by R-11) ------------------------------

PDF_SUFFIX = ".pdf"
DOCX_SUFFIX = ".docx"
CSV_SUFFIX = ".csv"
MD_SUFFIX = ".md"

MIME_BY_SUFFIX = {
    PDF_SUFFIX: "application/pdf",
    DOCX_SUFFIX: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    CSV_SUFFIX: "text/csv",
    MD_SUFFIX: "text/markdown",
}

#: Plain-text members of the accepted set — within this pair the extension decides.
_TEXT_SUFFIXES = frozenset({CSV_SUFFIX, MD_SUFFIX})

# --- signatures ---------------------------------------------------------------

#: Bytes handed to :func:`sniff_family`; covers every signature below with room spare.
HEAD_BYTES = 512

_PDF_MAGIC = b"%PDF-"
_ZIP_MAGIC = b"PK\x03\x04"
_UTF8_BOM = b"\xef\xbb\xbf"

#: Members every real DOCX (an OOXML package) carries. An XLSX, PPTX, JAR, ODT or a
#: plain ZIP renamed `.docx` fails this. Reading the central directory decompresses
#: nothing, so there is no zip-bomb exposure here — the decompression-ratio caps
#: R-31(3) also requires belong to the T-203 parsers, which do the extracting.
_DOCX_REQUIRED_MEMBERS = ("[Content_Types].xml", "word/document.xml")

# NOTE(T-207/R-32): a `vbaProject.bin` member marks a disguised macro-bearing DOCM.
# That check is deliberately NOT here — R-32 places structural screening at the head of
# the ingestion worker, alongside the ClamAV pass. Do not migrate it into this module.

#: Containers and executables that must never be accepted as CSV/MD. Matched at offset 0.
_BINARY_SIGNATURES: tuple[bytes, ...] = (
    b"MZ",  # DOS/PE executable
    b"\x7fELF",  # ELF executable
    b"\xca\xfe\xba\xbe",  # Java class / Mach-O fat binary
    b"\xfe\xed\xfa\xce",  # Mach-O
    b"\xfe\xed\xfa\xcf",  # Mach-O 64-bit
    b"\xd0\xcf\x11\xe0",  # OLE2 compound file (legacy .doc/.xls — a macro carrier)
    b"\x1f\x8b",  # gzip
    b"Rar!\x1a\x07",  # RAR
    b"7z\xbc\xaf\x27\x1c",  # 7-Zip
    b"BZh",  # bzip2
    b"\xfd7zXZ",  # xz
    b"\x89PNG",  # PNG
    b"GIF8",  # GIF
    b"\xff\xd8\xff",  # JPEG
    b"{\\rtf",  # RTF — can embed OLE objects
    b"#!",  # shebang script
)

#: Markup that is valid UTF-8 yet an active-content vector, so it must not arrive as
#: `.md`. Matched case-insensitively after stripping leading whitespace.
_MARKUP_PREFIXES: tuple[bytes, ...] = (b"<?xml", b"<!doctype html", b"<html", b"<svg")

#: C0 controls tolerated in a text upload. Everything else below 0x20 — NUL above all —
#: plus DEL means binary content wearing a `.csv`/`.md` name.
_ALLOWED_CONTROLS = frozenset(b"\t\n\r\f\v")
_FORBIDDEN_CONTROLS = frozenset([b for b in range(0x20) if b not in _ALLOWED_CONTROLS] + [0x7F])
#: Complement of the above. `bytes.translate(None, _TEXT_SAFE)` deletes every safe byte
#: in one C-level pass, leaving only offenders — an O(n) scan rather than one pass per
#: forbidden byte, which matters at 50 MB.
_TEXT_SAFE = bytes(b for b in range(256) if b not in _FORBIDDEN_CONTROLS)


class UnsupportedFileTypeError(Exception):
    """The payload is not an FR-ERR-03 format, or contradicts its declared extension."""


class Family(enum.StrEnum):
    """Coarse class decided from the leading bytes alone."""

    PDF = "PDF"
    ZIP = "ZIP"
    TEXT = "TEXT"


class DetectedType(BaseModel):
    """Authoritative result of sniffing. ``suffix`` names the stored object."""

    suffix: str
    mime_type: str


def normalized_suffix(filename: str) -> str:
    """Lower-cased final extension of ``filename`` (``""`` when there is none)."""
    name = filename.replace("\\", "/").rsplit("/", 1)[-1]
    _, dot, ext = name.rpartition(".")
    return f".{ext.lower()}" if dot else ""


def sniff_family(head: bytes) -> Family:
    """Classify a payload from its leading bytes, or raise.

    Called on the first chunk, so an executable is rejected before the remainder of a
    50 MB body is read.
    """
    if head.startswith(_PDF_MAGIC):
        # Strictly offset 0. Readers tolerate a leading-junk offset, and that tolerance
        # is exactly the polyglot trick — one file that is a valid archive or a valid
        # PDF depending on who opens it.
        return Family.PDF
    if head.startswith(_ZIP_MAGIC):
        return Family.ZIP

    for signature in _BINARY_SIGNATURES:
        if head.startswith(signature):
            raise UnsupportedFileTypeError(f"binary signature {signature!r} is not accepted")

    stripped = head.lstrip().lower()
    for prefix in _MARKUP_PREFIXES:
        if stripped.startswith(prefix):
            raise UnsupportedFileTypeError(f"markup payload {prefix!r} is not an accepted type")

    return Family.TEXT


def _validate_text(payload: bytes) -> None:
    """Confirm the whole payload is control-free UTF-8 (see the module docstring)."""
    body = payload.removeprefix(_UTF8_BOM)
    try:
        body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UnsupportedFileTypeError("text upload is not valid UTF-8") from exc

    offenders = body.translate(None, _TEXT_SAFE)
    if offenders:
        raise UnsupportedFileTypeError(
            f"text upload contains control byte 0x{offenders[0]:02x} — not a text file"
        )


def _validate_docx(payload: bytes) -> None:
    """Confirm a ZIP container really is an OOXML word-processing package."""
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names = set(archive.namelist())
    except zipfile.BadZipFile as exc:
        raise UnsupportedFileTypeError("file is not a readable ZIP container") from exc

    missing = [member for member in _DOCX_REQUIRED_MEMBERS if member not in names]
    if missing:
        raise UnsupportedFileTypeError(f"ZIP is not a DOCX package — missing {missing}")


def detect_file_type(payload: bytes, *, filename: str) -> DetectedType:
    """Return the authoritative type of ``payload``, or raise.

    The declared extension is cross-checked against the detected family: a `.pdf` that
    sniffs as a ZIP, or a `.md` that sniffs as a PDF, is rejected — that contradiction is
    precisely the spoofing case R-31(3) exists to catch.
    """
    if not payload:
        raise UnsupportedFileTypeError("file is empty")

    declared = normalized_suffix(filename)
    if declared not in MIME_BY_SUFFIX:
        raise UnsupportedFileTypeError(f"extension {declared!r} is not an accepted format")

    family = sniff_family(payload[:HEAD_BYTES])
    if family is Family.PDF:
        detected = PDF_SUFFIX
    elif family is Family.ZIP:
        _validate_docx(payload)
        detected = DOCX_SUFFIX
    else:
        _validate_text(payload)
        # Both text formats are indistinguishable by content, so the extension decides —
        # safely, because the bytes are already known to be text.
        detected = declared if declared in _TEXT_SUFFIXES else ""

    if detected != declared:
        raise UnsupportedFileTypeError(
            f"file contents ({detected or family}) do not match the {declared!r} extension"
        )
    return DetectedType(suffix=detected, mime_type=MIME_BY_SUFFIX[detected])
