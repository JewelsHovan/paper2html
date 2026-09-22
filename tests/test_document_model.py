"""Tests for paper2html.models.document — ticket #005.

Tests cover all 10 ACs plus the MUST-FIX scenarios.  No conftest changes,
no @pytest.mark.integration markers.  All fixtures are module-level or inline.
"""

from __future__ import annotations

import json
import sys
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from bs4 import BeautifulSoup
from PIL import Image

# ---------------------------------------------------------------------------
# Module-level fixtures
# ---------------------------------------------------------------------------


def _make_png(width: int = 1, height: int = 1) -> bytes:
    """Return real PNG bytes (includes \x89PNG header)."""
    buf = BytesIO()
    Image.new("RGB", (width, height)).save(buf, format="PNG")
    return buf.getvalue()


MINIMAL_1x1_PNG: bytes = _make_png(1, 1)
# Use 16x16 for pymupdf insert_image which may reject 1x1
MINIMAL_16x16_PNG: bytes = _make_png(16, 16)


def _make_metadata(
    entry_id: str = "http://arxiv.org/abs/2301.00001v1",
) -> SimpleNamespace:
    """Return a duck-typed metadata object matching arxiv.Result interface."""
    return SimpleNamespace(
        title="Test Paper",
        authors=[SimpleNamespace(name="A B"), SimpleNamespace(name="C D")],
        summary="Abstract text.",
        entry_id=entry_id,
    )


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


# ---------------------------------------------------------------------------
# 1. test_model_importable — AC-1
# ---------------------------------------------------------------------------


def test_model_importable() -> None:
    """AC-1: all 7 names importable from both module and package."""
    from paper2html.models.document import (
        Citation,
        DocumentMetadata,
        Equation,
        Figure,
        IngestionPath,
        NormalizedDocument,
        Section,
        from_arxiv_html,
        from_pdf,
    )
    from paper2html.models import (
        Citation as Citation2,
        DocumentMetadata as DM2,
        Equation as Equation2,
        Figure as Figure2,
        IngestionPath as IP2,
        NormalizedDocument as ND2,
        Section as Section2,
        from_arxiv_html as fa2,
        from_pdf as fp2,
    )

    assert NormalizedDocument is ND2
    assert Section is Section2
    assert Figure is Figure2
    assert Equation is Equation2
    assert Citation is Citation2
    assert DocumentMetadata is DM2
    assert IngestionPath is IP2
    assert from_arxiv_html is fa2
    assert from_pdf is fp2


# ---------------------------------------------------------------------------
# 2. test_figure_bytes_roundtrip — AC-2
# ---------------------------------------------------------------------------


def test_figure_bytes_roundtrip() -> None:
    """AC-2: Figure.image_bytes bytes, 1x1 PNG roundtrips byte-for-byte through JSON mode."""
    from paper2html.models.document import Figure

    fig = Figure(id="fig-1", image_bytes=MINIMAL_1x1_PNG)
    dumped = fig.model_dump(mode="json")
    # In JSON mode bytes should be base64 string
    assert isinstance(dumped["image_bytes"], str), "Expected base64 string in JSON mode"
    restored = Figure.model_validate(dumped)
    assert restored.image_bytes == MINIMAL_1x1_PNG, "Roundtrip did not preserve bytes"


# ---------------------------------------------------------------------------
# 3. test_section_hierarchy_from_arxiv — AC-3
# ---------------------------------------------------------------------------


def test_section_hierarchy_from_arxiv() -> None:
    """AC-3: nested sections produce correct ids, parent_id, subsection_ids, text exclusion."""
    from paper2html.models.document import from_arxiv_html

    html = """\
    <html><body>
    <section>
      <h2>Intro</h2>
      <p>Parent text.</p>
      <section>
        <h3>Sub1</h3>
        <p>t1.</p>
      </section>
      <section>
        <h3>Sub2</h3>
        <p>t2.</p>
      </section>
    </section>
    </body></html>
    """
    doc = from_arxiv_html(_soup(html), _make_metadata())
    sections = doc.sections

    assert [s.id for s in sections] == ["sec-1", "sec-1-1", "sec-1-2"]

    parent = sections[0]
    assert parent.subsection_ids == ["sec-1-1", "sec-1-2"]

    child1 = sections[1]
    child2 = sections[2]
    assert child1.parent_id == "sec-1"
    assert child2.parent_id == "sec-1"

    # Parent text_content must contain "Parent text" but NOT child text
    assert "Parent text" in parent.text_content
    assert "t1" not in parent.text_content, "Child text leaked into parent text_content"
    assert "t2" not in parent.text_content, "Child text leaked into parent text_content"


def test_inline_math_becomes_katex_delimited_no_latex_leak() -> None:
    """Inline <math> must render as \\(alttext\\); the <annotation> LaTeX source
    must NOT leak into prose (the 'hth_{t}' duplication bug).
    """
    from paper2html.models.document import from_arxiv_html

    html = (
        "<html><body><section><h2>S</h2>"
        '<p>Let <math alttext="h_{t}"><mi>h</mi><mi>t</mi>'
        '<annotation encoding="application/x-tex">h_{t}</annotation></math> '
        "denote the state.</p>"
        "</section></body></html>"
    )
    doc = from_arxiv_html(_soup(html), _make_metadata())
    text = doc.sections[0].text_content

    assert "\\(h_{t}\\)" in text, f"inline math not KaTeX-delimited: {text!r}"
    # The raw annotation must not appear as bare prose (only inside the \(...\)).
    assert "ht" not in text.replace("\\(h_{t}\\)", ""), (
        f"MathML glyphs leaked: {text!r}"
    )


def test_display_math_excluded_from_prose() -> None:
    """Block/display <math> renders as a standalone equation, not inline prose."""
    from paper2html.models.document import from_arxiv_html

    html = (
        "<html><body><section><h2>S</h2>"
        '<p>The rate is <math display="block" alttext="x=1">'
        '<annotation encoding="application/x-tex">x=1</annotation></math></p>'
        "</section></body></html>"
    )
    doc = from_arxiv_html(_soup(html), _make_metadata())
    assert "x=1" not in doc.sections[0].text_content


def test_heading_and_figure_caption_excluded_from_prose() -> None:
    """Section heading text and nested figure captions must not bleed into body."""
    from paper2html.models.document import from_arxiv_html

    html = (
        "<html><body><section><h2>3.1 Encoder</h2>"
        "<p>Body sentence.</p>"
        "<figure><img src='x.png'/><figcaption>Figure 5: caption text</figcaption></figure>"
        "</section></body></html>"
    )
    doc = from_arxiv_html(_soup(html), _make_metadata())
    text = doc.sections[0].text_content
    assert "Body sentence." in text
    assert "Encoder" not in text, "heading leaked into body"
    assert "caption text" not in text, "figure caption bled into body"


def test_title_number_spacing_normalized() -> None:
    """'5.4Regularization' (adjacent number/title spans) -> '5.4 Regularization'."""
    from paper2html.models.document import from_arxiv_html

    html = "<html><body><section><h2>5.4Regularization</h2><p>x.</p></section></body></html>"
    doc = from_arxiv_html(_soup(html), _make_metadata())
    assert doc.sections[0].title == "5.4 Regularization"


def test_data_table_rendered_as_structured_html() -> None:
    """An ltx_tabular table becomes clean <table> HTML on its section, with inline
    math converted to KaTeX delimiters — not flattened into prose soup.
    """
    from paper2html.models.document import from_arxiv_html

    html = (
        "<html><body><section><h2>Methods</h2>"
        "<p>See the table.</p>"
        '<figure class="ltx_table"><table class="ltx_tabular">'
        "<tr><th>Layer</th><th>Complexity</th></tr>"
        '<tr><td>Self-Attention</td><td><math alttext="O(n^{2})">'
        '<annotation encoding="application/x-tex">O(n^{2})</annotation></math></td></tr>'
        "</table><figcaption>Table 1: complexity</figcaption></figure>"
        "</section></body></html>"
    )
    doc = from_arxiv_html(_soup(html), _make_metadata())
    sec = doc.sections[0]

    assert len(sec.tables) == 1
    tbl = sec.tables[0]
    assert "<table>" in tbl and "<th>Layer</th>" in tbl
    assert "Self-Attention" in tbl
    assert "\\(O(n^{2})\\)" in tbl, "cell math not KaTeX-delimited"
    assert "Table 1: complexity" in tbl
    # The table content must NOT have leaked into the prose body.
    assert "Self-Attention" not in sec.text_content
    assert "O(n" not in sec.text_content


def test_table_figure_not_emitted_as_empty_figure() -> None:
    """An ltx_table figure wrapper must not also produce an image-less Figure."""
    from paper2html.models.document import from_arxiv_html

    html = (
        "<html><body><section><h2>M</h2>"
        '<figure class="ltx_table"><table class="ltx_tabular">'
        "<tr><td>a</td><td>b</td></tr></table>"
        "<figcaption>Table 1</figcaption></figure>"
        "</section></body></html>"
    )
    doc = from_arxiv_html(_soup(html), _make_metadata())
    assert len(doc.figures) == 0, "table wrapper leaked as a figure"
    assert len(doc.sections[0].tables) == 1


def test_section_extraction_through_div_wrappers() -> None:
    """Regression: real arXiv (LaTeXML) wraps sections in
    <div class="ltx_page_main"> → <div class="ltx_page_content"> → <article>,
    so sections are NOT direct children of <body>. A recursive=False scan there
    found zero sections, yielding an empty document (0% progress bar, only
    abstract/references rendered). Top-level sections must be found regardless
    of intervening div/article wrappers.
    """
    from paper2html.models.document import from_arxiv_html

    html = """\
    <html><body>
    <header>nav junk</header>
    <div class="ltx_page_main"><div class="ltx_page_content"><article>
      <section>
        <h2>Introduction</h2>
        <p>Intro body.</p>
        <section>
          <h3>Background</h3>
          <p>Sub body.</p>
        </section>
      </section>
      <section>
        <h2>Method</h2>
        <p>Method body.</p>
      </section>
    </article></div></div>
    </body></html>
    """
    doc = from_arxiv_html(_soup(html), _make_metadata())

    assert [s.id for s in doc.sections] == ["sec-1", "sec-1-1", "sec-2"]
    assert doc.sections[0].title == "Introduction"
    assert doc.sections[2].title == "Method"
    assert "Intro body" in doc.sections[0].text_content
    # Sub text must not leak into its parent
    assert "Sub body" not in doc.sections[0].text_content
    assert doc.sections[1].parent_id == "sec-1"


# ---------------------------------------------------------------------------
# 4. test_arxiv_figure_extraction — AC-4
# ---------------------------------------------------------------------------


def test_arxiv_figure_extraction() -> None:
    """AC-4: figure + figcaption + img → caption matches, bytes == MINIMAL_1x1_PNG."""
    from paper2html.models.document import from_arxiv_html

    html = """\
    <html><body>
    <section>
      <h2>Results</h2>
      <p>Some text.</p>
      <figure>
        <img src="test.png"/>
        <figcaption>My figure caption</figcaption>
      </figure>
    </section>
    </body></html>
    """
    doc = from_arxiv_html(
        _soup(html),
        _make_metadata(),
        figure_assets={"test.png": MINIMAL_1x1_PNG},
    )
    assert len(doc.figures) == 1
    fig = list(doc.figures.values())[0]
    assert fig.caption == "My figure caption"
    assert fig.image_bytes == MINIMAL_1x1_PNG


# ---------------------------------------------------------------------------
# 5. test_arxiv_equation_extraction — AC-5
# ---------------------------------------------------------------------------


def test_arxiv_equation_extraction() -> None:
    """AC-5: inline + block MathML → 2 equations, display types, non-empty mathml."""
    from paper2html.models.document import from_arxiv_html

    html = """\
    <html><body>
    <section>
      <h2>Math</h2>
      <p>Inline: <math alttext="\\alpha"><mi>α</mi></math> here.</p>
      <p>Block: <math display="block"><mi>β</mi></math></p>
    </section>
    </body></html>
    """
    doc = from_arxiv_html(_soup(html), _make_metadata())
    assert len(doc.equations) == 2

    eqs = list(doc.equations.values())
    inline_eq = next(e for e in eqs if e.display_type == "inline")
    block_eq = next(e for e in eqs if e.display_type == "block")

    assert inline_eq.raw_latex == "\\alpha"
    assert inline_eq.mathml and len(inline_eq.mathml) > 0

    assert block_eq.display_type == "block"
    assert block_eq.mathml and len(block_eq.mathml) > 0


# ---------------------------------------------------------------------------
# 6. test_pdf_adapter_basic — AC-6 + scenario 2
# ---------------------------------------------------------------------------


def test_pdf_adapter_basic(tmp_path: Path) -> None:
    """AC-6: in-memory pymupdf doc → ingestion_path PDF, ≥1 section non-empty text, ≥1 figure."""
    import pymupdf
    from paper2html.models.document import from_pdf, IngestionPath

    doc = pymupdf.open()
    page = doc.new_page(-1, width=595, height=842)
    page.insert_text((50, 100), "Hello page one")
    page.insert_image(pymupdf.Rect(50, 200, 150, 300), stream=MINIMAL_16x16_PNG)

    # Save and reopen to ensure images are fully embedded
    pdf_bytes = doc.tobytes()
    doc.close()
    doc2 = pymupdf.open(stream=pdf_bytes, filetype="pdf")

    result = from_pdf(doc2, Path("test.pdf"))
    doc2.close()

    assert result.metadata.ingestion_path == IngestionPath.PDF
    assert result.metadata.arxiv_id is None

    non_empty_sections = [s for s in result.sections if s.text_content.strip()]
    assert len(non_empty_sections) >= 1, (
        "Expected at least 1 section with non-empty text"
    )

    non_empty_figures = [f for f in result.figures.values() if f.image_bytes]
    assert len(non_empty_figures) >= 1, (
        "Expected at least 1 figure with non-empty image_bytes"
    )

    fig = non_empty_figures[0]
    assert fig.page_number is not None
    assert fig.image_mime_type in {"image/png", "image/jpeg"}


# ---------------------------------------------------------------------------
# 7. test_no_provider_imports — AC-7
# ---------------------------------------------------------------------------


def test_no_provider_imports() -> None:
    """AC-7: importing and running adapters never touches anthropic/openai/httpx/requests/aiohttp."""
    import subprocess

    code = r"""
import sys
import types

BLOCKED = frozenset(["anthropic", "openai", "httpx", "requests", "aiohttp"])

class _BlockProviders:
    def find_module(self, fullname, path=None):
        if any(fullname == b or fullname.startswith(b + ".") for b in BLOCKED):
            return self
    def load_module(self, fullname):
        raise ImportError(f"provider blocked: {fullname}")

sys.meta_path.insert(0, _BlockProviders())

from bs4 import BeautifulSoup
from types import SimpleNamespace
from pathlib import Path

from paper2html.models.document import from_arxiv_html, from_pdf

# Run from_arxiv_html
html = "<html><body><section><h2>S</h2><p>Text.</p></section></body></html>"
soup = BeautifulSoup(html, "html.parser")
meta = SimpleNamespace(
    title="T",
    authors=[SimpleNamespace(name="A")],
    summary="abs",
    entry_id="http://arxiv.org/abs/2301.00001v1",
)
doc1 = from_arxiv_html(soup, meta)

# Run from_pdf
import pymupdf
pdf_doc = pymupdf.open()
p = pdf_doc.new_page(-1, width=595, height=842)
p.insert_text((50, 100), "Hello")
doc2 = from_pdf(pdf_doc, Path("x.pdf"))
pdf_doc.close()

for name in BLOCKED:
    assert name not in sys.modules, f"{name} was imported!"

print("OK")
"""
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        f"Subprocess failed (rc={proc.returncode}):\n"
        f"stdout: {proc.stdout}\n"
        f"stderr: {proc.stderr}"
    )
    assert "OK" in proc.stdout


# ---------------------------------------------------------------------------
# 8. test_serialization_roundtrip_full — AC-8
# ---------------------------------------------------------------------------


def test_serialization_roundtrip_full() -> None:
    """AC-8: full roundtrip incl. json.loads of the dumped string."""
    from paper2html.models.document import (
        Citation,
        DocumentMetadata,
        Equation,
        Figure,
        IngestionPath,
        NormalizedDocument,
        Section,
    )

    sec = Section(
        id="sec-1",
        title="Intro",
        level=2,
        text_content="Some content.",
        parent_id=None,
        subsection_ids=["sec-1-1"],
        equation_ids=["eq-1"],
        figure_ids=["fig-1"],
    )
    child_sec = Section(
        id="sec-1-1",
        title="Sub",
        level=3,
        text_content="Sub content.",
        parent_id="sec-1",
    )
    fig = Figure(
        id="fig-1",
        caption="A figure",
        image_bytes=MINIMAL_1x1_PNG,
        image_mime_type="image/png",
        referencing_sentence_ids=["sec-1-sent-1"],
        label="fig:label",
        page_number=0,
        alt_text="alt",
        gloss="gloss",
    )
    eq = Equation(
        id="eq-1",
        raw_latex=r"\alpha",
        mathml="<math><mi>α</mi></math>",
        display_type="block",
        source_text="Equation source.",
    )
    cit = Citation(
        id="ref-1",
        raw_text="Author. Title. 2023.",
        arxiv_id="2301.00001",
        doi="10.1000/xyz123",
        title="Cited Title",
        authors=["Auth One"],
    )
    meta = DocumentMetadata(
        arxiv_id="2301.00001",
        title="Full Paper",
        authors=["A B", "C D"],
        abstract="Abstract.",
        source_url="http://arxiv.org/abs/2301.00001v1",
        ingestion_path=IngestionPath.ARXIV_HTML,
    )
    original = NormalizedDocument(
        metadata=meta,
        sections=[sec, child_sec],
        figures={"fig-1": fig},
        equations={"eq-1": eq},
        citations={"ref-1": cit},
        ingestion_warnings=["sec-1: some warning"],
    )

    dumped_str = json.dumps(original.model_dump(mode="json"))
    restored = NormalizedDocument.model_validate(json.loads(dumped_str))

    assert restored == original
    assert restored.figures["fig-1"].image_bytes == MINIMAL_1x1_PNG


# ---------------------------------------------------------------------------
# 9. test_ingestion_warnings_on_missing_elements — AC-9 + scenario 3
# ---------------------------------------------------------------------------


def test_ingestion_warnings_on_missing_elements() -> None:
    """AC-9: figure w/o bytes → present in dict with b"" + warning; empty section → warning."""
    from paper2html.models.document import from_arxiv_html

    # (a) figure with no img/svg → bytes == b"" and warning present
    html_no_img = """\
    <html><body>
    <section>
      <h2>Sec</h2>
      <p>Some text here.</p>
      <figure id="fig-no-img">
        <figcaption>No image at all</figcaption>
      </figure>
    </section>
    </body></html>
    """
    doc = from_arxiv_html(_soup(html_no_img), _make_metadata())

    assert len(doc.figures) == 1
    fig = list(doc.figures.values())[0]
    assert fig.image_bytes == b"", "Expected empty bytes for figure with no img"

    fig_id = list(doc.figures.keys())[0]
    assert any(fig_id in w for w in doc.ingestion_warnings), (
        f"Expected warning containing {fig_id!r}; got: {doc.ingestion_warnings}"
    )

    # (b) section with whitespace-only text → warning
    # A section with no text content at all (no heading, no body text — just whitespace)
    html_empty_sec = """\
    <html><body>
    <section>
    </section>
    </body></html>
    """
    doc2 = from_arxiv_html(_soup(html_empty_sec), _make_metadata())
    assert any("empty text_content" in w for w in doc2.ingestion_warnings), (
        f"Expected 'empty text_content' warning; got: {doc2.ingestion_warnings}"
    )

    # (c) valid math element → mathml is always populated from soup, no spurious warning
    html_valid_math = """\
    <html><body>
    <section>
      <h2>Math</h2>
      <p><math alttext="\\alpha"><mi>α</mi></math></p>
    </section>
    </body></html>
    """
    doc3 = from_arxiv_html(_soup(html_valid_math), _make_metadata())
    eq_warnings = [
        w for w in doc3.ingestion_warnings if "neither raw_latex nor mathml" in w
    ]
    assert eq_warnings == [], f"Spurious equation warning on valid math: {eq_warnings}"


# ---------------------------------------------------------------------------
# 10. test_referencing_sentences_from_arxiv — AC-10
# ---------------------------------------------------------------------------


def test_referencing_sentences_from_arxiv() -> None:
    """AC-10: <a href="#fig-label-1"> in section text → figure.referencing_sentence_ids populated."""
    from paper2html.models.document import from_arxiv_html

    html = """\
    <html><body>
    <section>
      <h2>Discussion</h2>
      <p>See <a href="#fig-label-1">Figure 1</a> here. Another sentence.</p>
    </section>
    <figure id="fig-label-1">
      <img src="fig1.png"/>
      <figcaption>Caption for figure 1</figcaption>
    </figure>
    </body></html>
    """
    doc = from_arxiv_html(
        _soup(html),
        _make_metadata(),
        figure_assets={"fig1.png": MINIMAL_1x1_PNG},
    )

    # Find the figure that has the matching label
    fig = next(
        (f for f in doc.figures.values() if f.label == "fig-label-1"),
        None,
    )
    assert fig is not None, "Figure with label 'fig-label-1' not found"
    assert len(fig.referencing_sentence_ids) > 0, (
        "Expected referencing_sentence_ids to be non-empty"
    )
    import re

    for sent_id in fig.referencing_sentence_ids:
        assert re.match(r"sec-\d+(-\d+)*-sent-\d+", sent_id), (
            f"Sentence id {sent_id!r} does not match expected pattern"
        )


# ---------------------------------------------------------------------------
# 11. test_pdf_adapter_no_metadata — AC-11
# ---------------------------------------------------------------------------


def test_pdf_adapter_no_metadata() -> None:
    """from_pdf with metadata=None → arxiv_id None, ingestion_path PDF, no raise."""
    import pymupdf
    from paper2html.models.document import from_pdf, IngestionPath

    doc = pymupdf.open()
    doc.new_page(-1, width=595, height=842)

    result = from_pdf(doc, Path("x.pdf"), metadata=None)
    doc.close()

    assert result.metadata.arxiv_id is None
    assert result.metadata.ingestion_path == IngestionPath.PDF


# ---------------------------------------------------------------------------
# 12. test_deterministic_ids
# ---------------------------------------------------------------------------


def test_deterministic_ids() -> None:
    """Running from_arxiv_html twice on same fixture → identical id lists."""
    from paper2html.models.document import from_arxiv_html

    html = """\
    <html><body>
    <section>
      <h2>Intro</h2>
      <p>Text one.</p>
      <figure><img src="a.png"/><figcaption>A</figcaption></figure>
      <math alttext="x"><mi>x</mi></math>
    </section>
    <section>
      <h2>Methods</h2>
      <p>Text two.</p>
      <figure><img src="b.png"/><figcaption>B</figcaption></figure>
      <math alttext="y"><mi>y</mi></math>
    </section>
    </body></html>
    """
    assets = {"a.png": MINIMAL_1x1_PNG, "b.png": MINIMAL_1x1_PNG}
    meta = _make_metadata()

    doc1 = from_arxiv_html(_soup(html), meta, figure_assets=assets)
    doc2 = from_arxiv_html(_soup(html), meta, figure_assets=assets)

    assert [s.id for s in doc1.sections] == [s.id for s in doc2.sections]
    assert list(doc1.figures.keys()) == list(doc2.figures.keys())
    assert list(doc1.equations.keys()) == list(doc2.equations.keys())

    # Ids should be 1-indexed contiguous
    assert list(doc1.figures.keys()) == ["fig-1", "fig-2"]
    assert list(doc1.equations.keys()) == ["eq-1", "eq-2"]
    assert [s.id for s in doc1.sections] == ["sec-1", "sec-2"]


# ---------------------------------------------------------------------------
# 13. test_old_style_arxiv_id — MUST-FIX
# ---------------------------------------------------------------------------


def test_old_style_arxiv_id() -> None:
    """Old-style entry_id 'http://arxiv.org/abs/math/0211159v1' → arxiv_id == 'math/0211159'."""
    from paper2html.models.document import from_arxiv_html

    meta = _make_metadata(entry_id="http://arxiv.org/abs/math/0211159v1")
    html = "<html><body><section><h2>S</h2><p>T.</p></section></body></html>"
    doc = from_arxiv_html(_soup(html), meta)

    assert doc.metadata.arxiv_id == "math/0211159", (
        f"Expected 'math/0211159', got {doc.metadata.arxiv_id!r}"
    )


# ---------------------------------------------------------------------------
# 14. test_svg_figure_rasterized_via_cairosvg — MUST-FIX
# ---------------------------------------------------------------------------


def test_svg_figure_rasterized_via_cairosvg(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inline <svg> figure → cairosvg.svg2png called → non-empty bytes returned."""
    from paper2html.models.document import from_arxiv_html

    monkeypatch.setattr(
        "paper2html.ingestion._svg._convert", lambda *args: MINIMAL_1x1_PNG
    )

    html = """\
    <html><body>
    <section>
      <h2>SVG Section</h2>
      <p>Some text.</p>
      <figure>
        <svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
          <circle cx="50" cy="50" r="40"/>
        </svg>
        <figcaption>SVG figure</figcaption>
      </figure>
    </section>
    </body></html>
    """
    doc = from_arxiv_html(_soup(html), _make_metadata())

    assert len(doc.figures) == 1
    fig = list(doc.figures.values())[0]
    assert fig.image_bytes == MINIMAL_1x1_PNG, (
        "Expected cairosvg mock bytes to be returned"
    )


# ---------------------------------------------------------------------------
# 15. test_svg_figure_cairosvg_failure — MUST-FIX
# ---------------------------------------------------------------------------


def test_svg_figure_cairosvg_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """cairosvg.svg2png raises OSError → figure present with b'' + warning."""
    from paper2html.models.document import from_arxiv_html

    def fail(*args):
        raise OSError("cairo not found")

    monkeypatch.setattr("paper2html.ingestion._svg._convert", fail)

    html = """\
    <html><body>
    <section>
      <h2>SVG Section</h2>
      <p>Some text.</p>
      <figure>
        <svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
          <rect width="100" height="100"/>
        </svg>
        <figcaption>Broken SVG</figcaption>
      </figure>
    </section>
    </body></html>
    """
    doc = from_arxiv_html(_soup(html), _make_metadata())

    assert len(doc.figures) == 1
    fig = list(doc.figures.values())[0]
    assert fig.image_bytes == b"", "Expected empty bytes on cairosvg failure"

    fig_id = list(doc.figures.keys())[0]
    assert any("cairosvg" in w or fig_id in w for w in doc.ingestion_warnings), (
        f"Expected warning about cairosvg failure; got: {doc.ingestion_warnings}"
    )


# ---------------------------------------------------------------------------
# 16. test_data_uri_figure
# ---------------------------------------------------------------------------


def test_data_uri_figure() -> None:
    """Figure with data: URI img src → bytes decoded from base64."""
    import base64
    from paper2html.models.document import from_arxiv_html

    b64 = base64.b64encode(MINIMAL_1x1_PNG).decode()
    html = f"""\
    <html><body>
    <section>
      <h2>Data URI</h2>
      <p>Some text.</p>
      <figure>
        <img src="data:image/png;base64,{b64}"/>
        <figcaption>Data URI figure</figcaption>
      </figure>
    </section>
    </body></html>
    """
    doc = from_arxiv_html(_soup(html), _make_metadata())
    assert len(doc.figures) == 1
    fig = list(doc.figures.values())[0]
    assert fig.image_bytes == MINIMAL_1x1_PNG


# ---------------------------------------------------------------------------
# 17. test_citations_parsing
# ---------------------------------------------------------------------------


def test_citations_parsing() -> None:
    """ltx_biblist present → citations dict populated."""
    from paper2html.models.document import from_arxiv_html

    html = """\
    <html><body>
    <section>
      <h2>References</h2>
      <p>Text.</p>
    </section>
    <ul class="ltx_biblist">
      <li class="ltx_bibitem">Smith et al. 2020. Some Paper. ICML.</li>
      <li class="ltx_bibitem">Jones 2021. Another Paper. NeurIPS.</li>
    </ul>
    </body></html>
    """
    doc = from_arxiv_html(_soup(html), _make_metadata())
    assert len(doc.citations) == 2
    cit = list(doc.citations.values())[0]
    assert "Smith" in cit.raw_text


def test_citation_leading_bib_label_stripped() -> None:
    """The leading '[N]' bib label is removed so the <ol> doesn't render '32. [32]'."""
    from paper2html.models.document import from_arxiv_html

    html = (
        "<html><body><ul class='ltx_biblist'>"
        "<li class='ltx_bibitem'>[32] Noam Shazeer et al. Outrageously large networks. 2017.</li>"
        "</ul></body></html>"
    )
    doc = from_arxiv_html(_soup(html), _make_metadata())
    raw = list(doc.citations.values())[0].raw_text
    assert raw.startswith("Noam Shazeer"), f"bib label not stripped: {raw!r}"
    assert "[32]" not in raw


# ---------------------------------------------------------------------------
# 18. test_pdf_adapter_with_metadata
# ---------------------------------------------------------------------------


def test_pdf_adapter_with_metadata() -> None:
    """from_pdf with non-None metadata → metadata fields populated."""
    import pymupdf
    from paper2html.models.document import from_pdf, IngestionPath

    meta = _make_metadata()
    doc = pymupdf.open()
    doc.new_page(-1, width=595, height=842)

    result = from_pdf(doc, Path("paper.pdf"), metadata=meta)
    doc.close()

    assert result.metadata.ingestion_path == IngestionPath.PDF
    assert result.metadata.title == "Test Paper"
    assert result.metadata.authors == ["A B", "C D"]
    assert result.metadata.arxiv_id == "2301.00001"


# ---------------------------------------------------------------------------
# 19. test_pdf_figure_extraction_failure
# ---------------------------------------------------------------------------


def test_pdf_figure_extraction_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """PDF extract_image raises → figure recorded with b'' + warning."""
    import pymupdf
    from paper2html.models.document import from_pdf

    doc = pymupdf.open()
    page = doc.new_page(-1, width=595, height=842)
    page.insert_text((50, 100), "Hello")
    page.insert_image(pymupdf.Rect(50, 200, 150, 300), stream=MINIMAL_16x16_PNG)

    pdf_bytes = doc.tobytes()
    doc.close()
    doc2 = pymupdf.open(stream=pdf_bytes, filetype="pdf")

    def failing_extract(self: pymupdf.Document, xref: int) -> dict:
        raise RuntimeError("Simulated extraction failure")

    monkeypatch.setattr(pymupdf.Document, "extract_image", failing_extract)

    result = from_pdf(doc2, Path("test.pdf"))
    doc2.close()

    # Figure should still be recorded with empty bytes
    assert len(result.figures) >= 1
    failed_figs = [f for f in result.figures.values() if f.image_bytes == b""]
    assert len(failed_figs) >= 1
    assert any("failed to extract" in w for w in result.ingestion_warnings)


# ---------------------------------------------------------------------------
# 20. test_arxiv_id_no_match_returns_none
# ---------------------------------------------------------------------------


def test_arxiv_id_no_match_returns_none() -> None:
    """_short_arxiv_id returns None when entry_id has no recognizable arxiv id."""
    from paper2html.models.document import _short_arxiv_id

    assert _short_arxiv_id("") is None
    assert _short_arxiv_id("http://example.com/foo") is None


# ---------------------------------------------------------------------------
# 21. test_mime_jpeg
# ---------------------------------------------------------------------------


def test_mime_jpeg() -> None:
    """_mime returns 'image/jpeg' for jpg/jpeg extensions."""
    from paper2html.models.document import _mime

    assert _mime("jpg") == "image/jpeg"
    assert _mime("jpeg") == "image/jpeg"
    assert _mime("JPEG") == "image/jpeg"
    assert _mime("png") == "image/png"


# ---------------------------------------------------------------------------
# 22. test_ac10_anchor_not_matching_figure
# ---------------------------------------------------------------------------


def test_ac10_anchor_not_matching_figure() -> None:
    """Anchor href pointing to non-existent figure id → no error, no referencing ids added."""
    from paper2html.models.document import from_arxiv_html

    html = """\
    <html><body>
    <section>
      <h2>Discussion</h2>
      <p>See <a href="#fig-nonexistent">Figure X</a> here.</p>
    </section>
    <figure id="fig-actual">
      <img src="real.png"/>
      <figcaption>Actual figure</figcaption>
    </figure>
    </body></html>
    """
    doc = from_arxiv_html(
        _soup(html),
        _make_metadata(),
        figure_assets={"real.png": MINIMAL_1x1_PNG},
    )
    # No error; the actual figure should have no referencing sentence ids
    fig = next(f for f in doc.figures.values() if f.label == "fig-actual")
    assert fig.referencing_sentence_ids == []
