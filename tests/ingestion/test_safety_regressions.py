"""Offline security/resource regressions; tiny fixtures and mock transports only."""

from dataclasses import replace
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import base64

import httpx
import pymupdf
import pytest
from bs4 import BeautifulSoup
from PIL import Image

from paper2html.ingestion import _client, _safety, pdf
from paper2html.ingestion._client import RateLimitedClient, _reset_singleton
from paper2html.ingestion._figure_fetcher import build_figures
from paper2html.ingestion._html_parser import parse_sections
from paper2html.ingestion._limits import IngestionLimits, LIMITS
from paper2html.ingestion._metadata import fetch_metadata
from paper2html.ingestion._types import FigureRef
from paper2html.ingestion.arxiv import _download_pdf, ingest_arxiv
from paper2html.models.document import from_arxiv_html

ATOM = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/abs/2301.07041v1</id><title>Safe paper</title><author><name>Alice</name></author><summary>Abstract</summary></entry></feed>'


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    monkeypatch.setattr(_client, "_RATE_LIMIT_SECONDS", 0)


@pytest.fixture
def png():
    stream = BytesIO()
    Image.new("RGB", (4, 3), "red").save(stream, format="PNG")
    return stream.getvalue()


def install(handler):
    client = RateLimitedClient(
        httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    )
    _reset_singleton(client)
    return client


@pytest.mark.parametrize(
    "url",
    [
        "http://arxiv.org/a",
        "https://evil.test/a",
        "https://arxiv.org.evil.test/a",
        "https://arxiv.org@127.0.0.1/a",
        "https://user@arxiv.org/a",
        "https://arxiv.org:444/a",
        "https://127.0.0.1/a",
        "file:///tmp/a",
        "https://arxiv.org\\@evil.test/a",
        "https://arxiv.org:bad/a",
    ],
)
def test_unsafe_url_never_connects(url):
    handler = Mock()
    client = install(handler)
    with pytest.raises(ValueError):
        client.get(url)
    handler.assert_not_called()


def test_redirect_validated_before_target_connection():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://127.0.0.1/private"})

    with pytest.raises(ValueError):
        install(handler).get("https://arxiv.org/start")
    assert calls == ["https://arxiv.org/start"]


@pytest.mark.parametrize(
    "base,src", [("", "https://evil.test/img"), ("https://evil.test/", "img")]
)
def test_hostile_src_or_base_skips_without_connection(base, src, caplog):
    handler = Mock()
    install(handler)
    pairs, _ = parse_sections(
        f'<base href="{base}"><section><figure><img src="{src}"></figure></section>',
        "https://arxiv.org/html/1234",
    )
    assert build_figures(pairs[0][1]) == []
    handler.assert_not_called()
    assert "Unsafe ingestion URL" in caplog.text


class Chunks(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.reads = 0
        self.closed = False

    def __iter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk

    def close(self):
        self.closed = True


@pytest.mark.parametrize("declared,expected_reads", [("100", 0), (None, 2), ("1", 2)])
def test_stream_body_limit_early_and_actual(declared, expected_reads):
    stream = Chunks([b"1234", b"5678", b"9"])
    headers = {"content-length": declared} if declared else {}
    client = install(lambda r: httpx.Response(200, headers=headers, stream=stream))
    with pytest.raises(ValueError, match="byte limit"):
        client.get("https://arxiv.org/a", max_bytes=6)
    assert stream.reads == expected_reads
    assert stream.closed


def test_unsupported_compression_rejected_before_decode():
    stream = Chunks([b"not gzip"])
    client = install(
        lambda r: httpx.Response(200, headers={"content-encoding": "br"}, stream=stream)
    )
    with pytest.raises(ValueError, match="Unsupported"):
        client.get("https://arxiv.org/a")
    assert stream.reads == 0


def test_metadata_redirects_all_limited_and_finite(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.extensions["timeout"]["read"] <= LIMITS.request_seconds
        if len(calls) < 3:
            return httpx.Response(302, headers={"location": "/next"})
        return httpx.Response(200, content=ATOM)

    client = install(handler)
    throttle = Mock()
    monkeypatch.setattr(client, "_throttle", throttle)
    assert fetch_metadata("2301.07041").authors == ["Alice"]
    assert throttle.call_count == len(calls) == 3
    calls.clear()
    monkeypatch.setattr(
        client._client,
        "_transport",
        httpx.MockTransport(
            lambda r: httpx.Response(302, headers={"location": "/loop"})
        ),
    )
    with pytest.raises(ValueError, match="redirect limit"):
        fetch_metadata("2301.07041")
    assert throttle.call_count == 3 + LIMITS.redirects + 1


def test_metadata_error_no_hidden_retries(monkeypatch):
    handler = Mock(side_effect=httpx.ReadTimeout("metadata timed out"))
    client = install(handler)
    throttle = Mock()
    monkeypatch.setattr(client, "_throttle", throttle)
    with pytest.raises(httpx.ReadTimeout):
        fetch_metadata("2301.07041")
    assert handler.call_count == throttle.call_count == 1
    assert client._last_request_time > 0


def test_deadline_during_stream_and_before_redirect(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(_client, "monotonic", lambda: clock[0])

    class Slow(Chunks):
        def __iter__(self):
            yield b"a"
            clock[0] += LIMITS.total_seconds + 1
            yield b"b"

    stream = Slow([])
    client = install(lambda r: httpx.Response(200, stream=stream))
    with pytest.raises(TimeoutError, match="deadline"):
        client.get("https://arxiv.org/a")
    assert stream.closed

    def redirect(request):
        clock[0] += LIMITS.total_seconds + 1
        return httpx.Response(302, headers={"location": "/next"})

    handler = Mock(side_effect=redirect)
    with pytest.raises(TimeoutError):
        install(handler).get("https://arxiv.org/a")
    assert handler.call_count == 1


def test_metadata_xml_entity_forbidden():
    handler = Mock(
        return_value=httpx.Response(
            200,
            content=b'<!DOCTYPE feed [<!ENTITY x SYSTEM "https://evil.test/x">]><feed>&x;</feed>',
        )
    )
    install(handler)
    with pytest.raises(Exception, match="DTDForbidden"):
        fetch_metadata("2301.07041")
    assert handler.call_count == 1


def test_data_uri_valid_invalid_and_oversized(png, monkeypatch, caplog):
    handler = Mock()
    install(handler)
    uri = "data:image/png;base64," + base64.b64encode(png).decode()
    assert len(build_figures([FigureRef("ok", src_url=uri)])) == 1
    for bad in [
        "data:image/png;base64,!!",
        "data:image/svg+xml;base64,PHN2Zy8+",
        "data:image/png,raw",
        uri.replace("png", "jpeg"),
    ]:
        assert build_figures([FigureRef("bad", src_url=bad)]) == []
    from paper2html.ingestion import _figure_fetcher

    monkeypatch.setattr(_figure_fetcher, "LIMITS", replace(LIMITS, image_bytes=8))
    assert build_figures([FigureRef("large", src_url=uri)]) == []
    assert "oversized" in caplog.text.lower()
    handler.assert_not_called()


def test_timeout_asset_does_not_lose_paper_and_nested_dedup(tmp_path, png, caplog):
    html = b'<section><h1>A</h1><div><section><h2>B</h2><figure><figure><img src="/ok"></figure></figure><figure><img src="/ok"></figure><figure><img src="/timeout"></figure></section></div></section>'
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.host == "export.arxiv.org":
            return httpx.Response(200, content=ATOM)
        if request.url.path.startswith("/html/"):
            return httpx.Response(200, content=html)
        if request.url.path == "/timeout":
            raise httpx.ReadTimeout("asset timeout")
        return httpx.Response(200, content=png)

    install(handler)
    result = ingest_arxiv("2301.07041", tmp_path)
    assert result.sections[0].figures == []
    assert len(result.sections[1].figures) == 2
    assert calls.count("/ok") == calls.count("/timeout") == 1
    assert "asset timeout" in caplog.text


@pytest.mark.parametrize("sectionless", [False, True])
def test_wrapped_and_sectionless_attachments(sectionless):
    content = '<p>Preserved prose.</p><math id="m"><mi>x</mi></math><figure><img src="a"><figcaption>Picture</figcaption></figure><table class="ltx_tabular"><tr><td>Cell</td></tr></table>'
    html = (
        f"<body>{content}</body>"
        if sectionless
        else f"<section><h1>Parent</h1><div><section><h2>Child</h2>{content}</section></div></section>"
    )
    doc = from_arxiv_html(
        BeautifulSoup(html, "html.parser"), SimpleNamespace(title="Paper", authors=[])
    )
    owner = doc.sections[-1]
    assert "Preserved prose" in owner.text_content
    assert owner.equation_ids and owner.figure_ids and owner.tables
    if not sectionless:
        assert owner.parent_id == doc.sections[0].id
        assert doc.sections[0].subsection_ids == [owner.id]
        assert "Preserved prose" not in doc.sections[0].text_content


def make_pdf(path, png, width=100, pages=1, encrypted=False):
    with pymupdf.open() as doc:
        for _ in range(pages):
            page = doc.new_page(width=width, height=100)
            page.insert_image(pymupdf.Rect(5, 5, 25, 25), stream=png)
        kwargs = (
            dict(
                encryption=pymupdf.PDF_ENCRYPT_AES_256,
                owner_pw="owner",
                user_pw="secret",
            )
            if encrypted
            else {}
        )
        doc.save(path, **kwargs)


def test_pdf_content_identity_reuse_and_corrupt_regeneration(tmp_path, png):
    source, cache = tmp_path / "source.pdf", tmp_path / "cache"
    make_pdf(source, png)
    first = pdf.ingest_pdf(source, cache)
    all_paths = first.page_image_paths + [f.path for f in first.figure_image_paths]
    times = [p.stat().st_mtime_ns for p in all_paths]
    second = pdf.ingest_pdf(source, cache)
    assert first == second
    assert times == [p.stat().st_mtime_ns for p in all_paths]
    for p in all_paths:
        p.write_bytes(b"corrupted")
    regenerated = pdf.ingest_pdf(source, cache)
    assert regenerated == first
    for p in all_paths:
        assert _safety.cached_image(p)
    source.unlink()
    out = BytesIO()
    Image.new("RGB", (8, 6), "blue").save(out, format="PNG")
    make_pdf(source, out.getvalue(), width=180)
    changed = pdf.ingest_pdf(source, cache)
    assert changed.page_image_paths != first.page_image_paths
    assert changed.figure_image_paths[0].path != first.figure_image_paths[0].path
    with (
        Image.open(first.page_image_paths[0]) as a,
        Image.open(changed.page_image_paths[0]) as b,
    ):
        assert a.width != b.width
    with (
        Image.open(first.figure_image_paths[0].path) as a,
        Image.open(changed.figure_image_paths[0].path) as b,
    ):
        assert a.size != b.size
    assert all("/ingestion/pdf/" in str(p) for p in all_paths)


@pytest.mark.parametrize("failure", ["encrypted", "write", "raster"])
def test_pdf_document_closed_on_failure(tmp_path, png, monkeypatch, failure):
    source = tmp_path / "source.pdf"
    make_pdf(source, png, encrypted=failure == "encrypted")
    opened = []
    original = pymupdf.open

    def tracking(*args, **kwargs):
        doc = original(*args, **kwargs)
        opened.append(doc)
        return doc

    monkeypatch.setattr(pymupdf, "open", tracking)
    if failure == "write":
        monkeypatch.setattr(
            pdf, "_atomic_write", Mock(side_effect=OSError("disk full"))
        )
    if failure == "raster":
        monkeypatch.setattr(
            pymupdf.Page, "get_pixmap", Mock(side_effect=RuntimeError("render failed"))
        )
    with pytest.raises((ValueError, OSError, RuntimeError)):
        pdf.ingest_pdf(source, tmp_path / "cache")
    assert opened and all(doc.is_closed for doc in opened)
    assert not list((tmp_path / "cache").rglob("*.tmp"))
    assert not list((tmp_path / "cache").rglob("*.pdf"))


@pytest.mark.parametrize(
    "limit,kwargs", [("source_bytes", {}), ("pages", {"pages": 2}), ("pixels", {})]
)
def test_pdf_resource_limits_before_raster(tmp_path, png, monkeypatch, limit, kwargs):
    source = tmp_path / "source.pdf"
    make_pdf(source, png, **kwargs)
    monkeypatch.setattr(pdf, "LIMITS", replace(LIMITS, **{limit: 1}))
    render = Mock(side_effect=AssertionError("must not rasterize"))
    monkeypatch.setattr(pymupdf.Page, "get_pixmap", render)
    with pytest.raises(ValueError, match="limit"):
        pdf.ingest_pdf(source, tmp_path / "cache")
    render.assert_not_called()


@pytest.mark.parametrize("where", ["ingestion", "pages", "image", "downloads"])
def test_symlink_cache_rejected(tmp_path, png, where):
    source, cache, outside = (
        tmp_path / "source.pdf",
        tmp_path / "cache",
        tmp_path / "outside",
    )
    make_pdf(source, png)
    outside.mkdir()
    cache.mkdir()
    if where == "ingestion":
        (cache / "ingestion").symlink_to(outside, target_is_directory=True)
    elif where == "downloads":
        (cache / "ingestion").mkdir()
        (cache / "ingestion" / "downloads").symlink_to(
            outside, target_is_directory=True
        )
        with pytest.raises(ValueError, match="Symlink"):
            _download_pdf("2301.07041", cache)
        assert not list(outside.iterdir())
        return
    else:
        first = pdf.ingest_pdf(source, cache)
        page = first.page_image_paths[0]
        page.unlink()
        if where == "pages":
            page.parent.rmdir()
            page.parent.symlink_to(outside, target_is_directory=True)
        else:
            page.symlink_to(outside / "stolen.jpg")
    with pytest.raises(ValueError, match="Symlink"):
        pdf.ingest_pdf(source, cache)
    assert not list(outside.iterdir())


def test_corrupt_download_regenerated_and_valid_reused(tmp_path, png):
    source = tmp_path / "source.pdf"
    make_pdf(source, png)
    calls = Mock(return_value=httpx.Response(200, content=source.read_bytes()))
    install(calls)
    path = _download_pdf("2301.07041", tmp_path / "cache")
    assert _download_pdf("2301.07041", tmp_path / "cache") == path
    assert calls.call_count == 1
    path.write_bytes(b"garbage")
    assert _download_pdf("2301.07041", tmp_path / "cache") == path
    assert calls.call_count == 2
    pdf.validate_pdf(path)


@pytest.mark.parametrize(
    "payload",
    [
        b'<svg width="1000000" height="1000000"/>',
        b'<svg width="1000000" height="1"/>',
        b'<svg width="10" height="10"><rect style="fill:u\\72l(https://evil.test/a)"/></svg>',
        b'<svg width="10" height="10"><image href="https://evil.test/a"/></svg>',
        b'<svg width="10" height="10"><script>alert(1)</script></svg>',
        b'<svg width="10" height="10"><rect fill="url(https://evil.test/a)"/></svg>',
        b'<!DOCTYPE svg [<!ENTITY e SYSTEM "file:///etc/passwd">]><svg width="10" height="10">&e;</svg>',
    ],
)
def test_svg_guards_before_conversion(payload, monkeypatch):
    converter = Mock(side_effect=AssertionError("must not convert"))
    from paper2html.ingestion import _svg

    monkeypatch.setattr(_svg, "_convert", converter)
    with pytest.raises(Exception):
        _safety.svg_png(payload)
    converter.assert_not_called()


def test_atomic_write_cleans_temp_on_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(
        _safety.os, "replace", Mock(side_effect=OSError("disk failure"))
    )
    with pytest.raises(OSError):
        _safety.atomic_write(b"content", tmp_path / "image.png")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"pixels": 0},
        {"pages": -1},
        {"pages": 1.5},
        {"request_seconds": float("inf")},
        {"body_bytes": True},
    ],
)
def test_limits_validated(kwargs):
    with pytest.raises(ValueError):
        IngestionLimits(**kwargs)


def test_pdf_normal_path_closes_and_source_mutation_rejected(
    tmp_path, png, monkeypatch
):
    source = tmp_path / "source.pdf"
    make_pdf(source, png)
    opened = []
    original = pymupdf.open

    def tracking(*args, **kwargs):
        doc = original(*args, **kwargs)
        opened.append(doc)
        return doc

    monkeypatch.setattr(pymupdf, "open", tracking)
    pdf.ingest_pdf(source, tmp_path / "cache")
    assert opened and all(doc.is_closed for doc in opened)
    # Simulate a replacement during snapshot without large or timing-sensitive I/O.
    stat = Path.stat
    count = [0]

    def changed(path, *args, **kwargs):
        result = stat(path, *args, **kwargs)
        if path == source:
            count[0] += 1
            return SimpleNamespace(
                st_ino=result.st_ino + 1, st_mtime_ns=result.st_mtime_ns
            )
        return result

    monkeypatch.setattr(Path, "stat", changed)
    with pytest.raises(ValueError, match="source changed"):
        pdf.ingest_pdf(source, tmp_path / "cache")
    assert count[0] == 1
    assert not list((tmp_path / "cache").rglob("*.pdf"))


@pytest.mark.parametrize("limit", ["pixels", "image_bytes"])
def test_extracted_image_budget_skips_with_warning(tmp_path, png, monkeypatch, limit):
    source = tmp_path / "source.pdf"
    make_pdf(source, png)
    monkeypatch.setattr(pdf, "LIMITS", replace(LIMITS, **{limit: 1}))
    warnings = []
    with pymupdf.open(source) as doc:
        assert pdf._extract_figures(doc, tmp_path / "figures", warnings) == []
    assert any("limit exceeded" in w for w in warnings)
    assert not (tmp_path / "figures").exists()


def test_raster_pixel_guard_before_decode(png, monkeypatch):
    monkeypatch.setattr(_safety, "LIMITS", replace(LIMITS, pixels=1))
    with pytest.raises(ValueError, match="pixel limit"):
        _safety.raster(png)


def test_svg_conversion_unique_and_explicit_dimensions(png, monkeypatch):
    converter = Mock(return_value=png)
    from paper2html.ingestion import _svg

    monkeypatch.setattr(_svg, "_convert", converter)
    svg = b'<svg width="4" height="3"><rect width="1" height="1"/></svg>'
    cache = {}
    refs = [FigureRef("first", inline_svg=svg), FigureRef("second", inline_svg=svg)]
    assert len(build_figures(refs, cache)) == 2
    assert len(build_figures(refs, cache)) == 2
    assert converter.call_count == 1
    converter.assert_called_once_with(svg, 4, 3)


def test_metadata_body_limit(monkeypatch):
    monkeypatch.setattr(_client, "LIMITS", replace(LIMITS, body_bytes=8))
    handler = Mock(return_value=httpx.Response(200, content=ATOM))
    install(handler)
    with pytest.raises(ValueError, match="byte limit"):
        fetch_metadata("2301.07041")
    assert handler.call_count == 1


def test_invalid_download_not_published(tmp_path):
    install(lambda r: httpx.Response(200, content=b"not a PDF"))
    with pytest.raises((ValueError, RuntimeError)):
        _download_pdf("2301.07041", tmp_path)
    assert not list(tmp_path.rglob("*.pdf"))
    assert not list(tmp_path.rglob("*.tmp"))
