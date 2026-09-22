"""Untrusted adapter and normalized-document rendering regressions."""

import base64
from io import BytesIO
from types import SimpleNamespace

import pytest
from bs4 import BeautifulSoup
from PIL import Image

from paper2html.models.document import from_arxiv_html
from paper2html.renderer import render_html, _sentences_to_html
from paper2html.review.schemas import AnnotationBundle


def adapt(html, assets=None):
    metadata = SimpleNamespace(
        title="Paper", authors=[], summary="", entry_id="2301.00001"
    )
    return from_arxiv_html(BeautifulSoup(html, "html.parser"), metadata, assets)


def render(doc, tmp_path, terms=None):
    path = render_html(
        doc,
        AnnotationBundle(items=[], term_definitions=terms or {}),
        tmp_path / "paper.html",
    )
    return BeautifulSoup(path.read_text(), "html.parser")


def test_adapter_mathml_is_inert(tmp_path):
    doc = adapt("""<section><h2>Math</h2><p>Preserved prose</p>
      <math display="block" onclick="alert(1)" href="https://evil.test">
      <semantics><mfrac><mi>x</mi><mn>2</mn></mfrac>
      <annotation-xml encoding="text/html"><img src="x" onerror="alert(1)"></annotation-xml>
      <script>alert(1)</script><unknown><mi>DROP</mi></unknown>
      <mtext><img src="evil">safe text</mtext>
      <mo style="background:url(https://evil.test)" xlink:href="javascript:alert(1)">+</mo>
      </semantics></math></section>""")
    soup = render(doc, tmp_path)
    math = soup.find("math")
    assert math and math.find("mfrac") and math.find("mn").text == "2"
    assert not math.find(["annotation-xml", "img", "script", "unknown"])
    assert "DROP" not in math.text
    assert not any(
        key in {"style", "href", "xlink:href"} or key.startswith("on")
        for t in [math, *math.find_all(True)]
        for key in t.attrs
    )
    assert "Preserved prose" in soup.text


def test_tables_are_rebuilt_and_table_only_math_loads_katex(tmp_path):
    doc = adapt("""<section><h2>Table</h2><figure><table class="ltx_tabular"><tr>
    <th rowspan="2" colspan="3">Layer</th><td><math alttext="x^2"><mi>x</mi></math></td>
    </tr></table><figcaption>Caption &amp; meaning</figcaption></figure></section>""")
    doc.sections[
        0
    ].tables.append("""<table onclick="alert(1)"><tr><td colspan='2" onmouseover="x'>A &amp; B
    <script>BAD</script><iframe>BAD</iframe><img src=x onerror=x>
    <math><mi>y</mi></math></td></tr></table>""")
    doc.original_html = ""  # Exercise the legacy normalized-table boundary.
    soup = render(doc, tmp_path)
    assert soup.find("th")["rowspan"] == "2"
    assert soup.find("th")["colspan"] == "3"
    assert "Caption & meaning" in soup.text
    assert "renderMathInElement" in str(soup)
    tables = soup.find_all("table")
    assert len(tables) == 2 and tables[1].find("math")
    assert not tables[1].find(["script", "iframe", "img"])
    assert not tables[1].find("td").attrs
    assert "A & B" in tables[1].text


@pytest.mark.parametrize(
    "format,mime",
    [
        ("PNG", "image/png"),
        ("JPEG", "image/jpeg"),
        ("GIF", "image/gif"),
        ("WEBP", "image/webp"),
    ],
)
def test_actual_raster_mime_through_adapter(format, mime, tmp_path):
    stream = BytesIO()
    Image.new("RGB", (2, 2)).save(stream, format=format)
    data = stream.getvalue()
    doc = adapt(
        '<section><figure><img src="a.png"></figure></section>', {"a.png": data}
    )
    assert doc.figures["fig-1"].image_mime_type == mime
    doc.figures["fig-1"].image_mime_type = 'image/svg+xml" onerror="alert(1)'
    soup = render(doc, tmp_path)
    img = soup.find("img")
    assert img["src"] == f"data:{mime};base64,{base64.b64encode(data).decode()}"
    assert "onerror" not in img.attrs


@pytest.mark.parametrize(
    "data", [b"corrupt", b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>']
)
def test_unsafe_images_degrade_in_adapter_and_renderer(data, tmp_path):
    doc = adapt(
        '<section><p>Keep this section</p><figure><img src="a"><figcaption>Caption</figcaption></figure></section>',
        {"a": data},
    )
    assert not doc.figures["fig-1"].image_bytes
    assert doc.ingestion_warnings
    doc.figures["fig-1"].image_bytes = data
    with pytest.warns(UserWarning, match="figure omitted"):
        soup = render(doc, tmp_path)
    assert not soup.find("img")
    assert "Keep this section" in soup.text and "Caption" in soup.text


def test_raw_text_intervals_and_escaped_ids():
    text = "a Figure 1 ref & entity &amp; gradient descent gradient"
    fid = 'fig-" onmouseover="evil-1'
    sid = 'sec-" onclick="evil'
    html = _sentences_to_html(
        text,
        sid,
        {1: fid},
        {
            "a": 'A " definition',
            "fig": "fig",
            "ref": "ref",
            "&": "ampersand",
            "gradient": "short",
            "gradient descent": "long",
            "amp": "not an entity",
        },
        set(),
    )
    soup = BeautifulSoup(str(html), "html.parser")
    assert soup.text == text
    assert soup.a["href"] == "#" + fid and soup.a.text == "Figure 1"
    assert soup.span["id"] == sid + "-sent-1"
    assert not any(
        key.startswith("on") for tag in soup.find_all(True) for key in tag.attrs
    )
    assert soup.find(attrs={"data-def": "long"}).text == "gradient descent"
    assert soup.find(attrs={"data-def": "short"}).text == "gradient"


def bomb_png():
    """Valid PNG header advertising huge dimensions; no pixel allocation."""
    import struct
    import zlib

    ihdr = b"IHDR" + struct.pack(">IIBBBBB", 30000, 30000, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + ihdr
        + struct.pack(">I", zlib.crc32(ihdr))
        + b"\x00\x00\x00\x00IDAT"
    )


def test_decompression_bomb_degrades_and_poisoned_cache_regenerates(tmp_path):
    import pymupdf
    from paper2html.ingestion._safety import cached_image, raster
    from paper2html.ingestion.pdf import ingest_pdf

    data = bomb_png()
    original_limit = Image.MAX_IMAGE_PIXELS
    with pytest.raises(ValueError, match="decompression bomb"):
        raster(data)
    doc = adapt(
        '<section><p>Kept</p><figure><img src="a"></figure></section>', {"a": data}
    )
    assert not doc.figures["fig-1"].image_bytes
    assert any("decompression bomb" in warning for warning in doc.ingestion_warnings)
    doc.figures["fig-1"].image_bytes = data
    with pytest.warns(UserWarning, match="decompression bomb"):
        soup = render(doc, tmp_path)
    assert not soup.find("img") and "Kept" in soup.text

    pdf_path = tmp_path / "source.pdf"
    with pymupdf.open() as pdf:
        pdf.new_page(width=72, height=72).insert_text((10, 30), "Kept")
        pdf.save(pdf_path)
    cache = tmp_path / "cache"
    result = ingest_pdf(pdf_path, cache_dir=cache)
    page_path = result.page_image_paths[0]
    page_path.write_bytes(data)
    assert not cached_image(page_path, root=cache)
    recovered = ingest_pdf(pdf_path, cache_dir=cache)
    assert recovered.page_image_paths[0] == page_path
    assert cached_image(page_path, root=cache)
    assert page_path.read_bytes() != data
    assert Image.MAX_IMAGE_PIXELS == original_limit


@pytest.mark.parametrize("format", ["GIF", "WEBP"])
def test_animated_rasters_rejected_before_workers(format, tmp_path):
    from paper2html.ingestion._figure_fetcher import build_figures
    from paper2html.ingestion._safety import cached_image, raster
    from paper2html.ingestion._types import FigureRef

    stream = BytesIO()
    Image.new("RGB", (2, 2), "red").save(
        stream,
        format=format,
        save_all=True,
        append_images=[Image.new("RGB", (2, 2), "blue")],
        duration=100,
        loop=0,
    )
    data = stream.getvalue()
    with pytest.raises(ValueError, match="Animated raster"):
        raster(data)
    warnings = []
    uri = f"data:image/{format.lower()};base64,{base64.b64encode(data).decode()}"
    assert not build_figures([FigureRef("animation", src_url=uri)], warnings=warnings)
    assert len(warnings) == 1 and "Animated raster" in warnings[0]
    doc = adapt(
        '<section><p>Kept</p><figure><img src="a"></figure></section>', {"a": data}
    )
    assert not doc.figures["fig-1"].image_bytes and doc.ingestion_warnings
    doc.figures["fig-1"].image_bytes = data
    with pytest.warns(UserWarning, match="Animated raster"):
        assert not render(doc, tmp_path).find("img")
    cached = tmp_path / "animated"
    cached.write_bytes(data)
    assert not cached_image(cached)


@pytest.mark.parametrize("depth", [400, 800])
@pytest.mark.parametrize("table", [False, True])
def test_deep_markup_is_bounded_directly_and_during_render(depth, table, tmp_path):
    from paper2html._render_safety import sanitize_markup

    math = (
        '<math display="block">'
        + "<mrow>" * depth
        + "<mi>x</mi>"
        + "</mrow>" * depth
        + "</math>"
    )
    source = "<table><tr><td>" + math + "</td></tr></table>" if table else math
    with pytest.warns(UserWarning, match="depth limit") as recorded:
        assert sanitize_markup(source, table=table) == ""
    assert len(recorded) == 1
    doc = adapt("<section><p>Kept</p>" + ("" if table else math) + "</section>")
    doc.original_html = ""  # Legacy normalized markup limit contract.
    if table:
        doc.sections[0].tables.append(source)
    with pytest.warns(UserWarning, match="depth limit") as recorded:
        soup = render(doc, tmp_path)
    assert len(recorded) == 1
    assert not soup.find("math") and "Kept" in soup.text


@pytest.mark.parametrize("limit", ["bytes", "nodes"])
def test_markup_source_and_node_limits(limit, tmp_path):
    from paper2html._render_safety import (
        MAX_MARKUP_BYTES,
        MAX_MARKUP_NODES,
        sanitize_markup,
    )

    source = (
        "é" * (MAX_MARKUP_BYTES // 2 + 1)
        if limit == "bytes"
        else "<mspace></mspace>" * MAX_MARKUP_NODES
    )
    source = "<math>" + source + "</math>"
    with pytest.warns(UserWarning, match="limit exceeded") as recorded:
        assert sanitize_markup(source) == ""
    assert len(recorded) == 1
    doc = adapt('<section><p>Kept</p><math display="block"><mi>x</mi></math></section>')
    doc.original_html = ""  # Exercise the normalized equation boundary.
    next(iter(doc.equations.values())).mathml = source
    with pytest.warns(UserWarning, match="limit exceeded") as recorded:
        soup = render(doc, tmp_path)
    assert len(recorded) == 1 and not soup.find("math")


def test_supported_markup_at_depth_limit_and_long_span_digits(tmp_path):
    from paper2html._render_safety import sanitize_markup

    math = "<math>" + "<mrow>" * 62 + "<mi>x</mi>" + "</mrow>" * 62 + "</math>"
    assert str(sanitize_markup(math)) == math
    digits = "9" * 5000
    table = f'<table class="ltx_tabular"><tr><td rowspan="{digits}" colspan="{digits}">Kept</td><th rowspan="2" colspan="3">Header</th></tr></table>'
    direct = BeautifulSoup(str(sanitize_markup(table, table=True)), "html.parser")
    assert not direct.td.attrs
    assert direct.th.attrs == {"rowspan": "2", "colspan": "3"}
    soup = render(adapt("<section>" + table + "</section>"), tmp_path)
    assert not soup.td.attrs
    assert soup.th.attrs == {"rowspan": "2", "colspan": "3"}
    assert "Kept" in soup.text


def test_sectionless_document_skips_active_prose(tmp_path):
    doc = adapt("""<html><body><script>LEAK_JS</script><style>LEAK_CSS</style>
    <noscript>LEAK_NOSCRIPT</noscript><template><p>LEAK_TEMPLATE</p></template>
    <p>Readable paper</p></body></html>""")
    assert "Readable paper" in doc.sections[0].text_content
    assert "LEAK_" not in doc.sections[0].text_content
    assert "LEAK_" not in str(render(doc, tmp_path))
