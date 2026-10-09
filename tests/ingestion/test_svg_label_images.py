"""Nested LaTeXML rasters must cross guarded ingress, never browser networking."""

import base64
from dataclasses import replace
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock

from bs4 import BeautifulSoup
import httpx
from PIL import Image
import pytest

from paper2html.ingestion import _client, _figure_fetcher, _svg, _svg_browser
from paper2html.ingestion._html_parser import parse_sections
from paper2html.models.document import from_arxiv_html
from tests.ingestion.test_svg_foreignobject import figure, png


def label(source="data.png", extra=""):
    return (
        '<span class="ltx_foreignobject_container"><span class="ltx_foreignobject_content">'
        '<span class="ltx_minipage" style="width:3em">Data '
        f'<img class="ltx_graphics" src="{source}" width="8" height="6" '
        f'alt="Scientific image" style="aspect-ratio:8/6" {extra}/>'
        "</span></span></span>"
    )


def test_label_image_canonical_pixels_no_source_url(monkeypatch):
    seen = []

    async def screenshot(markup, width, height):
        seen.append(markup)
        return png(width, height)

    monkeypatch.setattr(_svg_browser, "_screenshot", screenshot)
    _svg.render_svg(figure(label()), embedded_images={"data.png": png(8, 6)})
    assert 'src="data:image/png;base64,' in seen[0]
    assert 'src="data.png"' not in seen[0]
    assert "img-src data:" in seen[0]
    assert "script-src 'none'" in seen[0]


@pytest.mark.parametrize(
    "source,extra,assets",
    [
        ("https://evil.test/a.png", "", {}),
        ("file:///etc/passwd", "", {}),
        ("data:image/svg+xml;base64,PHN2Zy8+", "", {}),
        ("data.png", 'onload="go()"', {"data.png": png(8, 6)}),
        ("data.png", "", {"data.png": b"not an image"}),
    ],
)
def test_label_images_fail_before_browser(source, extra, assets, monkeypatch):
    screenshot = Mock()
    monkeypatch.setattr(_svg_browser, "_screenshot", screenshot)
    with pytest.raises((ValueError, OSError)):
        _svg.render_svg(figure(label(source, extra)), embedded_images=assets)
    screenshot.assert_not_called()


def test_data_uri_label_pixels_count_toward_aggregate_limit(monkeypatch):
    uri = "data:image/png;base64," + base64.b64encode(png(8, 6)).decode()
    screenshot = Mock()
    monkeypatch.setattr(_svg_browser, "_screenshot", screenshot)
    monkeypatch.setattr(_svg_browser, "LIMITS", replace(_svg_browser.LIMITS, pixels=47))
    with pytest.raises(ValueError, match="aggregate"):
        _svg.render_svg(figure(label(uri)))
    screenshot.assert_not_called()


def test_nested_ingestion_fetch_before_parent_and_reuse(monkeypatch):
    raw = png(8, 6)
    fetched = []
    seen = []

    def respond(request):
        fetched.append(str(request.url))
        return httpx.Response(200, content=raw)

    async def screenshot(markup, width, height):
        seen.append(markup)
        return png(width, height)

    client = _client.RateLimitedClient(
        httpx.Client(transport=httpx.MockTransport(respond))
    )
    monkeypatch.setattr(_client, "_RATE_LIMIT_SECONDS", 0)
    monkeypatch.setattr(_figure_fetcher, "get_client", lambda: client)
    monkeypatch.setattr(_svg_browser, "_screenshot", screenshot)
    html = (
        '<article class="ltx_document"><section id="S1"><h2>Data</h2>'
        '<figure id="F1">'
        + figure(label()).decode()
        + "<figcaption>Measured data</figcaption></figure></section></article>"
    )
    sections, orphans = parse_sections(html, "https://arxiv.org/html/1234.56789v1")
    assert not orphans
    refs = sections[0][1]
    assert refs[0].raw_src == "data.png" and refs[0].inline_svg is None
    assert refs[1].inline_svg is not None
    notices = []
    figures = _figure_fetcher.build_figures(refs, warnings=notices)
    assert not notices
    supplied = {f.raw_src: f.asset_bytes for f in figures}
    doc = from_arxiv_html(
        BeautifulSoup(html, "html.parser"),
        SimpleNamespace(title="Measured data", authors=[], summary="", entry_id=""),
        supplied,
    )
    assert len(doc.source_assets) == 2  # Parent view plus original nested raster.
    assert len(seen) == 1  # Parent raster reused, never label-free fallback.
    assert fetched == ["https://arxiv.org/html/data.png"]
    assert 'src="data:image/png;base64,' in seen[0]


@pytest.mark.browser
def test_real_static_label_image_and_text_fit():
    import os

    if os.environ.get("PAPER2HTML_BROWSER_TESTS") != "1":
        pytest.skip("set PAPER2HTML_BROWSER_TESTS=1 to require Chromium")
    pixels = BytesIO()
    Image.new("RGB", (8, 6), "red").save(pixels, "PNG")
    result = _svg.render_svg(
        figure(label()), embedded_images={"data.png": pixels.getvalue()}
    )
    with Image.open(BytesIO(result)) as image:
        assert image.size == (40, 20)
        assert any(
            r > 200 and g < 80 and b < 80
            for r, g, b in image.convert("RGB").get_flattened_data()
        )


@pytest.mark.browser
@pytest.mark.parametrize("background", ["#FFFFFF", "#F9F9F9"])
def test_malformed_gray_listing_reflows_instead_of_unreadable_shrink(background):
    import os

    if os.environ.get("PAPER2HTML_BROWSER_TESTS") != "1":
        pytest.skip("set PAPER2HTML_BROWSER_TESTS=1 to require Chromium")
    lines = "".join(
        f'<span class="ltx_listingline"><span class="ltx_text">row_{i} = {i}</span></span>'
        for i in range(25)
    )
    data = (
        '<svg width="550" height="52" viewBox="0 0 550 52">'
        '<g fill="#A6A6A6"><path d="M0 0 H550 V52 H0 Z" stroke="none"/></g>'
        f'<g fill="{background}"><path d="M1 1 H549 V51 H1 Z" stroke="none"/></g>'
        '<foreignObject x="15" y="15" width="520" height="14">'
        '<span class="ltx_foreignobject_container"><span class="ltx_foreignobject_content">'
        '<span class="ltx_minipage" style="width:37em"><span class="ltx_listing">'
        + lines
        + "</span></span></span></span></foreignObject></svg>"
    ).encode()
    with pytest.warns(UserWarning, match="reflowed"):
        result = _svg.render_svg(data)
    with Image.open(BytesIO(result)) as image:
        assert image.width == 550 and image.height > 250
        assert image.info["paper2html-reflow"] == "1"


def test_repeated_raster_occurrences_cannot_amplify_browser_markup(monkeypatch):
    from paper2html.ingestion import _safety

    screenshot = Mock()
    serialize = Mock(
        side_effect=AssertionError("budget must reject before serialization")
    )
    original_raster = _safety.raster
    validate = Mock(side_effect=original_raster)
    monkeypatch.setattr(_svg_browser, "_screenshot", screenshot)
    monkeypatch.setattr(_svg_browser, "_serialize", serialize)
    monkeypatch.setattr(_safety, "raster", validate)
    monkeypatch.setattr(
        _svg_browser, "LIMITS", replace(_svg_browser.LIMITS, image_bytes=512)
    )
    images = '<img src="data.png" width="8" height="6"/>' * 20
    with pytest.raises(ValueError, match="aggregate"):
        _svg.render_svg(
            figure("<span>" + images + "</span>"),
            embedded_images={"data.png": png(8, 6)},
        )
    assert validate.call_count == 1
    serialize.assert_not_called()
    screenshot.assert_not_called()


@pytest.mark.browser
def test_minipage_negative_bullet_is_translated_inside_all_four_edges():
    import os

    if os.environ.get("PAPER2HTML_BROWSER_TESTS") != "1":
        pytest.skip("set PAPER2HTML_BROWSER_TESTS=1 to require Chromium")
    data = (
        '<svg width="300" height="80" viewBox="0 0 300 80">'
        '<foreignObject width="290" height="70">'
        '<span class="ltx_foreignobject_container"><span class="ltx_foreignobject_content">'
        '<span class="ltx_minipage" style="width:16em"><span class="ltx_item">'
        '<span class="ltx_tag_item">■</span><span>Retain the marker.</span>'
        "</span></span></span></span></foreignObject></svg>"
    ).encode()
    # The renderer checks every visible text/image rect against every edge.
    result = _svg.render_svg(data)
    with Image.open(BytesIO(result)) as image:
        assert image.size == (300, 80)
        rgb = image.convert("RGB")
        dark = [
            (x, y)
            for y in range(image.height)
            for x in range(image.width)
            if max(rgb.getpixel((x, y))) < 80
        ]
        assert (
            dark and min(x for x, _ in dark) < 12
        )  # formerly off-canvas marker is visible


def test_unmarked_truncated_browser_png_is_rejected(monkeypatch):
    async def cropped(markup, width, height):
        return png(width, 1)

    monkeypatch.setattr(_svg_browser, "_screenshot", cropped)
    with pytest.raises(ValueError, match="dimensions"):
        _svg.render_svg(figure(label()), embedded_images={"data.png": png(8, 6)})


def test_gray_reflow_profile_rejects_unclassified_geometry_and_paint():
    from defusedxml import ElementTree

    from paper2html.ingestion._svg_browser import _gray_card_profile

    base = (
        '<svg width="550" height="52" viewBox="0 0 550 52">'
        '<g fill="#A6A6A6"><path d="M0 0 H550 V52 H0 Z" stroke="none"/></g>'
        '<g fill="#F9F9F9"><path d="M1 1 H549 V51 H1 Z" stroke="none"/></g>'
        '<foreignObject width="520" height="14">'
        '<span class="ltx_foreignobject_container"><span class="ltx_foreignobject_content">'
        '<span class="ltx_minipage">words</span></span></span>'
        "</foreignObject></svg>"
    )
    assert _gray_card_profile(ElementTree.fromstring(base))
    for sibling in (
        "<span>IMPORTANTHEADER</span>",
        '<span class="ltx_foreignobject_content"><span>second content</span></span>',
        "IMPORTANTHEADER",
    ):
        assert not _gray_card_profile(
            ElementTree.fromstring(
                base.replace(
                    "</span></foreignObject>", sibling + "</span></foreignObject>"
                )
            )
        )
    for unwanted in (
        '<rect x="5" y="5" width="20" height="20" fill="#000"/>',
        '<circle cx="10" cy="10" r="5"/>',
        '<path d="M10 10 L20 20" fill="#A6A6A6" stroke="none"/>',
    ):
        assert not _gray_card_profile(
            ElementTree.fromstring(base.replace("</svg>", unwanted + "</svg>"))
        )
    assert not _gray_card_profile(
        ElementTree.fromstring(base.replace("H549 V51 H1 Z", "C10 40 20 30 1 1 Z"))
    )
    assert not _gray_card_profile(
        ElementTree.fromstring(base.replace('stroke="none"', 'stroke="#000000"', 1))
    )
    assert not _gray_card_profile(
        ElementTree.fromstring(
            base.replace(
                'd="M1 1 H549 V51 H1 Z"', 'd="M1 1 H549 V51 H1 Z" fill="#000000"'
            )
        )
    )
    assert not _gray_card_profile(
        ElementTree.fromstring(
            base.replace('viewBox="0 0 550 52"', 'viewBox="5 5 550 52"')
        )
    )
    assert not _gray_card_profile(
        ElementTree.fromstring(
            base.replace('stroke="none"', 'stroke="none" style="stroke:black"', 1)
        )
    )


@pytest.mark.browser
def test_unclassified_geometry_never_discarded_by_reflow():
    import os

    if os.environ.get("PAPER2HTML_BROWSER_TESTS") != "1":
        pytest.skip("set PAPER2HTML_BROWSER_TESTS=1 to require Chromium")
    base = (
        '<svg width="550" height="52" viewBox="0 0 550 52">'
        '<g fill="#A6A6A6"><path d="M0 0 H550 V52 H0 Z" stroke="none"/></g>'
        '<g fill="#F9F9F9"><path d="M1 1 H549 V51 H1 Z" stroke="none"/></g>'
        '<foreignObject x="15" y="15" width="520" height="14">'
        '<span class="ltx_foreignobject_container"><span class="ltx_foreignobject_content">'
        '<span class="ltx_minipage" style="width:37em">'
        + " ".join(["This source panel needs reflow."] * 45)
        + "</span></span></span></foreignObject></svg>"
    )
    for geometry in (
        '<rect x="5" y="5" width="20" height="20" fill="black"/>',
        '<path d="M5 5 L20 20" fill="#A6A6A6" stroke="none"/>',
        '<path d="M1 1 H549 V51 H1 Z" stroke="#000000"/>',
    ):
        with pytest.raises(ValueError, match="Panel too small"):
            _svg.render_svg(base.replace("</svg>", geometry + "</svg>").encode())
    for sibling in (
        "<span>IMPORTANTHEADER</span>",
        '<span class="ltx_foreignobject_content"><span>second content</span></span>',
        "IMPORTANTHEADER",
    ):
        mutated = base.replace(
            "</span></foreignObject>", sibling + "</span></foreignObject>"
        )
        with pytest.raises(ValueError, match="Panel too small"):
            _svg.render_svg(mutated.encode())
    # A direct foreignObject sibling is also never allowed to disappear.
    with pytest.raises(ValueError, match="Panel too small"):
        _svg.render_svg(
            base.replace(
                "</foreignObject>", "<span>IMPORTANTHEADER</span></foreignObject>"
            ).encode()
        )
    with pytest.warns(UserWarning, match="reflowed"):
        result = _svg.render_svg(base.encode())
    with Image.open(BytesIO(result)) as image:
        assert image.info["paper2html-reflow"] == "1"
