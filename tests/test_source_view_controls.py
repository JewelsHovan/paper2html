"""Reader-owned controls retain original image bytes without widening the page."""

import base64
from dataclasses import replace
from io import BytesIO

from bs4 import BeautifulSoup
from PIL import Image
import pytest

from paper2html.ingestion import _limits
from paper2html.renderer import render_html
from paper2html.review.schemas import AnnotationBundle
from paper2html.source_view import asset_key, sanitize_article
from tests.test_renderer import make_arxiv_doc
from tests.test_browser import browser_page as _offline_browser_page

browser_page = _offline_browser_page


def pixels(width=10, height=8):
    output = BytesIO()
    Image.new("RGB", (width, height), "white").save(output, "PNG")
    return output.getvalue()


def test_original_nested_raster_download_is_exact_inert_and_separate():
    source = '<article><svg width="40" height="20"><foreignobject><span><img src="data.png"/></span></foreignobject></svg></article>'
    parsed = BeautifulSoup(source, "html.parser")
    raw = pixels(30, 20)
    assets = {asset_key(parsed.svg): pixels(40, 20), "data.png": raw}
    out = BeautifulSoup(str(sanitize_article(source, assets)), "html.parser")
    assert len(out.find_all("img")) == 1
    link = out.select_one(".source-image-download")
    assert base64.b64decode(link["href"].split(",", 1)[1]) == raw
    assert link["download"].endswith(".png")
    assert link.get_text() == "" and not out.find(["script", "svg", "object"])
    image = out.find("img")
    assert image.parent["href"] == "#" + image["id"]


def test_actual_emitted_raster_occurrences_have_a_document_budget(monkeypatch):
    monkeypatch.setattr(_limits, "LIMITS", replace(_limits.LIMITS, source_bytes=1500))
    with pytest.raises(ValueError, match="emitted image byte budget"):
        sanitize_article(
            "<article>" + '<img src="a"/>' * 20 + "</article>", {"a": pixels()}
        )


@pytest.mark.browser
def test_tables_inside_phrasing_wrappers_scroll_locally(tmp_path, browser_page):
    page, load = browser_page
    doc = make_arxiv_doc()
    doc.original_html = (
        '<article><figure><span class="ltx_inline-block"><table><tr><td>'
        + "WIDE" * 120
        + "</td></tr></table></span><figcaption>Wide table</figcaption></figure></article>"
    )
    out = render_html(doc, AnnotationBundle(items=[]), tmp_path / "nested.html")
    load(out)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    region = page.locator(".source-table-scroll")
    assert region.evaluate("e => e.scrollWidth > e.clientWidth")
    assert region.get_attribute("tabindex") == "0"


@pytest.mark.browser
def test_source_image_original_size_zoom_is_locally_scrollable(tmp_path, browser_page):
    page, load = browser_page
    doc = make_arxiv_doc()
    doc.original_html = (
        '<article><span class="ltx_inline-block"><img src="a"/></span></article>'
    )
    doc.source_assets = {"a": pixels(800, 100)}
    out = render_html(doc, AnnotationBundle(items=[]), tmp_path / "zoom.html")
    load(out)
    page.locator(".source-image-scroll a").click()
    assert page.locator(".source-image-scroll").evaluate(
        "e => e.scrollWidth > e.clientWidth"
    )
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
