"""Synthetic source-fidelity contracts: no copyrighted paper fixtures or live calls."""

import base64
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pymupdf
import pytest
from bs4 import BeautifulSoup
from PIL import Image

from paper2html.ingestion._client import RateLimitedClient, _reset_singleton
from paper2html.ingestion._figure_fetcher import build_figures
from paper2html.ingestion._html_parser import parse_sections
from paper2html.models.document import NormalizedDocument, from_arxiv_html
from paper2html.renderer import check_offline_integrity, render_html
from paper2html.review.schemas import AnnotationBundle
from paper2html.source_view import sanitize_article
from tests.test_cli_process import _invoke

SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="20" height="12"><path d="M0 0L20 12" stroke="black"/></svg>'
RICH = """<article class="ltx_document" id="paper"><h1>Original title</h1>
<div class="ltx_authors">Author <address>University <a href="mailto:a@example.org">a@example.org</a></address></div>
<div class="ltx_keywords">Keywords: fidelity, source</div>
<section id="intro"><h2>Introduction</h2><p class="ltx_p">Before <math id="inline-math"><semantics><mi>x</mi><annotation encoding="application/x-tex">DUPLICATE</annotation></semantics></math> after.</p>
<figure id="fig"><img src="one.png"><object type="image/svg+xml" data="plot.svg"></object><figcaption>Caption <math><mi>y</mi></math> intact</figcaption></figure>
<p>Between figure and table.</p><figure><table class="ltx_tabular"><tr><th colspan="2">Heading</th></tr><tr><td>A</td><td><math><mn>2</mn></math></td></tr></table><figcaption>Table caption</figcaption></figure>
<div><math display="block"><mfrac><mi>a</mi><mi>b</mi></mfrac></math></div>
<p>Last <a href="#fig">figure backlink</a>.</p></section>
<section id="bib"><h2>References</h2><ul class="ltx_biblist"><li class="ltx_bibitem" id="ref">[1] Original reference.</li></ul></section></article>"""
MALICIOUS = """<script>window.PWNED=1</script><style>body{background:url(https://evil.invalid)}</style>
<iframe src="https://evil.invalid"></iframe><form action="https://evil.invalid"><input></form>
<p onclick="window.PWNED=1" style="background:url(file:///tmp/secret)">Safe</p>
<a href="javascript:window.PWNED=1">bad link</a><a href="data:text/html,bad">bad data</a>
<math><mtext><img src="https://evil.invalid/x" onerror="window.PWNED=1">Safe math</mtext><annotation-xml><script>window.PWNED=1</script></annotation-xml></math>"""


def png():
    stream = BytesIO()
    Image.new("RGB", (8, 6), "red").save(stream, format="PNG")
    return stream.getvalue()


def rich_doc():
    # Adapter/render tests consume the already-rasterized ingestion contract.
    # Native SVG conversion is exercised separately with an explicit prerequisite.
    return from_arxiv_html(
        BeautifulSoup(RICH, "html.parser"),
        SimpleNamespace(title="Original title"),
        {"one.png": png(), "plot.svg": png()},
    )


@pytest.fixture
def native_cairo():
    try:
        import cairosvg  # noqa: F401
    except (ImportError, OSError):
        pytest.skip(
            "Native Cairo unavailable; install it/configure its loader. CI requires Cairo."
        )


def test_object_svg_guarded_fetch_multiple_images_and_frontmatter(
    monkeypatch, native_cairo
):
    monkeypatch.setattr("paper2html.ingestion._client._RATE_LIMIT_SECONDS", 0)
    requested = []

    def handler(request):
        requested.append(str(request.url))
        return httpx.Response(
            200, content=SVG if request.url.path.endswith(".svg") else png()
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        _reset_singleton(RateLimitedClient(http_client=client))
        sections, orphans = parse_sections(RICH, "https://arxiv.org/html/2501.00001v2/")
        figures = build_figures([r for _, refs in sections for r in refs] + orphans)
    assert requested == [
        "https://arxiv.org/html/2501.00001v2/one.png",
        "https://arxiv.org/html/2501.00001v2/plot.svg",
    ]
    assert len(figures) == 2 and all(f.mime_type == "image/png" for f in figures)
    doc = from_arxiv_html(
        BeautifulSoup(RICH, "html.parser"),
        SimpleNamespace(),
        {f.raw_src: f.asset_bytes for f in figures},
    )
    assert len(doc.source_assets) == 2
    assert next(iter(doc.figures.values())).image_bytes


def test_inline_svg_raster_is_reused_across_ingestion_adapter_and_source(monkeypatch):
    from paper2html.source_view import asset_key

    source = (
        '<article class="ltx_document"><section id="s"><h2>Diagram</h2><p>Text.</p>'
        f'<figure id="f">{SVG.decode()}<figcaption>Original caption</figcaption>'
        "</figure></section></article>"
    )
    converted = []

    def convert(data):
        converted.append(data)
        return png()

    monkeypatch.setattr("paper2html.ingestion._svg.render_svg", convert)
    sections, orphans = parse_sections(source, "https://arxiv.org/html/2501.00001")
    figures = build_figures([r for _, refs in sections for r in refs] + orphans)
    soup = BeautifulSoup(source, "html.parser")
    key = asset_key(soup.svg)
    assert figures[0].raw_src == key
    doc = from_arxiv_html(
        soup, SimpleNamespace(), {f.raw_src: f.asset_bytes for f in figures}
    )
    assert len(converted) == 1
    assert doc.source_assets[key] == png()
    assert next(iter(doc.figures.values())).image_bytes == png()
    assert not doc.ingestion_warnings
    rendered = BeautifulSoup(
        str(sanitize_article(source, doc.source_assets)), "html.parser"
    )
    assert len(rendered.find_all("img")) == 1
    assert "Original caption" in rendered.text


def test_inline_svg_adapter_rasterizes_once_without_supplied_assets(monkeypatch):
    converted = []

    def convert(data):
        converted.append(data)
        return png()

    monkeypatch.setattr("paper2html.ingestion._svg.render_svg", convert)
    doc = from_arxiv_html(
        BeautifulSoup(
            f"<article><figure>{SVG.decode()}</figure></article>", "html.parser"
        ),
        SimpleNamespace(),
    )
    assert len(converted) == 1
    assert next(iter(doc.figures.values())).image_bytes == png()


def test_source_roundtrip_semantics_order_and_native_math(tmp_path):
    doc = NormalizedDocument.model_validate(rich_doc().model_dump(mode="json"))
    assert doc.source_assets == rich_doc().source_assets
    out = render_html(doc, AnnotationBundle(items=[]), tmp_path / "paper.html")
    soup = BeautifulSoup(out.read_text(), "html.parser")
    body = soup.select_one(".source-content")
    assert len(body.find_all("math")) == 4
    assert len(body.find_all("tr")) == 2
    scroll_region = body.find("table").parent
    assert scroll_region["class"] == ["source-table-scroll"]
    assert scroll_region["tabindex"] == "0" and scroll_region["role"] == "region"
    assert len(body.find_all(["td", "th"])) == 3
    assert len(body.select(".src-ltx_bibitem")) == 1
    assert len(body.find_all("img")) == 2
    text = body.get_text()
    parts = [
        "University",
        "Keywords:",
        "Before",
        "after.",
        "Caption",
        "Between",
        "Heading",
        "Table caption",
        "Last",
        "Original reference",
    ]
    assert [text.index(p) for p in parts] == sorted(text.index(p) for p in parts)
    assert "DUPLICATE" not in text
    assert body.find("math", id="source-inline-math")
    assert body.find("a", href="#source-fig") and body.find(id="source-fig")
    assert not body.find(["object", "svg", "annotation"])
    assert "renderMathInElement" not in out.read_text()
    for image in body.find_all("img"):
        with Image.open(
            BytesIO(base64.b64decode(image["src"].split(",")[1]))
        ) as opened:
            opened.verify()


def test_scroll_regions_only_wrap_outer_block_tables():
    source = "<div><table><tr><td><table><tr><td>Nested</td></tr></table></td></tr></table></div>"
    rendered = BeautifulSoup(str(sanitize_article(source, {})), "html.parser")
    assert len(rendered.select(".source-table-scroll")) == 1
    assert len(rendered.find_all("table")) == 2
    for parent in ("p", "span", "a", "strong"):
        markup = str(
            sanitize_article(
                f"<{parent}>a<table><tr><td>b</td></tr></table></{parent}>", {}
            )
        )
        assert '<div class="source-table-scroll"' not in markup
        assert '<span class="source-table-scroll"' in markup
        assert "<table>" in markup


def test_source_payloads_are_inert_and_missing_images_explicit():
    with pytest.warns(UserWarning, match="omitted"):
        markup = sanitize_article(RICH + MALICIOUS, {})
    soup = BeautifulSoup(str(markup), "html.parser")
    assert not soup.find(
        ["script", "style", "object", "iframe", "form", "input", "annotation-xml"]
    )
    assert not soup.find("img")
    assert soup.select(".source-image-missing")
    for tag in soup.find_all(True):
        assert not any(
            a.startswith("on") or a in {"style", "src", "data"} for a in tag.attrs
        )
    assert not soup.find(
        "a", href=lambda v: v and v.startswith(("javascript:", "data:"))
    )
    assert "Safe math" in soup.text


def test_source_only_pdf_no_provider_no_annotations_and_page_backlinks(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    pdf = tmp_path / "two.pdf"
    with pymupdf.open() as doc:
        for n in range(2):
            page = doc.new_page(width=160, height=160)
            page.insert_text((20, 20), f"Original page {n + 1}")
            page.draw_line((20, 40), (140, 140), color=(1, 0, 0))
        doc.save(pdf)
    out = tmp_path / "two.html"
    with (
        patch(
            "paper2html.cli.build_provider",
            side_effect=AssertionError("provider forbidden"),
        ),
        patch(
            "paper2html.cli.run_pipeline",
            side_effect=AssertionError("pipeline forbidden"),
        ),
    ):
        result = _invoke(
            tmp_path / "cache", "process", str(pdf), "--source-only", "-o", str(out)
        )
    assert result.exit_code == 0, result.output
    soup = BeautifulSoup(out.read_text(), "html.parser")
    assert len(soup.select(".source-facsimile img")) == 2
    assert "facsimile" in soup.text and "not a semantic reconstruction" in soup.text
    assert len(soup.find_all("a", href="#source-pages")) == 2
    assert not soup.select(".summary-block, .term-def, .margin-note")
    assert "Original page 2" in soup.text
    import sqlite3

    with sqlite3.connect(tmp_path / "cache" / "library.db") as db:
        assert db.execute("select cost_spent from papers").fetchone()[0] == 0
    collision = _invoke(
        tmp_path / "cache", "process", str(pdf), "--source-only", "-o", str(out)
    )
    assert collision.exit_code == 1


def test_source_article_budgets_and_math_depth():
    with pytest.raises(ValueError, match="depth"):
        sanitize_article("<div>" * 140 + "x" + "</div>" * 140, {})
    with pytest.warns(UserWarning, match="depth"):
        result = sanitize_article(
            "<p>Kept</p><math>" + "<mrow>" * 140 + "x" + "</mrow>" * 140 + "</math>", {}
        )
    assert "Kept" in result and "math" not in result


def test_source_only_html_no_provider_and_complete_frontmatter(tmp_path, monkeypatch):
    from paper2html.ingestion._types import ArxivIngestResult, ArxivMetadata, Figure

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    ingest = ArxivIngestResult(
        ArxivMetadata("2501.00001", "Original title"),
        raw_html=RICH,
        orphan_figures=[
            Figure("", png(), raw_src="one.png"),
            Figure("", png(), raw_src="plot.svg"),
        ],
    )
    out = tmp_path / "source.html"
    with (
        patch("paper2html.cli.ingest_arxiv", return_value=ingest),
        patch(
            "paper2html.cli.build_provider",
            side_effect=AssertionError("provider forbidden"),
        ),
        patch(
            "paper2html.cli.run_pipeline",
            side_effect=AssertionError("pipeline forbidden"),
        ),
    ):
        result = _invoke(
            tmp_path / "cache", "process", "2501.00001", "--source-only", "-o", str(out)
        )
    assert result.exit_code == 0, result.output
    soup = BeautifulSoup(out.read_text(), "html.parser")
    assert len(soup.find_all("math")) == 4 and len(soup.find_all("img")) == 2
    assert "University" in soup.text and "Keywords:" in soup.text
    assert not soup.select(".source-reading-aids")


def test_offline_integrity_allows_source_links_but_not_remote_resources(tmp_path):
    output = tmp_path / "links.html"
    output.write_text('<a href="https://example.org/dataset">Original dataset</a>')
    assert check_offline_integrity(output) == []
    output.write_text(
        '<a href="https://example.org/dataset">Original dataset</a>'
        '<img src="https://example.org/plot.png">'
        '<link rel="stylesheet" href="https://example.org/style.css">'
    )
    violations = check_offline_integrity(output)
    assert any("<img>" in item for item in violations)
    assert any("<link>" in item for item in violations)


def test_heading_only_parents_are_not_reported_as_missing_content():
    source = "<article><section><h2>Group</h2><section><h3>Child</h3><p>Kept text.</p></section></section><section><h2>Empty leaf</h2></section></article>"
    doc = from_arxiv_html(BeautifulSoup(source, "html.parser"), SimpleNamespace())
    assert doc.ingestion_warnings == ["sec-2: empty text_content"]
    assert doc.sections[1].text_content == "Kept text."


def test_pdf_page_assets_require_complete_sequence(tmp_path):
    from paper2html.models.document import from_pdf

    with pymupdf.open() as pdf:
        pdf.new_page()
        with pytest.raises(ValueError, match="every source page"):
            from_pdf(pdf, tmp_path / "source.pdf", page_assets=[])


def test_source_asset_aggregate_budget(monkeypatch):
    from dataclasses import replace
    from paper2html.ingestion import _limits

    monkeypatch.setattr(_limits, "LIMITS", replace(_limits.LIMITS, source_bytes=5))
    with pytest.raises(ValueError, match="asset budget"):
        sanitize_article("<p>Safe</p>", {"x": b"012345"})


@pytest.mark.parametrize("fallback", [False, True])
def test_annotated_pdf_default_facsimile_header_and_separate_notes(tmp_path, fallback):
    from paper2html.ingestion._types import ArxivMetadata, PdfFallbackResult
    from paper2html.models.document import Figure
    from paper2html.pipeline.schemas import PipelineResult
    from tests.test_cli_process import _make_fixture_pdf

    pdf = _make_fixture_pdf(tmp_path)
    out = tmp_path / "annotated.html"
    result = PipelineResult(
        doc_id="test", provider="mock", orchestrator_model="mock", worker_model="mock"
    )
    bundle = AnnotationBundle(items=[], margin_notes=["Optional margin note"])

    def annotate(doc, *args, **kwargs):
        doc.figures["fig-note"] = Figure(
            id="fig-note", gloss="Optional figure explanation"
        )
        return result

    metadata = ArxivMetadata(
        "2501.00001",
        "Fallback title",
        authors=["Fallback Author"],
        abstract="Fallback abstract",
    )
    with (
        patch(
            "paper2html.cli.ingest_arxiv", return_value=PdfFallbackResult(metadata, pdf)
        ),
        patch("paper2html.cli.run_pipeline", side_effect=annotate) as pipeline,
        patch("paper2html.cli.attach_reading_aids", return_value=bundle),
    ):
        cli = _invoke(
            tmp_path / "cache",
            "--provider",
            "mock",
            "process",
            "2501.00001" if fallback else str(pdf),
            "-o",
            str(out),
        )
    assert cli.exit_code == 0, cli.output
    pipeline.assert_called_once()
    soup = BeautifulSoup(out.read_text(), "html.parser")
    header = soup.select_one(".paper-header")
    assert header and ("Fallback title" if fallback else pdf.name) in header.text
    if fallback:
        assert "Fallback Author" in header.text and "Fallback abstract" in header.text
    assert len(soup.select(".source-facsimile img")) == 1
    assert "Optional margin note" in soup.select_one(".source-reading-aids").text
    assert "Optional figure explanation" in soup.select_one(".source-figure-notes").text
    assert "Optional" not in soup.select_one(".source-content").text


@pytest.mark.parametrize("overwrite", [False, True])
def test_source_depth_preflight_before_provider_preserves_output(
    tmp_path, monkeypatch, overwrite
):
    from paper2html.ingestion._types import ArxivMetadata, ArxivIngestResult

    monkeypatch.setenv("ANTHROPIC_API_KEY", "unused-test-key")
    source = (
        "<article class='ltx_document'>"
        + "<div>" * 135
        + "Content"
        + "</div>" * 135
        + "</article>"
    )
    ingest = ArxivIngestResult(ArxivMetadata("2501.00001", "Deep"), raw_html=source)
    out = tmp_path / "deep.html"
    if overwrite:
        out.write_text("existing output")
    with (
        patch("paper2html.cli.ingest_arxiv", return_value=ingest),
        patch("paper2html.cli.build_provider") as provider,
        patch("paper2html.cli.run_pipeline") as pipeline,
    ):
        args = ["process", "2501.00001", "-o", str(out)] + (
            ["--overwrite"] if overwrite else []
        )
        result = _invoke(tmp_path / "cache", *args)
    assert result.exit_code == 1
    assert (
        "Source preflight error: Source article node/depth limit exceeded"
        in result.output
    )
    assert "Traceback" not in result.output
    provider.assert_not_called()
    pipeline.assert_not_called()
    assert out.read_text() == "existing output" if overwrite else not out.exists()


@pytest.mark.parametrize("failure", ["corrupt", "overbudget", "missing-page"])
def test_pdf_source_errors_before_provider_no_publication(
    tmp_path, monkeypatch, failure
):
    from dataclasses import replace
    from paper2html.ingestion import _limits
    from paper2html.ingestion.pdf import PdfIngestionResult
    from tests.test_cli_process import _make_fixture_pdf

    monkeypatch.setenv("ANTHROPIC_API_KEY", "unused-test-key")
    pdf = _make_fixture_pdf(tmp_path)
    page = tmp_path / "page.png"
    page.write_bytes(b"not an image" if failure == "corrupt" else png())
    if failure == "overbudget":
        monkeypatch.setattr(_limits, "LIMITS", replace(_limits.LIMITS, source_bytes=8))
    ingested = PdfIngestionResult(
        [] if failure == "missing-page" else [page], [], 1, pdf
    )
    out = tmp_path / "bad.html"
    with (
        patch("paper2html.cli.ingest_pdf", return_value=ingested),
        patch("paper2html.cli.build_provider") as provider,
        patch("paper2html.cli.run_pipeline") as pipeline,
    ):
        result = _invoke(tmp_path / "cache", "process", str(pdf), "-o", str(out))
    assert result.exit_code == 1
    assert "Source " in result.output and "error:" in result.output
    assert "Traceback" not in result.output
    provider.assert_not_called()
    pipeline.assert_not_called()
    assert not out.exists()


def test_source_aid_math_loads_assets_but_not_native_only(tmp_path):
    doc = rich_doc()
    # Neither non-rendered normalized LaTeX nor native MathML requires KaTeX.
    doc.sections[0].text_content += r" \(unrendered\)"
    plain = render_html(
        doc, AnnotationBundle(items=[]), tmp_path / "plain.html"
    ).read_text()
    assert "renderMathInElement" not in plain
    assert "paper-header" not in str(BeautifulSoup(plain, "html.parser").find("header"))
    doc.figures[next(iter(doc.figures))].gloss = r"Figure note \(y^2\)"
    bundle = AnnotationBundle(
        items=[],
        term_definitions={"term": r"Definition \(x^2\)"},
        section_summaries={
            doc.sections[0].id: {
                "skim": r"Summary \(x^2\)",
                "read": "Read",
                "deep": "Deep",
            }
        },
    )
    aided = render_html(doc, bundle, tmp_path / "aided.html").read_text()
    assert "renderMathInElement(root" in aided and "KaTeX" in aided
    assert "Copyright (c) 2013-2020 Khan Academy" in aided
    plain_soup, aided_soup = [
        BeautifulSoup(html, "html.parser") for html in [plain, aided]
    ]
    assert str(plain_soup.select_one(".source-content")) == str(
        aided_soup.select_one(".source-content")
    )


def test_direct_source_render_budget_failure_preserves_destination(tmp_path):
    doc = rich_doc()
    doc.original_html = "<div>" * 135 + "Deep" + "</div>" * 135
    out = tmp_path / "keep.html"
    out.write_text("existing")
    with pytest.raises(ValueError, match="node/depth limit"):
        render_html(doc, AnnotationBundle(items=[]), out)
    assert out.read_text() == "existing"


def test_unrendered_partial_summary_math_does_not_load_katex(tmp_path):
    doc = rich_doc()
    bundle = AnnotationBundle(
        items=[],
        section_summaries={doc.sections[0].id: {"skim": r"Incomplete \(x^2\)"}},
    )
    output = render_html(doc, bundle, tmp_path / "partial.html").read_text()
    assert "renderMathInElement" not in output
