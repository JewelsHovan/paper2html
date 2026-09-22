"""SVG compatibility tests, with explicitly optional real native rendering."""

from dataclasses import replace
from io import BytesIO
from unittest.mock import Mock
import base64
import builtins

import httpx
from PIL import Image
import pytest

from paper2html.ingestion import _client, _figure_fetcher, _svg
from paper2html.ingestion._client import RateLimitedClient
from paper2html.ingestion._figure_fetcher import build_figures
from paper2html.ingestion._types import FigureRef
from tests.ingestion.test_review_followups import DVISVGM, MATPLOTLIB


@pytest.fixture
def raster_uri():
    output = BytesIO()
    Image.new("RGB", (8, 6), "#ff3300").save(output, format="PNG")
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode()


def imshow(uri: str) -> bytes:
    return f'''<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"
      width="100%" height="100%" viewBox="0 0 80 60" aria-label="Bob's plot" data-note="'not CSS">
      <defs><clipPath id="p1"><rect width="80" height="60"/></clipPath></defs>
      <g clip-path="url(#p1)"><image width="80" height="60" xlink:href="{uri}"/></g>
    </svg>'''.encode()


def test_percent_dimensions_and_non_css_prose(monkeypatch):
    svg = b"""<svg width="100%" height="100%" viewBox="0 0 80 60" aria-label="Bob's plot" data-note="'not CSS"><title>Bob's plot</title><rect width="40" height="30" fill="red"/></svg>"""
    convert = Mock(return_value=b"converted")
    monkeypatch.setattr(_svg, "_convert", convert)
    assert _svg.render_svg(svg) == b"converted"
    convert.assert_called_once_with(svg, 80, 60)


@pytest.mark.parametrize(
    "width,height,viewbox",
    [
        ("100%", "100%", ""),
        ("50%", "100%", "0 0 80 60"),
        ("200%", "100%", "0 0 80 60"),
        ("100%", "100%", "0 0 999999 999999"),
    ],
)
def test_undefined_percentage_or_huge_viewbox_rejected(
    width, height, viewbox, monkeypatch
):
    converter = Mock()
    monkeypatch.setattr(_svg, "_convert", converter)
    with pytest.raises(ValueError):
        _svg.render_svg(
            f'<svg width="{width}" height="{height}" viewBox="{viewbox}"/>'.encode()
        )
    converter.assert_not_called()


def test_embedded_raster_canonical_prevalidated_mapping(raster_uri, monkeypatch):
    converter = Mock(return_value=b"converted")
    monkeypatch.setattr(_svg, "_convert", converter)
    noncanonical = raster_uri.replace("image/png;base64,", "Image/PNG ; BASE64 ,\n")
    _svg.render_svg(imshow(noncanonical))
    data, width, height, resources = converter.call_args.args
    assert (width, height) == (80, 60)
    assert set(resources) == {raster_uri}
    assert raster_uri.encode() in data
    with Image.open(BytesIO(resources[raster_uri])) as image:
        assert image.size == (8, 6)


def test_fetcher_only_serves_exact_prevalidated_raster(raster_uri, monkeypatch):
    import sys
    from types import ModuleType, SimpleNamespace

    surface = ModuleType("cairosvg.surface")
    native = Mock(return_value=b"converted")
    surface.PNGSurface = SimpleNamespace(convert=native)
    monkeypatch.setitem(sys.modules, "cairosvg.surface", surface)
    raw = base64.b64decode(raster_uri.partition(",")[2])
    _svg._convert(b"<svg/>", 80, 60, {raster_uri: raw})
    fetch = native.call_args.kwargs["url_fetcher"]
    assert fetch(raster_uri, "image/*") == raw
    for url in [
        "https://evil.test/a",
        "file:///etc/passwd",
        raster_uri + "#extra",
        "data:image/svg+xml;base64,PHN2Zy8+",
    ]:
        with pytest.raises(ValueError, match="External SVG"):
            fetch(url, "image/*")


@pytest.mark.parametrize(
    "uri",
    [
        "https://evil.test/a.png",
        "file:///tmp/a.png",
        "data:text/html;base64,PGh0bWwvPg==",
        "data:image/svg+xml;base64,PHN2Zy8+",
        "data:image/png;base64,!!!",
        "data:image/png;base64,PHN2Zy8+",
    ],
)
def test_embedded_non_raster_or_malformed_rejected(uri, monkeypatch):
    converter = Mock()
    monkeypatch.setattr(_svg, "_convert", converter)
    with pytest.raises((ValueError, OSError)):
        _svg.render_svg(imshow(uri))
    converter.assert_not_called()


def test_embedded_raster_aggregate_pixel_budget(raster_uri, monkeypatch):
    monkeypatch.setattr(_svg, "LIMITS", replace(_svg.LIMITS, pixels=1))
    with pytest.raises(ValueError, match="aggregate limit"):
        _svg.render_svg(imshow(raster_uri))


def test_svg_data_uri_routes_through_same_validation(monkeypatch):
    converter = Mock(return_value=b"fake PNG")
    monkeypatch.setattr(_figure_fetcher, "svg_png", converter)
    uri = "data:image/svg+xml;base64," + base64.b64encode(DVISVGM).decode()
    assert _figure_fetcher._data_uri(uri, allow_svg=True) == b"fake PNG"
    converter.assert_called_once_with(DVISVGM)
    with pytest.raises(ValueError, match="SVG allowed only at figure ingress"):
        _figure_fetcher._data_uri(uri)


def test_svg_data_uri_over_budget_rejected_before_render(monkeypatch):
    monkeypatch.setattr(
        _figure_fetcher, "LIMITS", replace(_figure_fetcher.LIMITS, image_bytes=8)
    )
    converter = Mock()
    monkeypatch.setattr(_figure_fetcher, "svg_png", converter)
    uri = "data:image/svg+xml;base64," + base64.b64encode(DVISVGM).decode()
    with pytest.raises(ValueError, match="Oversized"):
        _figure_fetcher._data_uri(uri, allow_svg=True)
    converter.assert_not_called()


def test_custom_headers_merge_enforces_identity(monkeypatch):
    monkeypatch.setattr(_client, "_RATE_LIMIT_SECONDS", 0)
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(302, headers={"location": "/next"})
        return httpx.Response(200, content=b"body")

    client = RateLimitedClient(httpx.Client(transport=httpx.MockTransport(handler)))
    result = client.get(
        "https://arxiv.org/start", headers={"X-Test": "custom", "accept-encoding": "br"}
    )
    assert result.content == b"body"
    for request in calls:
        assert request.headers["X-Test"] == "custom"
        assert request.headers["Accept-Encoding"] == "identity"


def test_missing_cairo_warning_is_actionable_and_short(monkeypatch):
    original = builtins.__import__

    def missing(name, *args, **kwargs):
        if name == "cairosvg.surface":
            raise OSError("loader failed " * 100)
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing)
    warnings = []
    assert (
        build_figures([FigureRef("plot", inline_svg=DVISVGM)], warnings=warnings) == []
    )
    assert len(warnings) == 1 and len(warnings[0]) < 350
    assert "brew install cairo" in warnings[0]
    assert "DYLD_FALLBACK_LIBRARY_PATH" in warnings[0]
    assert "loader failed" not in warnings[0]


@pytest.fixture
def real_cairo():
    try:
        from cairosvg.surface import PNGSurface
    except (ImportError, OSError) as exc:
        pytest.skip(
            f"Real Cairo unavailable ({type(exc).__name__}); install Cairo or set DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib"
        )
    return PNGSurface


@pytest.mark.parametrize("kind", ["dvisvgm", "matplotlib", "imshow", "svg-data-uri"])
def test_real_cairo_compatible_svg_nonempty(real_cairo, raster_uri, kind):
    svg = {
        "dvisvgm": DVISVGM,
        "matplotlib": MATPLOTLIB,
        "imshow": imshow(raster_uri),
        "svg-data-uri": imshow(raster_uri),
    }[kind]
    expected = {
        "dvisvgm": (160, 107),
        "matplotlib": (192, 96),
        "imshow": (80, 60),
        "svg-data-uri": (80, 60),
    }[kind]
    if kind == "svg-data-uri":
        uri = "data:image/svg+xml;base64," + base64.b64encode(svg).decode()
        ref = FigureRef("positive", src_url=uri)
    else:
        ref = FigureRef("positive", inline_svg=svg)
    warnings = []
    figures = build_figures([ref], warnings=warnings)
    assert not warnings
    assert len(figures) == 1
    with Image.open(BytesIO(figures[0].asset_bytes)) as image:
        assert image.size == expected
        assert image.getbbox() is not None
        if kind in {"imshow", "svg-data-uri"}:
            assert image.convert("RGB").getpixel((40, 30)) == (255, 51, 0)


@pytest.mark.parametrize(
    "body",
    [
        '<image href="https://evil.test/image.png"/>',
        '<image href="data:image/svg+xml;base64,PHN2Zy8+"/>',
        "<style>path{fill:u\\72l(file:///etc/passwd)}</style>",
        '<defs><g id="a"><use href="#a"/></g></defs><use href="#a"/>',
        '<path fill="url(https://evil.test/a)"/>',
    ],
)
def test_real_cairo_malicious_inputs_rejected(real_cairo, body):
    with pytest.raises(ValueError):
        _svg.render_svg(f'<svg width="80" height="60">{body}</svg>'.encode())
