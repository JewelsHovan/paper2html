"""Positive compatibility and adversarial followups; no network connections."""

from dataclasses import replace
from io import BytesIO
from unittest.mock import Mock
import base64
import gzip
import zlib

import httpx
from PIL import Image
import pytest

from paper2html.ingestion import _client, _figure_fetcher, _svg
from paper2html.ingestion._client import RateLimitedClient, _reset_singleton
from paper2html.ingestion._figure_fetcher import build_figures
from paper2html.ingestion._limits import LIMITS
from paper2html.ingestion._metadata import fetch_metadata
from paper2html.ingestion._safety import safe_path, atomic_write, cached_image
from paper2html.ingestion._types import FigureRef
from paper2html.ingestion.arxiv import ingest_arxiv
from paper2html.ingestion.pdf import ingest_pdf


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    monkeypatch.setattr(_client, "_RATE_LIMIT_SECONDS", 0)


@pytest.fixture
def png():
    out = BytesIO()
    Image.new("RGB", (12, 8), "red").save(out, format="PNG")
    return out.getvalue()


def client(handler):
    result = RateLimitedClient(httpx.Client(transport=httpx.MockTransport(handler)))
    _reset_singleton(result)
    return result


DVISVGM = b"""<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="120pt" height="80pt" viewBox="0 0 120 80">
<defs><path id="g0" d="M0 0 L10 0 L5 10 Z"/></defs>
<g fill="#123456"><use x="10" y="20" xlink:href="#g0"/><use x="30" y="20" xlink:href="#g0"/></g></svg>"""
MATPLOTLIB = b"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">
<svg xmlns="http://www.w3.org/2000/svg" width="144pt" height="72pt" viewBox="0 0 144 72">
<defs><style type="text/css">*{stroke-linejoin:round;stroke-linecap:butt} .line {stroke: #336699; fill:none}</style><clipPath id="p1"><rect x="0" y="0" width="144" height="72"/></clipPath></defs>
<g clip-path="url(#p1)"><path class="line" style="stroke-width: 2;" d="M0 0 L100 60"/></g></svg>"""


@pytest.mark.parametrize(
    "svg,size",
    [
        (DVISVGM, (160, 107)),
        (MATPLOTLIB, (192, 96)),
        (b'<svg viewBox="0 0 20 30"><path d="M0 0L10 10"/></svg>', (20, 30)),
        (b'<svg width="1in" height="2pc"/>', (96, 32)),
        (b'<svg width="25.4mm" height="2.54cm"/>', (96, 96)),
        (
            b'<svg width="12px" height="8px"><defs><clipPath id="p"><rect width="12" height="8"/></clipPath></defs><path style="clip-path:u\\72l(\'#p\')" d="M0 0L1 1"/></svg>',
            (12, 8),
        ),
    ],
)
def test_usable_svg_units_local_refs_and_css(svg, size, png, monkeypatch):
    converter = Mock(return_value=png)
    monkeypatch.setattr(_svg, "_convert", converter)
    figures = build_figures([FigureRef("usable", inline_svg=svg)])
    assert len(figures) == 1
    # Exact original bytes, including styles, reach the renderer.
    converter.assert_called_once_with(svg, *size)


@pytest.mark.parametrize(
    "content",
    [
        '<style>@import "https://evil.test/s.css";</style>',
        '<style>@\\69mport "file:///etc/passwd";</style>',
        "<style>.a {fill:u\\72l(https://evil.test/x)}</style>",
        '<style>.a {fill: url("file:///etc/passwd")}</style>',
        "<style>@font-face {src:url(https://evil.test/font)}</style>",
        '<path style="fill:url(https://evil.test/a)"/>',
        '<path style="fill:u\\72l(file:///etc/passwd)"/>',
        '<use href="//evil.test/a#x"/>',
        '<use href="file:///tmp/local.svg#x"/>',
        "<foreignObject/>",
        "<script/>",
        '<g onclick="alert(1)"/>',
        '<defs><g id="a"><use href="#b"/></g><g id="b"><use href="#a"/></g></defs><use href="#a"/>',
    ],
)
def test_svg_external_and_active_content_rejected_before_render(content, monkeypatch):
    converter = Mock()
    monkeypatch.setattr(_svg, "_convert", converter)
    with pytest.raises(ValueError):
        _svg.render_svg(f'<svg width="100" height="50">{content}</svg>'.encode())
    converter.assert_not_called()


def test_explicit_cairo_fetch_seam_denies_network_and_files():
    for url in [
        "https://arxiv.org/image",
        "file:///etc/passwd",
        "data:image/png;base64,AAAA",
    ]:
        with pytest.raises(ValueError, match="External SVG"):
            _svg._deny_fetch(url, "image/svg+xml")


def test_trusted_symlink_root_and_ancestor_work(tmp_path, png):
    actual = tmp_path / "actual"
    actual.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(actual, target_is_directory=True)
    for root in (alias, alias / "cache"):
        target = safe_path(root, "ingestion", "downloads", "figure.png")
        atomic_write(png, target, root=root)
        assert cached_image(target, root=root)
        assert target.is_relative_to(actual)
    # Real PDF ingestion must keep the same trusted root at every nested check.
    import pymupdf

    source = tmp_path / "sample.pdf"
    with pymupdf.open() as doc:
        doc.new_page(width=100, height=100)
        doc.save(source)
    assert ingest_pdf(source, alias / "pdf-cache").page_count == 1


@pytest.mark.parametrize(
    "owned",
    [
        "ingestion",
        "ingestion/downloads",
        "ingestion/pdf/hash/pages",
        "ingestion/pdf/hash/pages/0000.jpg",
    ],
)
def test_original_trusted_root_rechecks_owned_symlinks(tmp_path, png, owned):
    root, outside = tmp_path / "cache", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    malicious = root / owned
    malicious.parent.mkdir(parents=True, exist_ok=True)
    malicious.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="Symlink"):
        safe_path(root, owned, "anything")
    with pytest.raises(ValueError, match="Symlink"):
        atomic_write(png, malicious / "anything", root=root)
    assert not list(outside.iterdir())


@pytest.mark.parametrize("part", ["../escape", "/absolute", "a/../../escape"])
def test_path_escape_rejected(tmp_path, part):
    with pytest.raises(ValueError, match="escapes"):
        safe_path(tmp_path, part)


class Raw(httpx.SyncByteStream):
    def __init__(self, content):
        self.content = content

    def __iter__(self):
        yield self.content


@pytest.mark.parametrize(
    "encoding,encode", [("gzip", gzip.compress), ("deflate", zlib.compress)]
)
def test_bounded_compression_happy_and_expansion(encoding, encode):
    def response(body):
        return httpx.Response(
            200, headers={"Content-Encoding": encoding}, stream=Raw(encode(body))
        )

    transport = client(lambda r: response(b"Hello paper" * 10))
    result = transport.get("https://arxiv.org/a", max_bytes=1000)
    assert result.content == b"Hello paper" * 10
    assert "content-encoding" not in result.headers
    bomb = encode(b"x" * 100_000)  # only ~100 encoded bytes, output guard is 1 KiB
    transport = client(
        lambda r: httpx.Response(
            200, headers={"Content-Encoding": encoding}, stream=Raw(bomb)
        )
    )
    with pytest.raises(ValueError, match="decoded body.*byte limit"):
        transport.get("https://arxiv.org/a", max_bytes=1024)


@pytest.mark.parametrize(
    "transform", [lambda b: b[:-3], lambda b: b + b"trailing", lambda b: b + b]
)
def test_compression_truncated_trailing_concatenated_rejected(transform):
    body = transform(gzip.compress(b"paper"))
    transport = client(
        lambda r: httpx.Response(
            200, headers={"content-encoding": "gzip"}, stream=Raw(body)
        )
    )
    with pytest.raises(ValueError, match="Truncated|Trailing/concatenated"):
        transport.get("https://arxiv.org/a")


def test_encoded_budget_and_missing_location():
    body = gzip.compress(b"abc")
    transport = client(
        lambda r: httpx.Response(
            200, headers={"content-encoding": "gzip"}, stream=Raw(body)
        )
    )
    with pytest.raises(ValueError, match="encoded body.*byte limit"):
        transport.get("https://arxiv.org/a", max_bytes=4)
    transport = client(lambda r: httpx.Response(302))
    with pytest.raises(ValueError, match="missing Location"):
        transport.get("https://arxiv.org/a")


@pytest.mark.parametrize(
    "entry_id",
    [
        "http://arxiv.org/api/errors#incorrect_id_format",
        "http://arxiv.org/abs/2301.07042",
        "https://evil.test/2301.07041",
        "http://arxiv.org/abs/2301.070410",
        "",
    ],
)
def test_atom_error_or_wrong_id_rejected(entry_id):
    atom = f'<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>{entry_id}</id><title>Error</title></entry></feed>'.encode()
    client(lambda r: httpx.Response(200, content=atom))
    with pytest.raises(ValueError, match="entry ID|ID mismatch"):
        fetch_metadata("2301.07041")


@pytest.mark.parametrize(
    "entry_id",
    ["http://arxiv.org/abs/2301.07041v12", "https://arxiv.org/abs/math/0211159v3"],
)
def test_atom_exact_normalized_id_accepted(entry_id):
    requested = "math/0211159" if "math/" in entry_id else "2301.07041"
    atom = f'<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>{entry_id}</id><title>Paper</title></entry></feed>'.encode()
    client(lambda r: httpx.Response(200, content=atom))
    assert fetch_metadata(requested).title == "Paper"


def test_data_uri_whitespace_case_type_and_size(png, monkeypatch):
    payload = base64.b64encode(png).decode()
    payload = "\r\n ".join(payload[i : i + 16] for i in range(0, len(payload), 16))
    uri = "DATA: Image/PNG ; BASE64 ,\n" + payload
    assert build_figures([FigureRef("positive", src_url=uri)])[0].asset_bytes == png
    warnings = []
    assert not build_figures(
        [FigureRef("wrong", src_url=uri.replace("PNG", "JPEG"))], warnings=warnings
    )
    assert "MIME type does not match" in warnings[0]
    warnings.clear()
    assert not build_figures(
        [FigureRef("malformed", src_url="data:image/png;base64,!!!")], warnings=warnings
    )
    assert "Malformed base64" in warnings[0]
    monkeypatch.setattr(_figure_fetcher, "LIMITS", replace(LIMITS, image_bytes=8))
    warnings.clear()
    assert not build_figures([FigureRef("oversize", src_url=uri)], warnings=warnings)
    assert "Oversized" in warnings[0]
    with pytest.raises(ValueError, match="Oversized"):
        _figure_fetcher._data_uri("data:image/png;base64," + " " * 1000)


def test_jpeg_preserved_byte_identity_mime_and_size():
    out = BytesIO()
    Image.new("RGB", (100, 80), "blue").save(out, format="JPEG")
    jpeg = out.getvalue()
    client(lambda r: httpx.Response(200, content=jpeg))
    figure = build_figures([FigureRef("photo", src_url="https://arxiv.org/photo.jpg")])[
        0
    ]
    assert figure.asset_bytes == jpeg
    assert figure.mime_type == "image/jpeg"
    assert len(figure.asset_bytes) < 1500


def test_structured_warnings_once_per_unique_asset(tmp_path):
    atom = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/abs/2301.07041v1</id><title>Paper</title></entry></feed>'
    html = b'<section><figure><img src="/bad"/></figure><figure><img src="/bad"/></figure></section>'

    def handler(request):
        if request.url.host == "export.arxiv.org":
            return httpx.Response(200, content=atom)
        if request.url.path.startswith("/html/"):
            return httpx.Response(200, content=html)
        raise httpx.ReadTimeout("image timed out")

    client(handler)
    result = ingest_arxiv("2301.07041", tmp_path)
    assert len(result.ingestion_warnings) == 1
    assert "image timed out" in result.ingestion_warnings[0]
    assert result.metadata.title == "Paper"


def test_conversion_always_passes_deny_fetch_seam(monkeypatch, png):
    import sys
    from types import ModuleType, SimpleNamespace

    surface = ModuleType("cairosvg.surface")
    convert = Mock(return_value=png)
    surface.PNGSurface = SimpleNamespace(convert=convert)
    monkeypatch.setitem(sys.modules, "cairosvg.surface", surface)
    assert _svg._convert(DVISVGM, 160, 107) == png
    convert.assert_called_once_with(
        bytestring=DVISVGM,
        output_width=160,
        output_height=107,
        unsafe=False,
        url_fetcher=_svg._deny_fetch,
    )
