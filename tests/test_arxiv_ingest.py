"""Tests for the arXiv ingestion module (ticket #003).

HTTP mocking strategy: httpx.MockTransport injected into RateLimitedClient
via _reset_singleton(client).  This avoids respx class-level patching
complexity and gives us full control over per-test transports.

The autouse reset_rate_limiter_singleton fixture in conftest.py ensures
each test starts with a fresh singleton.
"""

from __future__ import annotations

import inspect
from io import BytesIO
from PIL import Image
import pymupdf
import time
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from paper2html.ingestion import (
    ArxivIngestResult,
    PdfFallbackResult,
    ingest_arxiv,
)
from paper2html.ingestion._client import RateLimitedClient, _reset_singleton
from paper2html.ingestion._metadata import normalise_id

# ---------------------------------------------------------------------------
# Tiny valid raster/PDF and Atom transport fixtures
# ---------------------------------------------------------------------------
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_buffer = BytesIO()
Image.new("RGB", (3, 3)).save(_buffer, format="PNG")
_FAKE_PNG = _buffer.getvalue()
with pymupdf.open() as _doc:
    _doc.new_page()
    _FAKE_PDF = _doc.tobytes()
_ATOM = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/abs/2301.07041v1</id><title>Test Paper Title</title><summary>Test abstract text.</summary><author><name>Alice Researcher</name></author><category term="cs.LG"/><published>2023-01-17</published><updated>2023-01-20</updated></entry></feed>'
_METADATA_CALLS = []

# ---------------------------------------------------------------------------
# HTML fixtures
# ---------------------------------------------------------------------------
_SIMPLE_HTML = """\
<!DOCTYPE html>
<html>
<head><title>Test Paper</title></head>
<body>
<section>
  <h2>Introduction</h2>
  <p>Some text.</p>
  <figure>
    <img src="2301.07041v1/figures/fig1.png" alt="Figure 1"/>
    <figcaption>Figure 1 caption</figcaption>
  </figure>
  <figure>
    <img src="2301.07041v1/figures/fig2.png" alt="Figure 2"/>
    <figcaption>Figure 2 caption</figcaption>
  </figure>
</section>
</body>
</html>
"""

_SVG_HTML = """\
<!DOCTYPE html>
<html>
<body>
<section>
  <h2>Results</h2>
  <figure>
    <img src="2301.07041v1/figures/svg_fig.svg" alt="SVG Figure"/>
    <figcaption>SVG figure caption</figcaption>
  </figure>
  <figure>
    <svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
      <circle cx="50" cy="50" r="40"/>
    </svg>
    <figcaption>Inline SVG caption</figcaption>
  </figure>
</section>
</body>
</html>
"""

_HTML_BASE_URL = "https://arxiv.org/html/2301.07041"


# ---------------------------------------------------------------------------
# Transport helpers
# ---------------------------------------------------------------------------
def _build_transport(routes: dict[str, tuple[int, bytes]]) -> httpx.MockTransport:
    """Build an httpx.MockTransport from a URL → (status, body) dict.

    Exact match wins; then the longest prefix match is used to avoid
    the base page URL shadowing deeper asset URLs.
    """
    # Sort by length descending so more-specific URLs match first.
    sorted_routes = sorted(routes.items(), key=lambda kv: len(kv[0]), reverse=True)

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if request.url.host == "export.arxiv.org":
            _METADATA_CALLS.append(request)
            atom = _ATOM.replace(
                b"2301.07041v1", (request.url.params["id_list"] + "v1").encode()
            )
            return httpx.Response(200, content=atom, request=request)
        for prefix, (status, body) in sorted_routes:
            if url_str == prefix or url_str.startswith(prefix):
                return httpx.Response(status, content=body, request=request)
        return httpx.Response(404, content=b"not found", request=request)

    return httpx.MockTransport(handler)


def _install_client(routes: dict[str, tuple[int, bytes]]) -> RateLimitedClient:
    """Create a RateLimitedClient with mocked HTTP and install as singleton."""
    _METADATA_CALLS.clear()
    transport = _build_transport(routes)
    http_client = httpx.Client(transport=transport, follow_redirects=True, timeout=30.0)
    client = RateLimitedClient(http_client=http_client)
    # Reset last_request_time so no initial wait
    client._last_request_time = 0.0
    _reset_singleton(client)
    return client


# ---------------------------------------------------------------------------
# AC-1: metadata fields
# ---------------------------------------------------------------------------
def test_metadata_fields(tmp_path: Path) -> None:
    """AC-1: metadata is non-empty and correctly mapped."""
    _install_client(
        {
            "https://arxiv.org/html/2301.07041": (404, b"not found"),
            "https://arxiv.org/pdf/2301.07041": (200, _FAKE_PDF),
        }
    )

    result = ingest_arxiv("2301.07041", tmp_path)

    assert isinstance(result, PdfFallbackResult)
    meta = result.metadata
    assert meta.arxiv_id == "2301.07041"
    assert len(meta.title) > 0
    assert len(meta.authors) >= 1
    assert all(isinstance(a, str) for a in meta.authors)
    assert len(meta.abstract) > 0
    assert len(meta.categories) >= 1
    assert all(isinstance(c, str) for c in meta.categories)


# ---------------------------------------------------------------------------
# Regression: browser-faithful figure URL resolution (real arXiv structure)
# ---------------------------------------------------------------------------
def test_figure_src_resolves_against_doc_url_not_nested() -> None:
    """arXiv serves the doc at the unversioned, non-slash path with
    version-prefixed *sibling* srcs. RFC 3986 resolution must drop the doc's
    last path segment — a forced trailing slash mis-resolved to a nested
    (HTTP 404) path. Reproduces the 1706.03762 figure-loss bug.
    """
    from paper2html.ingestion._html_parser import parse_sections

    html = (
        "<html><body><section><h2>Intro</h2>"
        '<figure><img src="1706.03762v7/Figures/x.png"/>'
        "<figcaption>Fig</figcaption></figure>"
        "</section></body></html>"
    )
    section_tuples, _ = parse_sections(html, "https://arxiv.org/html/1706.03762")

    _, fig_refs = section_tuples[0]
    assert fig_refs[0].src_url == ("https://arxiv.org/html/1706.03762v7/Figures/x.png")


def test_figure_src_honors_base_href() -> None:
    """A <base href> overrides the document URL for relative resolution."""
    from paper2html.ingestion._html_parser import parse_sections

    html = (
        '<html><head><base href="https://arxiv.org/html/2401.00001v2/"></head>'
        "<body><section><h2>Intro</h2>"
        '<figure><img src="figures/y.png"/><figcaption>Fig</figcaption></figure>'
        "</section></body></html>"
    )
    section_tuples, _ = parse_sections(html, "https://arxiv.org/html/2401.00001")

    _, fig_refs = section_tuples[0]
    assert fig_refs[0].src_url == ("https://arxiv.org/html/2401.00001v2/figures/y.png")


# ---------------------------------------------------------------------------
# AC-2: HTML happy path — sections and figures
# ---------------------------------------------------------------------------
def test_html_happy_path(tmp_path: Path) -> None:
    """AC-2: ArxivIngestResult with ≥1 section, heading populated, 2 figures."""
    _install_client(
        {
            "https://arxiv.org/html/2301.07041": (200, _SIMPLE_HTML.encode()),
            "https://arxiv.org/html/2301.07041v1/figures/fig1.png": (200, _FAKE_PNG),
            "https://arxiv.org/html/2301.07041v1/figures/fig2.png": (200, _FAKE_PNG),
        }
    )

    result = ingest_arxiv("2301.07041", tmp_path)

    assert isinstance(result, ArxivIngestResult)
    assert len(result.sections) >= 1

    intro = result.sections[0]
    assert intro.heading_text == "Introduction"
    assert intro.heading_level == 2
    assert len(intro.figures) == 2
    assert all(f.caption for f in intro.figures)


# ---------------------------------------------------------------------------
# AC-3 / AC-4: PDF fallback on 404
# ---------------------------------------------------------------------------
def test_pdf_fallback_on_404(tmp_path: Path) -> None:
    """AC-3/AC-4: 404 on HTML → PdfFallbackResult, pdf_path exists with .pdf suffix."""
    _install_client(
        {
            "https://arxiv.org/html/2301.07041": (404, b"not found"),
            "https://arxiv.org/pdf/2301.07041": (200, _FAKE_PDF),
        }
    )

    result = ingest_arxiv("2301.07041", tmp_path)

    assert isinstance(result, PdfFallbackResult)
    assert result.pdf_path.suffix == ".pdf"
    assert result.pdf_path.exists()
    assert result.pdf_path.stat().st_size > 0
    # File must be at the expected location
    expected = tmp_path / "ingestion" / "downloads" / "2301.07041.pdf"
    assert result.pdf_path == expected


# ---------------------------------------------------------------------------
# AC-5: SVG figures rasterised to PNG
# ---------------------------------------------------------------------------
def test_svg_figures_rasterized(tmp_path: Path) -> None:
    """AC-5: SVG assets (URL and inline) are rasterised to PNG bytes."""
    _install_client(
        {
            "https://arxiv.org/html/2301.07041": (200, _SVG_HTML.encode()),
            # The .svg asset served as SVG content
            "https://arxiv.org/html/2301.07041v1/figures/svg_fig.svg": (
                200,
                b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><rect/></svg>',
            ),
        }
    )

    with (
        patch("paper2html.ingestion._svg._convert", return_value=_FAKE_PNG),
    ):
        result = ingest_arxiv("2301.07041", tmp_path)

    assert isinstance(result, ArxivIngestResult)
    figs = result.figures  # top-level property
    assert len(figs) >= 1
    for fig in figs:
        assert fig.asset_bytes[:4] == b"\x89PNG", f"Not PNG: {fig.asset_bytes[:8]!r}"
        assert not fig.asset_bytes[:4].startswith(b"<svg")
        assert not fig.asset_bytes[:5].startswith(b"<?xml")


def test_inline_svg_captured_and_rasterized(tmp_path: Path) -> None:
    """AC-5 (inline SVG variant): inline <svg> figures are rasterised to PNG."""
    # HTML with only an inline SVG figure (no img src)
    html_inline = """\
    <!DOCTYPE html><html><body>
    <section>
      <h2>Methods</h2>
      <figure>
        <svg xmlns="http://www.w3.org/2000/svg" width="50" height="50">
          <circle cx="25" cy="25" r="20"/>
        </svg>
        <figcaption>Inline only</figcaption>
      </figure>
    </section>
    </body></html>
    """
    _install_client(
        {
            "https://arxiv.org/html/2301.07041": (200, html_inline.encode()),
        }
    )

    with (
        patch("paper2html.ingestion._svg._convert", return_value=_FAKE_PNG),
    ):
        result = ingest_arxiv("2301.07041", tmp_path)

    assert isinstance(result, ArxivIngestResult)
    figs = result.figures
    assert len(figs) == 1
    assert figs[0].asset_bytes[:4] == b"\x89PNG"


# ---------------------------------------------------------------------------
# AC-6: rate limiting (real sleep, mocked HTTP)
# ---------------------------------------------------------------------------
def test_rate_limit(tmp_path: Path) -> None:
    """AC-6: 3 sequential HTTP calls must take ≥6.0 s total."""
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, content=_FAKE_PNG, request=request)

    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=transport, follow_redirects=True, timeout=30.0)
    client = RateLimitedClient(http_client=http_client)
    client._last_request_time = 0.0

    start = time.monotonic()
    client.get("https://arxiv.org/a")
    client.get("https://arxiv.org/b")
    client.get("https://arxiv.org/c")
    elapsed = time.monotonic() - start

    assert elapsed >= 6.0, f"Expected ≥6.0 s, got {elapsed:.2f} s"
    assert call_count == 3


# ---------------------------------------------------------------------------
# AC-7: no concurrency primitives
# ---------------------------------------------------------------------------
def test_no_concurrency_primitives() -> None:
    """AC-7: source of the 5 core modules contains no async/asyncio/ThreadPoolExecutor."""
    import paper2html.ingestion.arxiv as arxiv_mod
    import paper2html.ingestion._client as client_mod
    import paper2html.ingestion._metadata as metadata_mod
    import paper2html.ingestion._html_parser as html_mod
    import paper2html.ingestion._figure_fetcher as fig_mod

    combined = "".join(
        inspect.getsource(m)
        for m in [arxiv_mod, client_mod, metadata_mod, html_mod, fig_mod]
    )
    assert "ThreadPoolExecutor" not in combined
    assert "async def" not in combined
    assert "asyncio" not in combined


# ---------------------------------------------------------------------------
# AC-8: URL input normalised, metadata called with base ID
# ---------------------------------------------------------------------------
def test_url_input_normalised(tmp_path: Path) -> None:
    """AC-8: versioned URL → metadata fetched exactly once with base ID."""
    _install_client(
        {
            "https://arxiv.org/html/2301.07041": (404, b"not found"),
            "https://arxiv.org/pdf/2301.07041": (200, _FAKE_PDF),
        }
    )

    result = ingest_arxiv("https://arxiv.org/abs/2301.07041v2", tmp_path)

    assert isinstance(result, PdfFallbackResult)
    assert len(_METADATA_CALLS) == 1
    assert _METADATA_CALLS[0].url.params["id_list"] == "2301.07041"


# ---------------------------------------------------------------------------
# test_normalise_id parametrized
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2301.07041", "2301.07041"),
        ("2301.07041v2", "2301.07041"),
        ("https://arxiv.org/abs/2301.07041v2", "2301.07041"),
        ("https://arxiv.org/pdf/2301.07041", "2301.07041"),
        ("math/0211159", "math/0211159"),
        ("https://arxiv.org/abs/hep-th/9901001v3", "hep-th/9901001"),
    ],
)
def test_normalise_id(raw: str, expected: str) -> None:
    assert normalise_id(raw) == expected


def test_normalise_id_invalid() -> None:
    with pytest.raises(ValueError):
        normalise_id("not-an-id")


# ---------------------------------------------------------------------------
# AC-9: no write outside cache root
# ---------------------------------------------------------------------------
def test_no_write_outside_cache_root(tmp_path: Path) -> None:
    """AC-9: only files under tmp_path are written during PDF fallback."""
    _install_client(
        {
            "https://arxiv.org/html/2301.07041": (404, b"not found"),
            "https://arxiv.org/pdf/2301.07041": (200, _FAKE_PDF),
        }
    )

    result = ingest_arxiv("2301.07041", tmp_path)

    assert isinstance(result, PdfFallbackResult)
    pdf = result.pdf_path
    assert pdf.is_relative_to(tmp_path)

    # Walk everything under tmp_path to confirm nothing escaped
    all_files = list(tmp_path.rglob("*"))
    for f in all_files:
        assert f.is_relative_to(tmp_path)


def test_old_style_id_safe_filename(tmp_path: Path) -> None:
    """AC-9: old-style ID math/0211159 → math_0211159.pdf inside cache root."""
    _install_client(
        {
            "https://arxiv.org/html/math/0211159": (404, b"not found"),
            "https://arxiv.org/pdf/math/0211159": (200, _FAKE_PDF),
        }
    )

    result = ingest_arxiv("math/0211159", tmp_path)

    assert isinstance(result, PdfFallbackResult)
    assert result.pdf_path == tmp_path / "ingestion" / "downloads" / "math_0211159.pdf"
    assert result.pdf_path.is_relative_to(tmp_path)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------
def test_zero_figures_section(tmp_path: Path) -> None:
    """Section with no figures returns figures==[]."""
    html_no_figs = """\
    <!DOCTYPE html><html><body>
    <section><h1>Abstract</h1><p>Text only, no figures.</p></section>
    </body></html>
    """
    _install_client(
        {
            "https://arxiv.org/html/2301.07041": (200, html_no_figs.encode()),
        }
    )

    result = ingest_arxiv("2301.07041", tmp_path)

    assert isinstance(result, ArxivIngestResult)
    assert result.sections[0].figures == []
    assert result.figures == []


def test_non_200_asset_skipped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Non-200 figure asset response → figure skipped, no exception."""
    html = """\
    <!DOCTYPE html><html><body>
    <section>
      <h2>Results</h2>
      <figure>
        <img src="2301.07041v1/figures/forbidden.png"/>
        <figcaption>Forbidden figure</figcaption>
      </figure>
    </section>
    </body></html>
    """
    _install_client(
        {
            "https://arxiv.org/html/2301.07041": (200, html.encode()),
            "https://arxiv.org/html/2301.07041v1/figures/forbidden.png": (
                403,
                b"forbidden",
            ),
        }
    )

    import logging

    with (
        caplog.at_level(logging.WARNING),
    ):
        result = ingest_arxiv("2301.07041", tmp_path)

    assert isinstance(result, ArxivIngestResult)
    assert result.figures == []
    assert any("403" in r.message or "Skipping" in r.message for r in caplog.records)


def test_malformed_svg_warn_skip(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """svg2png raises → figure skipped, WARNING logged."""
    html = """\
    <!DOCTYPE html><html><body>
    <section>
      <h2>Broken</h2>
      <figure>
        <img src="2301.07041v1/figures/broken.svg"/>
        <figcaption>Broken SVG</figcaption>
      </figure>
    </section>
    </body></html>
    """
    _install_client(
        {
            "https://arxiv.org/html/2301.07041": (200, html.encode()),
            "https://arxiv.org/html/2301.07041v1/figures/broken.svg": (
                200,
                b'<svg width="10" height="10">not valid</svg>',
            ),
        }
    )

    import logging

    with (
        patch(
            "paper2html.ingestion._svg._convert",
            side_effect=Exception("Cairo parse error"),
        ),
        caplog.at_level(logging.WARNING),
    ):
        result = ingest_arxiv("2301.07041", tmp_path)

    assert isinstance(result, ArxivIngestResult)
    assert result.figures == []
    assert any(
        "Cairo" in r.message or "rasterise" in r.message.lower() for r in caplog.records
    )


# ---------------------------------------------------------------------------
# Integration tests (gated by PAPER2HTML_INTEGRATION_TESTS=1)
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_integration_html_paper(tmp_path: Path) -> None:
    """Live: 2301.07041 should have an HTML version with ≥1 section."""
    result = ingest_arxiv("2301.07041", tmp_path)
    assert isinstance(result, ArxivIngestResult)
    assert len(result.sections) >= 1
    assert result.metadata.title
    assert result.metadata.authors


@pytest.mark.integration
def test_integration_pdf_fallback(tmp_path: Path) -> None:
    """Live: 1001.0001 should fall back to PDF (skip if arXiv now has HTML)."""
    import httpx as _httpx

    check_response = _httpx.get(
        "https://arxiv.org/html/1001.0001", follow_redirects=True
    )
    if check_response.status_code == 200:
        pytest.skip("arXiv back-rendered 1001.0001 as HTML")

    result = ingest_arxiv("1001.0001", tmp_path)
    assert isinstance(result, PdfFallbackResult)
    assert result.pdf_path.exists()
    assert result.pdf_path.suffix == ".pdf"
