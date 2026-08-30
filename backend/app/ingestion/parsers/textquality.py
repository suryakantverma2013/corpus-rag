"""Text-layer quality measurement for PDFs (T-728, FR-ING-10, R-100 §8.90).

**What this measures and why it is not what it looks like.** A PDF can extract characters
that bear no relation to what the page displays. The route this detects is a font that
embeds its own program and supplies **no `ToUnicode` mapping**: extraction then falls back
to the font's assumed encoding, and for a symbolic font that assumption is wrong, so
``f(x)`` comes out as ``fsxd`` and ``P(x)`` as ``Psxd`` — real ASCII letters, forming
plausible-looking words, which is exactly why nothing downstream notices. The prose in such
a document is usually fine (its body font is well-behaved), so retrieval works and only
*grounding* fails; the user reads an ordinary abstention and is advised to rephrase a
question that cannot be rephrased. That is B-007, and the missing thing was a signal.

**The rule is: an embedded font program with no `ToUnicode`.** Both halves were measured,
and the obvious one-term version is wrong in each direction:

* **Without `embedded`** it flags healthy documents. A non-embedded standard-14 font
  legitimately carries no `ToUnicode` — Helvetica with ``/WinAnsiEncoding`` extracts
  ``f(x) = 1 - x if x <= -1`` perfectly — so *absence of `ToUnicode`* is not itself a defect.
* **With the FontDescriptor `Symbolic` flag added**, which is the natural third term, it
  misses the defect. That flag is set by the producer and was measured wrong in both
  directions: the broken mathematics font declares Flags=34 (*Nonsymbolic*) while emitting
  the garbage, and a healthy body font declares Flags=4 (*Symbolic*). Adding it drops
  detection on the sample document from 8.39% to 1.12%. R-100(3) refuses it — *a flag a
  producer sets by hand is evidence about the producer, not about the file.*

**A false-positive class is accepted, not engineered away (R-100(4)):** an old PDF whose
*simple* embedded font relies on a standard encoding has no `ToUnicode` and extracts
correctly. It would be reported here. That is tolerable only because the signal is
**advisory** — R-100(5) — and it is why nothing may ever gate on this number.

**Cost is asymmetric by construction (R-100(6)).** :meth:`TextQuality.page` takes the
span-level pass **only when the page actually uses a suspect font**, so a healthy document
pays one cheap ``get_fonts()`` per page and never apportions characters: measured +1.3% on a
660-page manual (0 of 150 pages attributed) against +137.6% on the broken book. Font
dictionaries are resolved once per document and cached, so the cost of the cheap half scales
with the font count rather than the page count.

Nothing here raises. Detection **fails open** (FR-ING-10): a font dictionary that cannot be
read counts as not-suspect, and a page that cannot be attributed contributes nothing, so the
worst outcome is a document that ingests exactly as it did before this module existed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    import pymupdf


def base_font_name(name: str) -> str:
    """Strip a subset prefix: ``ABCDEF+Times`` -> ``Times``.

    Span font names carry the prefix and `get_fonts` base names sometimes do not, so the two
    will not match without this. **It is load-bearing rather than cosmetic:** an unstripped
    comparison silently matches nothing, every font reads as unknown, and the ratio comes out
    a confident ``0.0`` on a thoroughly broken document — which is how the first draft of the
    probe behind R-100 reported five healthy documents and one broken one as all identical.
    """
    return name.split("+", 1)[1] if len(name) > 7 and name[6] == "+" else name


def _ink(text: str) -> int:
    """Count characters that were actually **drawn**, ignoring whitespace.

    FR-ING-10 measures *"the proportion of extracted characters drawn from such fonts"*, and
    whitespace is the one thing in an extraction that is largely **not** drawn from any font:
    the extractor synthesises it to convey layout. Counting it put the two halves of the ratio
    on different footings — the numerator comes from spans, the denominator from the page text
    the parser chunks — and those two disagree about whitespace by a document-dependent amount.

    **Measured, which is the only reason this is a rule and not a preference.** `pdf.py` extracts
    with ``sort=True``, and against plain ``get_text("text")`` that inflates the character count
    by **1.06x to 2.09x** across the six-document sample — while the *non-whitespace* counts are
    identical to within 1% (0.99-1.00x). So `sort=True` duplicates no glyphs; it only pads. But
    counting the padding diluted the ratio by that same 1.06-2.09x, unevenly: a heavily columned
    manual was diluted twice as much as a linear one, so a single global threshold did not mean
    the same thing from one document to the next. The broken sample read **4.03%** where R-100
    measured 8.39%, i.e. the shipped detector was up to twice as insensitive as the ruling that
    tuned it — against a threshold R-100(5) chose *for* sensitivity.

    Ignoring whitespace on both sides makes the ratio independent of whitespace convention
    altogether, so it is immune to ``sort=True`` and to :func:`normalize` alike, and it restores
    the ruling's own figure: the sample now measures **8.26%** against R-100's 8.39%, with all
    five healthy documents unchanged at 0.00% across 1,296 pages.

    **The cheap path survives, which is what made this the right fix.** Taking the denominator
    from spans would have measured the same thing, but only by running the span pass on every
    page — destroying the asymmetry in R-100(6) that lets the feature ship on by default.
    """
    return sum(1 for character in text if not character.isspace())


def _font_is_suspect(document: pymupdf.Document, xref: int) -> bool:
    """No `ToUnicode`, and a font program is embedded. See the module docstring."""
    try:
        keys = document.xref_get_keys(xref)
        if "ToUnicode" in keys:
            return False
        descriptor: int | None = None
        if "FontDescriptor" in keys:
            descriptor = int(document.xref_get_key(xref, "FontDescriptor")[1].split()[0])
        elif "DescendantFonts" in keys:
            # A Type0 font keeps its descriptor one level down, on the descendant.
            raw = document.xref_get_key(xref, "DescendantFonts")[1]
            descendant = int(raw.strip("[] ").split()[0])
            if "FontDescriptor" in document.xref_get_keys(descendant):
                descriptor = int(document.xref_get_key(descendant, "FontDescriptor")[1].split()[0])
        if descriptor is None:
            return False
        return any(k.startswith("FontFile") for k in document.xref_get_keys(descriptor))
    except Exception:
        # Fails open (FR-ING-10). An unreadable font dictionary is not evidence of a defect,
        # and raising here would fail a document over a diagnostic.
        return False


@dataclass(slots=True)
class TextQuality:
    """Accumulates the FR-ING-10 measurement across a document's pages.

    Held by the parser for the length of one parse and never checkpointed. It carries counts
    only — no page text — so it cannot become a second copy of the document.
    """

    #: Non-whitespace characters extracted from fonts with no usable Unicode mapping.
    suspect_chars: int = 0
    #: All non-whitespace extracted characters, the denominator.
    total_chars: int = 0
    #: Font-object verdicts, resolved once per document rather than once per page.
    _verdicts: dict[str, bool] | None = None

    def page(self, document: pymupdf.Document, page: pymupdf.Page, text: str) -> None:
        """Fold one page in, given the text the parser already extracted from it.

        ``text`` is passed rather than re-extracted so the denominator is exactly what the
        parser went on to chunk, and so the cheap path costs no second extraction.
        """
        self.total_chars += _ink(text)
        if self._verdicts is None:
            self._verdicts = {}
        try:
            fonts = page.get_fonts(full=True)
        except Exception:
            return  # fail open
        suspect_here = False
        for font in fonts:
            name = base_font_name(font[3])
            if name not in self._verdicts:
                self._verdicts[name] = _font_is_suspect(document, font[0])
            suspect_here = suspect_here or self._verdicts[name]
        if not suspect_here:
            # The whole reason a healthy document is nearly free: no span pass at all.
            return
        try:
            blocks = page.get_text("dict")["blocks"]
        except Exception:
            return  # fail open
        for block in blocks:
            for line in block.get("lines", ()):
                for span in line.get("spans", ()):
                    if self._verdicts.get(base_font_name(span["font"])):
                        self.suspect_chars += _ink(span["text"])

    @property
    def ratio(self) -> float | None:
        """Suspect share of extracted characters, or ``None`` when nothing was extracted.

        ``None`` rather than ``0.0`` for a document with no characters: a scanned PDF
        extracts nothing, and reporting it as *perfect* text quality would be a claim about
        a text layer that does not exist. The two are different facts and only one of them
        is good news.
        """
        if self.total_chars <= 0:
            return None
        return self.suspect_chars / self.total_chars
