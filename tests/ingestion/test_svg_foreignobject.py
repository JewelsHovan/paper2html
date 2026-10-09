"""Static LaTeXML SVGs: strict reconstruction before any browser invocation."""

import asyncio
import os
from io import BytesIO
from unittest.mock import Mock

from PIL import Image, ImageChops
import pytest

from paper2html.ingestion import _svg, _svg_browser


def png(width=40, height=20):
    out = BytesIO()
    Image.new("RGBA", (width, height), "white").save(out, "PNG")
    return out.getvalue()


def figure(
    label='<span class="ltx_foreignobject_container"><span class="ltx_foreignobject_content"><span class="ltx_text" style="font-size:80%;font-weight:bold">Hello <math display="inline"><semantics><mrow><mi>x</mi><mo>+</mo><mn>1</mn></mrow><annotation encoding="application/x-tex">x+1</annotation></semantics></math></span></span></span>',
    *,
    fo_attrs='width="35" height="15" style="--ltx-fo-width:2em;--ltx-fo-height:0.7em;--ltx-fo-depth:0.1em;font-size:8.5pt"',
):
    return (
        f'<svg width="40" height="20" viewbox="0 0 40 20"><g style="--ltx-fill-color:#123456;--ltx-stroke-color:#123456" fill="#123456"><path d="M0 0 L2 2"/><foreignobject {fo_attrs}>{label}</foreignobject></g></svg>'
    ).encode()


def test_reconstructed_static_math_and_lowercase_viewbox(monkeypatch):
    seen = []

    async def screenshot(markup, width, height):
        seen.append((markup, width, height))
        return png(width, height)

    monkeypatch.setattr(_svg_browser, "_screenshot", screenshot)
    monkeypatch.setattr(
        _svg, "_convert", Mock(side_effect=AssertionError("Cairo must not run"))
    )
    result = _svg.render_svg(figure())
    assert result.startswith(b"\x89PNG")
    html, width, height = seen[0]
    assert (width, height) == (40, 20)
    assert 'viewBox="0 0 40 20"' in html
    assert 'xmlns="http://www.w3.org/1999/xhtml"' in html
    assert 'xmlns="http://www.w3.org/1998/Math/MathML"' in html
    assert "x+1</annotation>" not in html
    assert "#123456" in html and "font-weight:bold" in html
    assert ".ltx_font_bold{font-weight:bold}" in html
    assert ".ltx_font_italic{font-style:italic}" in html
    assert (
        "display:flex;align-items:flex-end;height:100%;max-width:var(--ltx-fo-width)"
        in html
    )
    assert "line-height:1.2" in html


@pytest.mark.parametrize(
    "label,attrs",
    [
        ("", 'width="35" height="15"'),
        ("<span>  </span>", 'width="35" height="15"'),
        ("<script>alert(1)</script><span>ok</span>", 'width="35" height="15"'),
        ('<img src="file:///etc/passwd"/>', 'width="35" height="15"'),
        ('<span onclick="alert(1)">ok</span>', 'width="35" height="15"'),
        (
            '<span style="background:url(file:///tmp/x)">ok</span>',
            'width="35" height="15"',
        ),
        ('<span style="color:var(--bad)">ok</span>', 'width="35" height="15"'),
        (
            '<span style="font-size:calc(100px * 100)">ok</span>',
            'width="35" height="15"',
        ),
        ('<span style="f\\6fnt-size:10px">ok</span>', 'width="35" height="15"'),
        ('<span style="font-size:16000em">ok</span>', 'width="35" height="15"'),
        ('<span style="font-size:16000pt">ok</span>', 'width="35" height="15"'),
        (
            '<span><math><mspace width="16000em"/></math>ok</span>',
            'width="35" height="15"',
        ),
        (
            '<span><math href="https://evil.test"><mi>x</mi></math></span>',
            'width="35" height="15"',
        ),
        (
            '<span><math><semantics><annotation encoding="application/x-tex" onload="go()">x</annotation></semantics></math></span>',
            'width="35" height="15"',
        ),
        (
            "<span>ok</span>",
            'width="35" height="15" style="--ltx-fo-width:100em;--evil:url(https://evil.test)"',
        ),
        ("<span>ok</span>", 'width="35" height="15" src="file:///etc/passwd"'),
    ],
)
def test_bad_labels_fail_before_browser(label, attrs, monkeypatch):
    screenshot = Mock(side_effect=AssertionError("browser must not run"))
    monkeypatch.setattr(_svg_browser, "_screenshot", screenshot)
    with pytest.raises(ValueError):
        _svg.render_svg(figure(label, fo_attrs=attrs))
    screenshot.assert_not_called()


def test_geometry_rejected_before_browser(monkeypatch):
    screenshot = Mock()
    monkeypatch.setattr(_svg_browser, "_screenshot", screenshot)
    for original, replacement in [
        (b"M0 0 L2 2", b"M0 0 L2 2;evil()"),
        (b'fill="#123456"', b'fill="url(file:///x)"'),
        (b"<path ", b'<image href="data:image/png;base64,AAA"/><path '),
        (b"<g style=", b'<g onclick="alert(1)" style='),
        (b"<path ", b'<svg width="1" height="1"></svg><path '),
    ]:
        with pytest.raises(ValueError):
            _svg.render_svg(figure().replace(original, replacement))
    screenshot.assert_not_called()


def test_latexml_custom_paint_without_explicit_fill(monkeypatch):
    seen = []

    async def screenshot(markup, width, height):
        seen.append(markup)
        return png(width, height)

    monkeypatch.setattr(_svg_browser, "_screenshot", screenshot)
    data = figure().replace(b' fill="#123456"', b"")
    _svg.render_svg(data)
    assert 'fill="#123456"' in seen[0]
    assert 'stroke="#123456"' in seen[0]


def test_missing_browser_has_install_guidance(monkeypatch):
    import builtins

    original = builtins.__import__

    def missing(name, *args, **kwargs):
        if name == "playwright.async_api":
            raise ImportError("missing")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing)
    with pytest.raises(ValueError, match="playwright install chromium"):
        _svg.render_svg(figure())


def test_async_caller_and_invalid_png(monkeypatch):
    async def screenshot(markup, width, height):
        return png(width, height)

    monkeypatch.setattr(_svg_browser, "_screenshot", screenshot)

    async def caller():
        return _svg.render_svg(figure())

    assert asyncio.run(caller()).startswith(b"\x89PNG")

    async def bad(markup, width, height):
        return b"not png"

    monkeypatch.setattr(_svg_browser, "_screenshot", bad)
    with pytest.raises(ValueError, match="Invalid LaTeXML SVG screenshot"):
        _svg.render_svg(figure())


@pytest.mark.browser
def test_real_offline_chromium_static_math():
    if os.environ.get("PAPER2HTML_BROWSER_TESTS") != "1":
        pytest.skip("set PAPER2HTML_BROWSER_TESTS=1 to require Chromium")
    with Image.open(BytesIO(_svg.render_svg(figure()))) as image:
        assert image.size == (40, 20)
        # White is deliberate: black diagram labels must remain readable in a
        # dark reader too. Check label ink outside the tiny path at x=0..2,
        # rather than accepting an entirely white (but opaque) screenshot.
        rgb = image.convert("RGB")
        assert rgb.getpixel((39, 19)) == (255, 255, 255)
        labels = rgb.crop((4, 0, 35, 15))
        assert ImageChops.difference(
            labels, Image.new("RGB", labels.size, "white")
        ).getbbox()


def test_bounded_latexml_panel_layout_and_listing_projection(monkeypatch):
    seen = []

    async def screenshot(markup, width, height):
        seen.append(markup)
        return png(width, height)

    monkeypatch.setattr(_svg_browser, "_screenshot", screenshot)
    label = (
        '<span class="ltx_minipage" style="width:3em">'
        '<span class="ltx_p" style="--ltx-fg-color:#000000">Question</span>'
        '<span class="ltx_listing">'
        '<span class="ltx_listing_data"><a download="" '
        'href="data:text/plain;base64,eD0x">↓</a></span>'
        '<span class="ltx_listingline">x=1</span></span>'
        '<span class="ltx_td" style="padding:0.65pt 5pt">Data</span></span>'
    )
    data = figure(label).replace(b"<g style=", b'<g fill-opacity="1.0" style=')
    _svg.render_svg(data)
    html = seen[0]
    assert "Question" in html and "x=1" in html and "Data" in html
    assert 'fill-opacity="1.0"' in html
    assert "data:text" not in html and "href=" not in html and "download=" not in html
    assert "display:table-cell" in html and "white-space:pre" in html


@pytest.mark.parametrize("value", ["-1", "1.1", "50%", "1px", "nan", "1e999"])
def test_panel_opacity_rejects_non_unit_interval(value, monkeypatch):
    screenshot = Mock()
    monkeypatch.setattr(_svg_browser, "_screenshot", screenshot)
    data = figure().replace(b"<g style=", f'<g fill-opacity="{value}" style='.encode())
    with pytest.raises(ValueError):
        _svg.render_svg(data)
    screenshot.assert_not_called()


@pytest.mark.parametrize(
    "link",
    [
        '<a download="" href="https://evil.test/a">↓</a>',
        '<a download="" href="file:///tmp/a">↓</a>',
        '<a download="" href="data:text/html;base64,eD0x">↓</a>',
        '<a download="" href="data:text/plain;base64,!!!">↓</a>',
        '<a download="" href="data:text/plain;base64,eD0x" onclick="go()">↓</a>',
        '<a download="" href="data:text/plain;base64,eD0x"><img src="x"/></a>',
    ],
)
def test_listing_projection_never_enables_active_or_external_links(link, monkeypatch):
    screenshot = Mock()
    monkeypatch.setattr(_svg_browser, "_screenshot", screenshot)
    with pytest.raises(ValueError):
        _svg.render_svg(figure('<span class="ltx_listing_data">' + link + "</span>"))
    screenshot.assert_not_called()


@pytest.mark.parametrize(
    "style",
    [
        "width:10000em",
        "padding:-1px",
        "padding:1px 1px 1px 1px 1px",
        "--ltx-fg-color:url(file:///tmp/a)",
        "list-style-type:url(https://evil.test)",
    ],
)
def test_panel_layout_stays_bounded_and_resource_free(style, monkeypatch):
    screenshot = Mock()
    monkeypatch.setattr(_svg_browser, "_screenshot", screenshot)
    with pytest.raises(ValueError):
        _svg.render_svg(figure(f'<span style="{style}">text</span>'))
    screenshot.assert_not_called()


def test_no_foreignobject_keeps_cairo_original_bytes(monkeypatch):
    cairo = Mock(return_value=png())
    monkeypatch.setattr(_svg, "_convert", cairo)
    data = b'<svg width="40" height="20"><path d="M0 0 L2 2"/></svg>'
    assert _svg.render_svg(data) == png()
    cairo.assert_called_once_with(data, 40, 20)


@pytest.mark.parametrize(
    "separator,expected",
    [
        (" ", "foo bar"),
        ("\t", "foo\tbar"),
        ("  \t ", "foo  \t bar"),
        ("\n    ", "foo\n    bar"),
        ("\n", "foo\nbar"),
    ],
)
def test_listing_literal_whitespace_preserved_except_prettyprint_indent(
    separator, expected
):
    from defusedxml import ElementTree

    listing = ElementTree.fromstring(
        '<span class="ltx_listingline"><span>foo</span>'
        + separator
        + "<span>bar</span></span>"
    )
    serialized, _ = _svg_browser._serialize(listing, "span")
    emitted_separator = separator
    assert f"foo</span>{emitted_separator}<span>bar" in serialized
    assert "".join(ElementTree.fromstring(serialized).itertext()) == expected


def test_publisher_cannot_mint_renderer_reflow_marker(monkeypatch):
    screenshot = Mock(side_effect=AssertionError("browser must not run"))
    monkeypatch.setattr(_svg_browser, "_screenshot", screenshot)
    data = figure().replace(b"<svg ", b'<svg data-paper2html-gray-card="1" ')
    with pytest.raises(ValueError, match="Unsupported LaTeXML SVG attribute"):
        _svg.render_svg(data)
    screenshot.assert_not_called()


def test_listing_leading_newline_before_child_is_not_stripped():
    from defusedxml import ElementTree

    listing = ElementTree.fromstring(
        '<span class="ltx_listingline">\n<span>foo</span>\n</span>'
    )
    serialized, _ = _svg_browser._serialize(listing, "span")
    assert "".join(ElementTree.fromstring(serialized).itertext()) == "\nfoo"
