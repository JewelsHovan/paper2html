"""Tests for the HTML renderer (ticket #009).

Covers all 13 acceptance criteria plus supporting invariants.
All tests are self-contained and use in-memory fixtures only —
no network calls, no external services.
"""

from __future__ import annotations

import dataclasses
import re
import tempfile
from pathlib import Path

from bs4 import BeautifulSoup

from paper2html.models.document import (
    Citation,
    DocumentMetadata,
    Equation,
    Figure,
    IngestionPath,
    NormalizedDocument,
    Section,
)
from paper2html.renderer import check_offline_integrity, render_html
from paper2html.review.schemas import AnnotationBundle

# ---------------------------------------------------------------------------
# Minimal valid 1x1 PNG bytes (no external files needed)
# ---------------------------------------------------------------------------
from io import BytesIO
from PIL import Image

_png_buffer = BytesIO()
Image.new("RGB", (1, 1)).save(_png_buffer, format="PNG")
_TINY_PNG = _png_buffer.getvalue()


# ---------------------------------------------------------------------------
# Document factories
# ---------------------------------------------------------------------------


def make_arxiv_doc() -> NormalizedDocument:
    """Build a minimal arXiv-path NormalizedDocument for testing.

    - 3 sections: Introduction (sec-1), Proof of Main Theorem (sec-2), Results (sec-3)
    - sec-1 references Fig.1 and has at least 3 sentences
    - fig-1 has image bytes and referencing_sentence_ids pointing into sec-1
    - eq-1 is a block MathML equation in sec-1
    - 2 citations
    - ingestion_path = ARXIV_HTML
    """
    # sec-1 text: must have ≥3 sentences (split on ". ") and contain
    # - "gradient descent" (for term-def test)
    # - "Fig.1" (note: no space, so it survives the ". " split intact)
    sec1_text = (
        "We introduce a method using gradient descent optimisation. "
        "In Fig.1 we show the main architecture of our approach. "
        "The results confirm the theoretical predictions"
    )
    # sentence ids for sec-1: sec-1-sent-1, sec-1-sent-2, sec-1-sent-3
    # sec-1-sent-2 contains "Fig.1", so the backlink goes there

    sec1 = Section(
        id="sec-1",
        title="Introduction",
        level=1,
        text_content=sec1_text,
        equation_ids=["eq-1"],
        figure_ids=["fig-1"],
    )
    sec2 = Section(
        id="sec-2",
        title="Proof of Main Theorem",
        level=1,
        text_content="We prove that our method converges. The proof follows by induction.",
    )
    sec3 = Section(
        id="sec-3",
        title="Results",
        level=1,
        text_content="Our results are presented in Table 1. All metrics improve over baseline.",
    )

    fig1 = Figure(
        id="fig-1",
        caption="Overview of the proposed architecture.",
        image_bytes=_TINY_PNG,
        image_mime_type="image/png",
        referencing_sentence_ids=["sec-1-sent-2"],
        alt_text="A test figure",
        gloss="Shows the trend",
    )

    eq1 = Equation(
        id="eq-1",
        mathml="<mrow><mi>x</mi></mrow>",
        display_type="block",
        source_text="The main equation",
    )

    cit1 = Citation(id="ref-1", raw_text="Smith et al., 2023. A great paper.")
    cit2 = Citation(id="ref-2", raw_text="Jones & Lee, 2022. Another great paper.")

    meta = DocumentMetadata(
        title="A Test Paper on Gradient Descent",
        authors=["Alice Smith", "Bob Jones"],
        abstract="We study gradient descent and its properties.",
        ingestion_path=IngestionPath.ARXIV_HTML,
    )

    return NormalizedDocument(
        metadata=meta,
        sections=[sec1, sec2, sec3],
        figures={"fig-1": fig1},
        equations={"eq-1": eq1},
        citations={"ref-1": cit1, "ref-2": cit2},
    )


def make_pdf_doc() -> NormalizedDocument:
    """Build a minimal PDF-path NormalizedDocument for testing.

    - 1 section (Page 1) with a raw_latex equation
    - 1 figure with no referencing_sentence_ids (PDF path)
    - no citations
    - ingestion_path = PDF
    """
    sec1 = Section(
        id="sec-1",
        title="Page 1",
        level=1,
        text_content="This is the main text of the paper with equation x^2.",
        equation_ids=["eq-1"],
        figure_ids=["fig-1"],
    )

    fig1 = Figure(
        id="fig-1",
        caption="A figure from PDF.",
        image_bytes=_TINY_PNG,
        image_mime_type="image/png",
        referencing_sentence_ids=[],
    )

    eq1 = Equation(
        id="eq-1",
        raw_latex="x^2",
        display_type="block",
        source_text="equation x^2",
    )

    meta = DocumentMetadata(
        title="A PDF Paper",
        authors=["Carol Davis"],
        abstract="A PDF abstract.",
        ingestion_path=IngestionPath.PDF,
    )

    return NormalizedDocument(
        metadata=meta,
        sections=[sec1],
        figures={"fig-1": fig1},
        equations={"eq-1": eq1},
        citations={},
    )


def make_full_bundle() -> AnnotationBundle:
    """Build an AnnotationBundle with all reading-aid fields populated."""
    return AnnotationBundle(
        items=[],
        term_definitions={
            # Must match text in sec-1 of make_arxiv_doc()
            "gradient descent": "An iterative optimisation algorithm that minimises a loss function.",
        },
        margin_notes=[
            "Authors hedge on generality",
            "See supplementary for ablations",
        ],
        section_summaries={
            "sec-1": {
                "skim": "A brief skim-level summary.",
                "read": "A more detailed read-level summary.",
                "deep": "A comprehensive deep-level summary.",
            },
            # sec-2 intentionally omitted (no summaries)
        },
    )


def make_empty_bundle() -> AnnotationBundle:
    """Build an AnnotationBundle with no reading-aid fields."""
    return AnnotationBundle(items=[])


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _render_to_tmp(
    doc: NormalizedDocument, bundle: AnnotationBundle
) -> tuple[Path, BeautifulSoup]:
    """Render doc+bundle to a tempfile; return (path, soup)."""
    with tempfile.NamedTemporaryFile(
        suffix=".html", delete=False, mode="w", encoding="utf-8"
    ) as f:
        out_path = Path(f.name)
    render_html(doc, bundle, out_path)
    soup = BeautifulSoup(out_path.read_text(encoding="utf-8"), "html.parser")
    return out_path, soup


# ---------------------------------------------------------------------------
# AC-1: check_offline_integrity returns [] on every rendered output
# ---------------------------------------------------------------------------


def test_check_offline_integrity_clean() -> None:
    """AC-1: integrity checker returns no violations on arXiv rendered output."""
    doc = make_arxiv_doc()
    bundle = make_full_bundle()
    out_path, _ = _render_to_tmp(doc, bundle)
    try:
        violations = check_offline_integrity(out_path)
        assert violations == [], f"Integrity violations: {violations}"
    finally:
        out_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# AC-2: every Figure → <img src="data:...">
# ---------------------------------------------------------------------------


def test_figures_are_data_uris() -> None:
    """AC-2: all img tags have data: URI sources, no http/file paths."""
    doc = make_arxiv_doc()
    bundle = make_empty_bundle()
    out_path, soup = _render_to_tmp(doc, bundle)
    try:
        imgs = soup.find_all("img")
        assert len(imgs) >= 1, "Expected at least one <img> tag"
        for img in imgs:
            src = img.get("src", "")
            assert src.startswith("data:"), f"img src is not a data URI: {src!r}"
            assert not src.startswith("http"), f"img src is http: {src!r}"
            assert not src.startswith("file://"), f"img src is file://: {src!r}"
    finally:
        out_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# AC-3: arXiv MathML passthrough verbatim
# ---------------------------------------------------------------------------


def test_arxiv_mathml_passthrough() -> None:
    """AC-3: MathML content from arXiv equations appears verbatim in output."""
    doc = make_arxiv_doc()
    bundle = make_empty_bundle()
    out_path, _ = _render_to_tmp(doc, bundle)
    try:
        html_text = out_path.read_text(encoding="utf-8")
        # eq-1 has mathml="<mrow><mi>x</mi></mrow>"
        assert "<mrow>" in html_text, "MathML <mrow> not found in output"
        assert "<mi>x</mi>" in html_text, "MathML <mi>x</mi> not found in output"
    finally:
        out_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# AC-4: PDF KaTeX inlined (no CDN)
# ---------------------------------------------------------------------------


def test_pdf_katex_inlined() -> None:
    """AC-4: KaTeX JS is inlined (not from CDN), renderMathInElement present."""
    doc = make_pdf_doc()
    bundle = make_empty_bundle()
    out_path, soup = _render_to_tmp(doc, bundle)
    try:
        html_text = out_path.read_text(encoding="utf-8")

        # No external script[src] tags
        for script in soup.find_all("script", src=True):
            src = script.get("src", "")
            assert not src.startswith("http"), f"External script src: {src!r}"

        # KaTeX content inlined
        assert "katex" in html_text.lower(), "KaTeX not found in rendered PDF output"
        assert "renderMathInElement" in html_text, "renderMathInElement not found"

        # LaTeX delimiter present
        assert r"\[x^2\]" in html_text or "x^2" in html_text, (
            "LaTeX content x^2 not found in output"
        )
    finally:
        out_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# AC-5: summary toggle — 3 levels, 1 toggle button per section
# ---------------------------------------------------------------------------


def test_summary_toggle_three_levels() -> None:
    """AC-5: exactly one toggle button and three data-level divs for sec-1."""
    doc = make_arxiv_doc()
    bundle = make_full_bundle()
    out_path, soup = _render_to_tmp(doc, bundle)
    try:
        sec1_el = soup.find(id="sec-1")
        assert sec1_el is not None, "sec-1 not found in output"

        toggles = sec1_el.find_all(class_="summary-toggle")
        assert len(toggles) == 1, (
            f"Expected 1 summary-toggle in sec-1, got {len(toggles)}"
        )

        summary_block = sec1_el.find(class_="summary-block")
        assert summary_block is not None, "No .summary-block found in sec-1"

        level_divs = summary_block.find_all(attrs={"data-level": True})
        levels = [d.get("data-level") for d in level_divs]
        assert len(set(levels)) == 3, (
            f"Expected 3 distinct data-level values, got {levels}"
        )
        assert set(levels) == {"skim", "read", "deep"}, f"Unexpected levels: {levels}"
    finally:
        out_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# AC-6: term definitions → <span class="term-def" data-def="...">
# ---------------------------------------------------------------------------


def test_term_definitions_spans() -> None:
    """AC-6: term-def spans present statically with non-empty data-def."""
    doc = make_arxiv_doc()
    bundle = make_full_bundle()
    out_path, soup = _render_to_tmp(doc, bundle)
    try:
        term_spans = soup.find_all("span", class_="term-def")
        assert len(term_spans) >= 1, "No .term-def spans found"
        for span in term_spans:
            data_def = span.get("data-def", "")
            assert data_def, f"term-def span has empty data-def: {span}"
    finally:
        out_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# AC-7: citations → <cite data-ref-id="...">
# ---------------------------------------------------------------------------


def test_citations_cite_tags() -> None:
    """AC-7: cite elements with data-ref-id exist for ref-1 and ref-2."""
    doc = make_arxiv_doc()
    bundle = make_empty_bundle()
    out_path, soup = _render_to_tmp(doc, bundle)
    try:
        for ref_id in ("ref-1", "ref-2"):
            cites = soup.find_all("cite", attrs={"data-ref-id": ref_id})
            assert len(cites) >= 1, f"No <cite data-ref-id='{ref_id}'> found"
    finally:
        out_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# AC-8: bidirectional figure/sentence links
# ---------------------------------------------------------------------------


def test_figure_bidirectional_links() -> None:
    """AC-8: figure block has backlink to sentence; sentence has forward link to figure."""
    doc = make_arxiv_doc()
    bundle = make_empty_bundle()
    out_path, soup = _render_to_tmp(doc, bundle)
    try:
        # fig-1 block must contain a link to sec-1-sent-2
        fig_el = soup.find(id="fig-1")
        assert fig_el is not None, "fig-1 element not found"
        backref_links = fig_el.find_all("a", href=re.compile(r"#.*sent.*"))
        assert len(backref_links) >= 1, (
            "No sentence backref link found inside fig-1 block"
        )
        # Must specifically link to sec-1-sent-2
        hrefs = [a.get("href") for a in backref_links]
        assert any("sec-1-sent-2" in str(h) for h in hrefs), (
            f"Expected href containing sec-1-sent-2, got: {hrefs}"
        )

        # sec-1 body must contain a forward link to fig-1
        # The in-text "Fig.1" in sec-1-sent-2 should be linkified
        sec1_el = soup.find(id="sec-1")
        assert sec1_el is not None, "sec-1 element not found"
        fig_forward_links = sec1_el.find_all("a", href="#fig-1")
        assert len(fig_forward_links) >= 1, (
            "No forward link to #fig-1 found in sec-1 body"
        )
    finally:
        out_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# AC-9: proof/derivation sections wrapped in <details class="proof">
# ---------------------------------------------------------------------------


def test_proof_section_wrapped_details() -> None:
    """AC-9: sec-2 (Proof of Main Theorem) is in <details class='proof'>, closed."""
    doc = make_arxiv_doc()
    bundle = make_empty_bundle()
    out_path, soup = _render_to_tmp(doc, bundle)
    try:
        details_els = soup.find_all("details", class_="proof")
        assert len(details_els) >= 1, "No <details class='proof'> found"

        # Find the one containing sec-2's title
        found = False
        for details in details_els:
            summary_el = details.find("summary")
            if summary_el and "Proof" in summary_el.get_text():
                found = True
                # Must NOT have open attribute
                assert details.get("open") is None, (
                    "<details class='proof'> has 'open' attribute — should be closed by default"
                )
                break
        assert found, "No proof details block contains 'Proof' in its summary"
    finally:
        out_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# AC-10: margin notes → <aside class="margin-note">
# ---------------------------------------------------------------------------


def test_margin_notes_aside() -> None:
    """AC-10: number of aside.margin-note elements equals len(bundle.margin_notes)."""
    doc = make_arxiv_doc()
    bundle = make_full_bundle()
    out_path, soup = _render_to_tmp(doc, bundle)
    try:
        asides = soup.find_all("aside", class_="margin-note")
        assert len(asides) == len(bundle.margin_notes), (
            f"Expected {len(bundle.margin_notes)} margin-note asides, got {len(asides)}"
        )
    finally:
        out_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# AC-11: dark mode media query present
# ---------------------------------------------------------------------------


def test_dark_mode_media_query() -> None:
    """AC-11: @media (prefers-color-scheme: dark) present in inline <style>."""
    doc = make_arxiv_doc()
    bundle = make_empty_bundle()
    out_path, _ = _render_to_tmp(doc, bundle)
    try:
        html_text = out_path.read_text(encoding="utf-8")
        assert "prefers-color-scheme" in html_text, (
            "@media (prefers-color-scheme:...) not found in output"
        )
        assert "dark" in html_text, "dark mode keyword not found in output"
    finally:
        out_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# AC-12: no JS frameworks or CDN references
# ---------------------------------------------------------------------------


def test_no_js_frameworks_or_cdn() -> None:
    """AC-12: no React/Vue/Alpine/Angular/jQuery/CDN URLs in output."""
    doc = make_arxiv_doc()
    bundle = make_empty_bundle()
    out_path, _ = _render_to_tmp(doc, bundle)
    try:
        html_text = out_path.read_text(encoding="utf-8").lower()
        forbidden = [
            "react",
            "vue",
            "alpine",
            "angular",
            "jquery",
            "cdn.jsdelivr",
            "cdnjs.cloudflare",
            "unpkg.com",
        ]
        for term in forbidden:
            assert term not in html_text, (
                f"Forbidden JS framework/CDN reference found: {term!r}"
            )
    finally:
        out_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# AC-13: empty AnnotationBundle renders cleanly
# ---------------------------------------------------------------------------


def test_empty_bundle_renders_clean() -> None:
    """AC-13: render_html with empty bundle completes; passes AC-1 and AC-2."""
    doc = make_arxiv_doc()
    bundle = make_empty_bundle()
    out_path, soup = _render_to_tmp(doc, bundle)
    try:
        # AC-1: no integrity violations
        violations = check_offline_integrity(out_path)
        assert violations == [], f"Integrity violations with empty bundle: {violations}"

        # AC-2: img tags use data URIs
        for img in soup.find_all("img"):
            src = img.get("src", "")
            assert src.startswith("data:"), f"img src not a data URI: {src!r}"

        # No reading-aid elements emitted
        assert not soup.find_all("span", class_="term-def"), (
            ".term-def spans found with empty bundle"
        )
        assert not soup.find_all("aside", class_="margin-note"), (
            ".margin-note asides found with empty bundle"
        )
        assert not soup.find_all(class_="summary-block"), (
            ".summary-block found with empty bundle"
        )
    finally:
        out_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Invariant: deterministic rendering
# ---------------------------------------------------------------------------


def test_render_is_deterministic() -> None:
    """Rendering the same inputs twice produces byte-identical output."""
    doc = make_arxiv_doc()
    bundle = make_full_bundle()

    with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as f:
        path1 = Path(f.name)
    with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as f:
        path2 = Path(f.name)
    try:
        render_html(doc, bundle, path1)
        render_html(doc, bundle, path2)
        assert path1.read_bytes() == path2.read_bytes(), (
            "Rendering is not deterministic — byte content differs between two renders"
        )
    finally:
        path1.unlink(missing_ok=True)
        path2.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# check_offline_integrity detects violations
# ---------------------------------------------------------------------------


def test_check_offline_integrity_detects_violations() -> None:
    """Integrity checker catches http, srcset, and url() violations."""
    html_with_violations = """<!DOCTYPE html>
<html><head>
<style>body { background: url(https://evil.com/img.png); }</style>
</head>
<body>
<img src="https://evil.com/x.png" srcset="https://evil.com/x 1x, https://evil.com/y 2x">
</body></html>"""
    with tempfile.NamedTemporaryFile(
        suffix=".html", delete=False, mode="w", encoding="utf-8"
    ) as f:
        tmp_path = Path(f.name)
        f.write(html_with_violations)
    try:
        violations = check_offline_integrity(tmp_path)
        assert len(violations) >= 3, (
            f"Expected ≥3 violations, got {len(violations)}: {violations}"
        )
    finally:
        tmp_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Vendored KaTeX CSS integrity
# ---------------------------------------------------------------------------


def test_offline_integrity_on_vendored_katex_css() -> None:
    """Rendered PDF output passes integrity; vendored CSS has no bare font URLs."""
    doc = make_pdf_doc()
    bundle = make_empty_bundle()
    out_path, _ = _render_to_tmp(doc, bundle)
    try:
        violations = check_offline_integrity(out_path)
        assert violations == [], f"PDF render integrity violations: {violations}"

        # Check vendored CSS directly
        from paper2html.renderer import _ASSETS_DIR

        katex_css_path = _ASSETS_DIR / "katex.min.css"
        katex_css = katex_css_path.read_text(encoding="utf-8")
        assert "url(fonts/" not in katex_css, (
            "Vendored katex.min.css still has raw url(fonts/...) references"
        )
        assert "http" not in katex_css or "http" not in re.findall(
            r"url\(http", katex_css
        ), "Vendored katex.min.css has http URL references"
    finally:
        out_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Sentence anchor superset check
# ---------------------------------------------------------------------------


def test_sentence_anchor_superset() -> None:
    """Emitted span ids ⊇ all referencing_sentence_ids from all figures."""
    doc = make_arxiv_doc()
    bundle = make_empty_bundle()
    out_path, soup = _render_to_tmp(doc, bundle)
    try:
        # Collect all span ids in output
        span_ids = {span.get("id") for span in soup.find_all("span", id=True)}
        # Collect all referencing_sentence_ids from all figures
        required_ids = set()
        for fig in doc.figures.values():
            required_ids.update(fig.referencing_sentence_ids)

        missing = required_ids - span_ids
        assert not missing, f"Sentence span ids missing from output: {missing}"
    finally:
        out_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Bundle extension backcompat
# ---------------------------------------------------------------------------


def test_bundle_extension_backcompat() -> None:
    """AnnotationBundle(items=[]) works; fields default to empty; not a dataclass."""
    bundle = AnnotationBundle(items=[])
    assert bundle.items == []
    assert bundle.term_definitions == {}
    assert bundle.margin_notes == []
    assert bundle.section_summaries == {}

    assert not dataclasses.is_dataclass(AnnotationBundle), (
        "AnnotationBundle must NOT be a @dataclass"
    )


# ---------------------------------------------------------------------------
# check_offline_integrity detects @import violations (ticket #013)
# ---------------------------------------------------------------------------


def test_check_offline_integrity_detects_import_violation() -> None:
    """Integrity checker catches @import external-URL rule inside <style> blocks."""
    html_with_import = """<!DOCTYPE html>
<html><head>
<style>@import url(https://evil.com/x.css);</style>
</head>
<body><p>Hello</p></body></html>"""
    with tempfile.NamedTemporaryFile(
        suffix=".html", delete=False, mode="w", encoding="utf-8"
    ) as f:
        tmp_path = Path(f.name)
        f.write(html_with_import)
    try:
        violations = check_offline_integrity(tmp_path)
        assert any("@import" in v for v in violations), (
            f"Expected an @import violation; got: {violations}"
        )
    finally:
        tmp_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Attribute escaping safety
# ---------------------------------------------------------------------------


def test_attribute_escaping() -> None:
    """Term definitions with quotes and script tags are safely HTML-escaped."""
    tricky_def = 'Contains "quotes" and <script>alert(1)</script> and\nnewlines'
    bundle = AnnotationBundle(
        items=[],
        term_definitions={"gradient descent": tricky_def},
    )
    doc = make_arxiv_doc()
    out_path, soup = _render_to_tmp(doc, bundle)
    try:
        html_text = out_path.read_text(encoding="utf-8")

        # The raw <script>alert(1)</script> must NOT appear unescaped
        assert "<script>alert(1)</script>" not in html_text, (
            "Unescaped <script> found in output — XSS risk!"
        )

        # The term-def span must still be present
        term_spans = soup.find_all("span", class_="term-def")
        assert len(term_spans) >= 1, "term-def span not found after tricky def"

        # data-def attribute can be round-tripped via BeautifulSoup
        span = term_spans[0]
        data_def = span.get("data-def", "")
        assert data_def, "data-def is empty after tricky definition"
    finally:
        out_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Empty document (0 sections / figures / citations)
# ---------------------------------------------------------------------------


def test_empty_document() -> None:
    """A document with no sections/figures/citations renders without error (AC-1/AC-2)."""
    meta = DocumentMetadata(
        title="Empty Paper",
        authors=[],
        abstract="",
        ingestion_path=IngestionPath.ARXIV_HTML,
    )
    doc = NormalizedDocument(metadata=meta)
    bundle = make_empty_bundle()
    out_path, soup = _render_to_tmp(doc, bundle)
    try:
        violations = check_offline_integrity(out_path)
        assert violations == [], f"Empty doc integrity violations: {violations}"
        # No img tags expected
        imgs = soup.find_all("img")
        # All imgs (if any) must be data URIs
        for img in imgs:
            src = img.get("src", "")
            assert src.startswith("data:"), f"img src not data URI: {src!r}"
    finally:
        out_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Assets resolve via __file__
# ---------------------------------------------------------------------------


def test_assets_resolve() -> None:
    """Templates dir and KaTeX VERSION file resolve from the installed package."""
    from paper2html.renderer import _TEMPLATES_DIR, _ASSETS_DIR

    assert _TEMPLATES_DIR.is_dir(), f"Templates dir not found: {_TEMPLATES_DIR}"
    assert (_TEMPLATES_DIR / "paper.html.jinja2").is_file(), (
        "paper.html.jinja2 not found in templates dir"
    )
    assert (_TEMPLATES_DIR / "theme.css").is_file(), (
        "theme.css not found in templates dir"
    )
    assert (_TEMPLATES_DIR / "reading_aids.js").is_file(), (
        "reading_aids.js not found in templates dir"
    )

    assert _ASSETS_DIR.is_dir(), f"KaTeX assets dir not found: {_ASSETS_DIR}"
    version_file = _ASSETS_DIR / "VERSION"
    assert version_file.is_file(), f"KaTeX VERSION file not found: {version_file}"
    version = version_file.read_text().strip()
    assert version, "KaTeX VERSION file is empty"


def test_atomic_render_overwrites_utf8(tmp_path):
    out = tmp_path / "paper.html"
    out.write_text("original")
    doc = make_arxiv_doc()
    doc.metadata.title = "Résumé — 数学"
    assert render_html(doc, AnnotationBundle(items=[]), out) == out
    assert "Résumé — 数学" in out.read_text(encoding="utf-8")
    assert list(tmp_path.iterdir()) == [out]


def test_interrupted_replace_preserves_original_and_cleans_temp(tmp_path, monkeypatch):
    import pytest

    out = tmp_path / "paper.html"
    out.write_bytes(b"original")

    def interrupted(temp, target):
        assert Path(temp).parent == out.parent
        assert Path(temp).read_text(encoding="utf-8").startswith("<!DOCTYPE html>")
        assert target == out
        raise KeyboardInterrupt()

    monkeypatch.setattr("paper2html.renderer.os.replace", interrupted)
    with pytest.raises(KeyboardInterrupt):
        render_html(make_arxiv_doc(), AnnotationBundle(items=[]), out)
    assert out.read_bytes() == b"original"
    assert list(tmp_path.iterdir()) == [out]


def test_partial_write_failure_preserves_original_and_cleans_temp(
    tmp_path, monkeypatch
):
    import pytest
    import paper2html.renderer as renderer

    out = tmp_path / "paper.html"
    out.write_bytes(b"original")
    real_temp = renderer.tempfile.NamedTemporaryFile

    def failing_temp(*args, **kwargs):
        temp = real_temp(*args, **kwargs)
        write = temp.write

        def fail(text):
            write(text[:100])
            raise OSError("disk full")

        temp.write = fail
        return temp

    monkeypatch.setattr(renderer.tempfile, "NamedTemporaryFile", failing_temp)
    with pytest.raises(OSError, match="disk full"):
        render_html(make_arxiv_doc(), AnnotationBundle(items=[]), out)
    assert out.read_bytes() == b"original"
    assert list(tmp_path.iterdir()) == [out]


def test_atomic_no_clobber_concurrent_creator(tmp_path, monkeypatch):
    import os
    import pytest

    destination = tmp_path / "result.html"
    original_link = os.link

    def competing_link(source, target):
        target.write_text("concurrent winner")
        return original_link(source, target)

    monkeypatch.setattr("paper2html.renderer.os.link", competing_link)
    with pytest.raises(FileExistsError):
        render_html(
            make_arxiv_doc(), AnnotationBundle(items=[]), destination, overwrite=False
        )
    assert destination.read_text() == "concurrent winner"
    assert list(tmp_path.iterdir()) == [destination]


def test_render_preserves_existing_mode_and_sane_new_mode(tmp_path):
    import stat

    destination = tmp_path / "existing.html"
    destination.write_text("old")
    destination.chmod(0o640)
    render_html(make_arxiv_doc(), AnnotationBundle(items=[]), destination)
    assert stat.S_IMODE(destination.stat().st_mode) == 0o640
    new = tmp_path / "new.html"
    render_html(make_arxiv_doc(), AnnotationBundle(items=[]), new, overwrite=False)
    assert stat.S_IMODE(new.stat().st_mode) == 0o644


def test_atomic_publication_replaces_symlink_not_target(tmp_path):
    import pytest

    target = tmp_path / "target.html"
    target.write_text("untouched target")
    destination = tmp_path / "paper.html"
    destination.symlink_to(target)
    with pytest.raises(FileExistsError):
        render_html(
            make_arxiv_doc(), AnnotationBundle(items=[]), destination, overwrite=False
        )
    assert destination.is_symlink()
    assert target.read_text() == "untouched target"
    render_html(make_arxiv_doc(), AnnotationBundle(items=[]), destination)
    assert not destination.is_symlink()
    assert destination.read_text().startswith("<!DOCTYPE html>")
    assert target.read_text() == "untouched target"
    assert set(tmp_path.iterdir()) == {target, destination}
