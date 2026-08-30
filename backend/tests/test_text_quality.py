"""FR-ING-10 / R-100: measuring a PDF's text-layer quality (T-728).

**These build their own PDFs rather than reading a fixture**, for the reason R-100(1) had to
measure six real documents in the first place: the defect is a property of a *font
dictionary*, so a fixture that merely contains garbled characters would prove nothing about
the detector — it would prove something about a string. Every PDF here is constructed so
that the font's `ToUnicode` and embedding state are what the assertion is about.

The one thing these cannot cover is the real defect end to end, because reproducing a broken
TeX mathematics font needs a font program, not a PDF writer. That half was measured directly
against the document from B-007 (8.39% suspect characters, `P(x)` extracting as `Psxd`) and
against five healthy technical documents over 1,296 pages, all of which measured 0.00% — the
evidence R-100 rests on and which no unit test can restate.
"""

from __future__ import annotations

import pymupdf
import pytest

from app.ingestion.parsers.textquality import TextQuality, base_font_name


def _one_page(text: str, *, fontfile: str | None = None) -> pymupdf.Document:
    doc = pymupdf.open()
    page = doc.new_page()
    if fontfile is None:
        page.insert_text((60, 100), text, fontsize=12)
    else:
        page.insert_font(fontname="EMB", fontfile=fontfile)
        page.insert_text((60, 100), text, fontname="EMB", fontsize=12)
    reopened = pymupdf.open(stream=doc.tobytes(), filetype="pdf")
    doc.close()
    return reopened


def _measure(doc: pymupdf.Document) -> TextQuality:
    quality = TextQuality()
    for index in range(doc.page_count):
        page = doc.load_page(index)
        quality.page(doc, page, page.get_text("text", sort=True))
    return quality


class TestTheEmbeddedTermIsLoadBearing:
    """R-100(2): without it the detector flags healthy documents."""

    def test_a_non_embedded_base_14_font_is_never_suspect(self) -> None:
        """The measurement that refutes the obvious one-term detector.

        Helvetica carries **no `ToUnicode`** and extracts perfectly, because
        `/WinAnsiEncoding` already maps to Unicode. A detector keyed on "no `ToUnicode`"
        alone would report this document — which is why R-100(2) requires the font program
        to be embedded, and why that term cannot be dropped as redundant.
        """
        doc = _one_page("f(x) = 1 - x if x <= -1")
        try:
            assert doc[0].get_text().strip() == "f(x) = 1 - x if x <= -1"
            fonts = doc[0].get_fonts(full=True)
            assert fonts, "the fixture must actually use a font"
            assert "ToUnicode" not in doc.xref_get_keys(fonts[0][0])
            assert _measure(doc).ratio == 0.0
        finally:
            doc.close()

    def test_a_descriptor_with_no_font_program_is_not_suspect(self) -> None:
        """The *second* half of the embedded term, and mutation testing is why it exists.

        A non-embedded base-14 font has no `FontDescriptor` at all, so the test above returns
        early and never reaches the `FontFile` check — which meant removing that check left
        the whole suite green (§8.65(5): a duplicated guard hides its twin). A font that *has*
        a descriptor but embeds no program is the case only this reaches: real, because
        descriptors carry metrics for fonts the viewer is expected to supply.
        """
        from app.ingestion.parsers.textquality import _font_is_suspect

        class Stub:
            def xref_get_keys(self, xref: int) -> tuple[str, ...]:
                # 1 = the font object: a descriptor, and deliberately no ToUnicode.
                # 2 = the descriptor: metrics only, no FontFile* of any flavour.
                return ("FontDescriptor", "BaseFont") if xref == 1 else ("Flags", "ItalicAngle")

            def xref_get_key(self, xref: int, key: str) -> tuple[str, str]:  # noqa: ARG002
                return ("xref", "2 0 R")

        assert _font_is_suspect(Stub(), 1) is False  # type: ignore[arg-type]

    def test_a_descriptor_with_an_embedded_program_and_no_tounicode_is_suspect(self) -> None:
        """The positive twin, so the assertion above cannot pass by always answering False."""
        from app.ingestion.parsers.textquality import _font_is_suspect

        class Stub:
            def xref_get_keys(self, xref: int) -> tuple[str, ...]:
                return ("FontDescriptor", "BaseFont") if xref == 1 else ("Flags", "FontFile3")

            def xref_get_key(self, xref: int, key: str) -> tuple[str, str]:  # noqa: ARG002
                return ("xref", "2 0 R")

        assert _font_is_suspect(Stub(), 1) is True  # type: ignore[arg-type]


class TestTheMeasurement:
    def test_characters_are_attributed_to_the_font_that_drew_them(self) -> None:
        """The attribution path itself, driven directly — and it exists because the obvious
        fixture proved nothing.

        A PDF written by PyMuPDF reports its font as `Times New Roman Regular` from
        `get_fonts` and as `TimesNewRomanPSMT` on the spans, so the verdict map is keyed by a
        name no span ever uses and **nothing is attributed at all**. A test asserting `0.0`
        against that fixture passes whatever the detector does — confirmed by mutation:
        ignoring `ToUnicode` entirely left it green. Driving `page` with consistent names is
        what actually exercises the measurement.
        """
        suspect, healthy = 1, 2

        class Page:
            def get_fonts(self, full: bool = False) -> list:  # noqa: ARG002, FBT001, FBT002
                return [
                    (suspect, "", "", "BadFont", "F1", ""),
                    (healthy, "", "", "GoodFont", "F2", ""),
                ]

            def get_text(self, kind: str) -> dict:  # noqa: ARG002
                return {
                    "blocks": [
                        {
                            "lines": [
                                {
                                    "spans": [
                                        {"font": "ABCDEF+BadFont", "text": "1234567890"},
                                        {"font": "GoodFont", "text": "abcdefghij"},
                                    ]
                                }
                            ]
                        }
                    ]
                }

        class Doc:
            def xref_get_keys(self, xref: int) -> tuple[str, ...]:
                if xref == healthy:
                    return ("ToUnicode", "FontDescriptor")
                if xref == suspect:
                    return ("FontDescriptor",)
                return ("Flags", "FontFile3")  # the descriptor both point at

            def xref_get_key(self, xref: int, key: str) -> tuple[str, str]:  # noqa: ARG002
                return ("xref", "9 0 R")

        quality = TextQuality()
        quality.page(Doc(), Page(), "1234567890abcdefghij")  # type: ignore[arg-type]
        # Ten characters from the suspect font, ten from the healthy one.
        assert quality.suspect_chars == 10
        assert quality.total_chars == 20
        assert quality.ratio == pytest.approx(0.5)

    def test_the_ratio_does_not_move_when_only_whitespace_does(self) -> None:
        """The whitespace convention of the extraction must not change the measurement.

        This is the defect the ink-only rule fixes, and it was real rather than theoretical:
        `pdf.py` extracts with ``sort=True``, which pads for layout without drawing a single
        extra glyph, and counting that padding diluted the ratio by **1.06x to 2.09x** — a
        different amount per document, so one global threshold meant different things on
        different files. The broken sample read 4.03% against the 8.39% R-100 measured and
        tuned `PARSER_TEXT_QUALITY_MIN_RATIO` against.

        Here the *same* spans are folded in beside two very different page texts. The ratio
        must be identical. Under ``len`` it is 0.5 against 0.25 — the test that would have
        caught the shipped defect.
        """
        suspect = 1

        class Page:
            def get_fonts(self, full: bool = False) -> list:  # noqa: ARG002, FBT001, FBT002
                return [(suspect, "", "", "BadFont", "F1", "")]

            def get_text(self, kind: str) -> dict:  # noqa: ARG002
                return {"blocks": [{"lines": [{"spans": [{"font": "BadFont", "text": "12345"}]}]}]}

        class Doc:
            def xref_get_keys(self, xref: int) -> tuple[str, ...]:
                return ("FontDescriptor",) if xref == suspect else ("Flags", "FontFile2")

            def xref_get_key(self, xref: int, key: str) -> tuple[str, str]:  # noqa: ARG002
                return ("xref", "9 0 R")

        tight = TextQuality()
        tight.page(Doc(), Page(), "1234567890")  # type: ignore[arg-type]

        padded = TextQuality()
        # The same ten drawn characters, laid out across columns and lines.
        padded.page(Doc(), Page(), "12345\n\n     67890\n\n          ")  # type: ignore[arg-type]

        assert tight.total_chars == padded.total_chars == 10
        assert tight.ratio == padded.ratio == pytest.approx(0.5)

    def test_nothing_extracted_is_none_rather_than_zero(self) -> None:
        """`None` is *not measured*; 0.0 is *measured and clean*. A scan is the first, and
        calling it the second would be a claim about a text layer that does not exist."""
        doc = pymupdf.open()
        doc.new_page()
        reopened = pymupdf.open(stream=doc.tobytes(), filetype="pdf")
        doc.close()
        try:
            assert _measure(reopened).ratio is None
        finally:
            reopened.close()

    def test_the_ratio_is_over_characters_not_pages(self) -> None:
        """A page is not the unit. R-100(1) counts characters because a document whose body
        text is fine and whose formulae are not is exactly the case B-007 describes — 29% of
        its chunks were affected while every page looked populated."""
        quality = TextQuality()
        quality.total_chars = 1000
        quality.suspect_chars = 84
        assert quality.ratio == pytest.approx(0.084)


class TestTheCostIsAsymmetric:
    """R-100(6) is a claim about cost, so it gets a guard rather than a paragraph.

    A healthy document must never pay for span-level attribution — that is the whole reason
    this ships on by default (+1.3% measured on a 660-page manual, against +137.6% on the
    broken book). Without this, the early return is invisible to every other test here,
    because removing it changes only the bill and not the answer.
    """

    def test_a_page_with_no_suspect_font_never_takes_the_span_pass(self) -> None:
        calls: list[str] = []

        class Page:
            def get_fonts(self, full: bool = False) -> list:  # noqa: ARG002, FBT001, FBT002
                return [(1, "", "", "GoodFont", "F1", "")]

            def get_text(self, kind: str) -> dict:
                calls.append(kind)
                return {"blocks": []}

        class Doc:
            def xref_get_keys(self, xref: int) -> tuple[str, ...]:
                return ("ToUnicode", "FontDescriptor")

            def xref_get_key(self, xref: int, key: str) -> tuple[str, str]:  # noqa: ARG002
                return ("xref", "9 0 R")

        TextQuality().page(Doc(), Page(), "clean text")  # type: ignore[arg-type]
        assert calls == [], f"the cheap path called get_text{calls}"

    def test_a_page_with_a_suspect_font_does_take_it(self) -> None:
        """The twin, so the assertion above cannot pass by never calling anything at all."""
        calls: list[str] = []

        class Page:
            def get_fonts(self, full: bool = False) -> list:  # noqa: ARG002, FBT001, FBT002
                return [(1, "", "", "BadFont", "F1", "")]

            def get_text(self, kind: str) -> dict:
                calls.append(kind)
                return {"blocks": []}

        class Doc:
            def xref_get_keys(self, xref: int) -> tuple[str, ...]:
                return ("FontDescriptor",) if xref == 1 else ("FontFile2",)

            def xref_get_key(self, xref: int, key: str) -> tuple[str, str]:  # noqa: ARG002
                return ("xref", "9 0 R")

        TextQuality().page(Doc(), Page(), "dirty text")  # type: ignore[arg-type]
        assert calls == ["dict"]


class TestItFailsOpen:
    """FR-ING-10: a diagnostic must never fail a document."""

    def test_an_unreadable_page_contributes_nothing_and_does_not_raise(self) -> None:
        class Hostile:
            def get_fonts(self, full: bool = False) -> list:  # noqa: ARG002, FBT001, FBT002
                raise RuntimeError("font table is corrupt")

        quality = TextQuality()
        quality.page(None, Hostile(), "some text")  # type: ignore[arg-type]
        # 8, not 9: the space is not a drawn character. See `_ink`.
        assert quality.total_chars == 8
        assert quality.suspect_chars == 0

    def test_the_denominator_still_counts_when_fonts_cannot_be_read(self) -> None:
        """The text was extracted and will be chunked, so it belongs in the denominator even
        though it could not be attributed. Dropping it would make an unreadable document
        look *cleaner* the more of it failed to inspect."""

        class Hostile:
            def get_fonts(self, full: bool = False) -> list:  # noqa: ARG002, FBT001, FBT002
                raise RuntimeError("nope")

        quality = TextQuality()
        quality.page(None, Hostile(), "x" * 50)  # type: ignore[arg-type]
        assert quality.ratio == 0.0


class TestSubsetPrefixes:
    """The stripping is load-bearing, not cosmetic — see the function's docstring."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("ABCDEF+TimesLTStd-Roman", "TimesLTStd-Roman"),
            ("TimesLTStd-Roman", "TimesLTStd-Roman"),
            ("Times", "Times"),  # too short to carry a prefix
            ("ABCDE+X", "ABCDE+X"),  # 5-char tag is not the PDF subset form
        ],
    )
    def test_a_subset_tag_is_stripped_and_nothing_else_is(self, raw: str, expected: str) -> None:
        assert base_font_name(raw) == expected
