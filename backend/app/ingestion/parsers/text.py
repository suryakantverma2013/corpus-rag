"""Text normalisation — the single choke point every parser passes through (T-203).

Extraction libraries disagree about line endings, stray control bytes and Unicode
composition; retrieval and FR-ING-03's `chunk_hash` do not. Normalising in one place
means a `.docx` and its `.pdf` export produce identical text for identical paragraphs,
so a re-upload in the other format reuses chunks instead of re-embedding everything.

**Changing anything here changes every fingerprint** — bump
:data:`~app.ingestion.parsers.base.PREPROCESSING_VERSION` in the same commit.

Kept deliberately small. Tempting extras that are NOT done, and why:

* **De-hyphenation** of PDF line-wrapped words ("inter-\\nnational") — would also mangle
  legitimately hyphenated terms, and it is unfixable after the fact because the original
  is gone from the chunk.
* **Case folding / stop-word removal** — destroys information the embedding model uses.
* **NFKC** — collapses ligatures and full-width forms, but also rewrites superscripts and
  maths symbols in ways that change meaning. NFC only composes what is already equivalent.

Code points are written as integers rather than literal characters on purpose: a source
file whose behaviour depends on invisible bytes is one careless editor away from a silent
change to every fingerprint in the corpus.
"""

from __future__ import annotations

import re
import unicodedata

#: Every C0 control except tab and newline, plus DEL. Extraction of a damaged file can
#: emit these; they survive into embeddings as noise.
_CONTROL_CODES = tuple(code for code in range(0x20) if code not in (0x09, 0x0A)) + (0x7F,)

#: Zero-width characters, soft hyphen, and a BOM appearing mid-stream: invisible to a
#: reader, and they split words for the tokenizer.
_INVISIBLE_CODES = (0x00AD, 0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF)

#: NBSP, the U+2000 block, and the narrow/medium/ideographic spaces. Folded *to* a plain
#: space rather than deleted — they are real word separators, but leaving them verbatim
#: makes an otherwise-identical paragraph hash differently across formats.
_SPACE_CODES = (0x00A0, 0x1680, *range(0x2000, 0x200B), 0x202F, 0x205F, 0x3000)

_TRANSLATION: dict[int, str | None] = (
    dict.fromkeys(_CONTROL_CODES)
    | dict.fromkeys(_INVISIBLE_CODES)
    | dict.fromkeys(_SPACE_CODES, " ")
)

#: Three or more newlines collapse to one paragraph break. PDF extraction is especially
#: prone to long runs of them around figures.
_BLANK_RUN = re.compile(r"\n{3,}")

#: Trailing whitespace on any line — a pure artifact of fixed-width layout.
_TRAILING_WS = re.compile(r"[ \t]+$", re.MULTILINE)


def normalize(text: str) -> str:
    """Return ``text`` in canonical ingestion form.

    Idempotent by construction — ``normalize(normalize(x)) == normalize(x)`` — which the
    test suite pins, because a non-idempotent step would make a re-parse of unchanged
    bytes produce a different `chunk_hash` and force a pointless re-embed.
    """
    if not text:
        return ""

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.translate(_TRANSLATION)
    text = unicodedata.normalize("NFC", text)
    text = _TRAILING_WS.sub("", text)
    text = _BLANK_RUN.sub("\n\n", text)
    return text.strip()


def is_blank(text: str) -> bool:
    """True when ``text`` holds nothing a reader or an embedding model could use."""
    return not text or not text.strip()
