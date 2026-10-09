"""Opt-in, genuinely offline Chromium acceptance; explicit opt-in never skips setup errors."""

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from paper2html.models.document import Equation
from paper2html.renderer import render_html
from tests.test_cli_process import _invoke, _make_fixture_pdf
from tests.test_renderer import make_arxiv_doc, make_full_bundle
from tests.test_render_security import adapt
from paper2html.review.schemas import AnnotationBundle

pytestmark = pytest.mark.browser


@pytest.fixture
def browser_page():
    if os.environ.get("PAPER2HTML_BROWSER_TESTS") != "1":
        pytest.skip("set PAPER2HTML_BROWSER_TESTS=1 to require the browser gate")
    # Deliberately not importorskip: an explicitly requested gate must fail closed.
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        options = {}
        override = os.environ.get("PAPER2HTML_CHROMIUM_EXECUTABLE")
        if override:
            options["executable_path"] = override
        elif not Path(playwright.chromium.executable_path).exists():
            cached = sorted(
                Path.home().glob(
                    "Library/Caches/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell"
                )
            )
            if cached:
                options["executable_path"] = str(cached[-1])
        browser = playwright.chromium.launch(**options)
        context = browser.new_context(
            offline=True,
            service_workers="block",
            viewport={"width": 390, "height": 844},
        )
        page = context.new_page()
        requests, errors, failures = [], [], []
        page.on("request", lambda request: requests.append(request.url))
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("requestfailed", lambda request: failures.append(request.url))
        allowed = []

        def load(path):
            uri = path.resolve().as_uri()
            allowed.append(uri)
            page.goto(uri, wait_until="load")
            page.evaluate("document.fonts.ready")
            return page

        yield page, load
        try:
            assert not errors, errors
            assert not failures, failures
            # Only the explicitly opened document plus embedded resources. In particular,
            # file siblings and HTTP are forbidden even if offline mode blocked the load.
            assert not [
                url
                for url in requests
                if url not in allowed and not url.startswith(("data:", "blob:"))
            ], requests
            assert page.evaluate(
                "Array.from(document.images).every(i => i.complete && i.naturalWidth > 0)"
            )
        finally:
            context.close()
            browser.close()


def test_wide_source_tables_scroll_without_widening_mobile(tmp_path, browser_page):
    from tests.test_source_fidelity import rich_doc

    doc = rich_doc()
    doc.original_html = doc.original_html.replace("Heading", "WideColumn" * 30)
    out = render_html(doc, AnnotationBundle(items=[]), tmp_path / "wide.html")
    page, load = browser_page
    load(out)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    region = page.locator(".source-table-scroll").first
    assert region.evaluate("e => e.scrollWidth > e.clientWidth")
    region.focus()
    page.keyboard.press("ArrowRight")
    assert region.evaluate("e => e === document.activeElement")
    assert page.locator(".source-table-scroll table").count() == 1


def test_real_pdf_cli_library_browser(tmp_path, browser_page):
    _, load = browser_page
    pdf = _make_fixture_pdf(tmp_path)
    out = tmp_path / "result.html"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from paper2html.cli import app; app()",
            "--cache-dir",
            str(tmp_path / "cache"),
            "--provider",
            "mock",
            "process",
            str(pdf),
            "-o",
            str(out),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert _invoke(tmp_path / "cache", "list").exit_code == 0
    with patch("paper2html.cli.webbrowser.open") as opened:
        result = _invoke(tmp_path / "cache", "open", "1")
        assert result.exit_code == 0, result.output
        opened.assert_called_once_with(out.as_uri())
    page = load(out)
    assert page.locator(".summary-block").count() > 0
    assert page.locator(".term-def").count() > 0


def test_rich_keyboard_hover_math_and_responsive(tmp_path, browser_page):
    from playwright.sync_api import expect

    page, load = browser_page
    doc = make_arxiv_doc()
    unusual = 'ref-"[quoted]'
    citation = doc.citations.pop("ref-1")
    citation.id = unusual
    doc.citations[unusual] = citation
    doc.equations["latex"] = Equation(id="latex", raw_latex="x^2", display_type="block")
    doc.sections[0].equation_ids.append("latex")
    out = render_html(doc, make_full_bundle(), tmp_path / "rich.html")
    load(out)
    button = page.locator(".summary-toggle").first
    block = page.locator(".summary-block").first
    for level in ("skim", "read", "deep", "skim"):
        expect(block).to_have_attribute("data-active-level", level)
        expect(button).to_have_attribute(
            "aria-label", f"Summary depth: {level}. Click to cycle."
        )
        expect(block.locator(f'[data-level="{level}"]')).to_be_visible()
        for other in {"skim", "read", "deep"} - {level}:
            expect(block.locator(f'[data-level="{other}"]')).not_to_be_visible()
        button.click()
    tooltip = page.get_by_role("tooltip")
    for target in (
        page.locator(".term-def").first,
        page.locator("cite[data-ref-id]").last,
    ):
        target.hover()
        expect(tooltip).to_be_visible()
        if target.get_attribute("data-ref-id"):
            expect(tooltip).to_contain_text("Smith et al.")
        page.keyboard.press("Escape")
        expect(tooltip).not_to_be_visible()
        page.mouse.move(0, 0)
        target.focus()
        expect(target).to_be_focused()
        expect(tooltip).to_be_visible()
        bounds = tooltip.bounding_box()
        assert bounds and bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= 390
        assert bounds["y"] >= 0 and bounds["y"] + bounds["height"] <= 844
        page.keyboard.press("Escape")
        expect(tooltip).not_to_be_visible()
        page.keyboard.press("Tab")
    summary = page.locator("details.proof > summary")
    summary.focus()
    page.keyboard.press("Enter")
    expect(page.locator("details.proof")).to_have_attribute("open", "")
    page.keyboard.press("Space")
    expect(page.locator("details.proof")).not_to_have_attribute("open", "")
    link = page.locator(".fig-backlinks a").first
    href = link.get_attribute("href")
    link.click()
    assert page.evaluate("location.hash") == href
    assert page.locator("math").count() >= 1
    assert page.locator(".katex").count() >= 1
    assert page.evaluate("Array.from(document.fonts).some(f => f.status === 'loaded')")


def test_malignant_table_only_math_is_inert(tmp_path, browser_page):
    page, load = browser_page
    doc = adapt("""<section id='x"[a]'><h2>Untrusted</h2><p>danger</p>
    <figure><table class="ltx_tabular"><tr><td><math alttext="x^2"><mi>x</mi>
    <annotation-xml encoding="text/html"><img src="https://evil.invalid/x" onerror="window.PWNED=1"></annotation-xml>
    <script>window.PWNED=1</script></math></td></tr></table></figure></section>""")
    doc.sections[0].tables.append("""<table onclick="window.PWNED=1"><tr><td>safe
    <img src="file:///tmp/private" onerror="window.PWNED=1"><iframe src="https://evil.invalid"></iframe>
    <script>window.PWNED=1</script></td></tr></table>""")
    doc.original_html = ""  # This gate exercises mutated normalized tables.
    doc.sections[0].id = 'section-"[bad]'
    bundle = AnnotationBundle(
        items=[],
        term_definitions={
            "danger": '<img src="https://evil.invalid" onerror="window.PWNED=1">'
        },
    )
    load(render_html(doc, bundle, tmp_path / "unsafe.html"))
    page.locator(".term-def").focus()
    assert page.get_by_role("tooltip").inner_text().startswith("<img")
    assert page.evaluate("window.PWNED === undefined")
    assert page.locator("math").count() > 0
    assert page.locator(".katex").count() > 0
    assert page.locator("table img, table iframe, table script").count() == 0


def test_original_source_math_objects_and_payloads(tmp_path, browser_page):
    from tests.test_source_fidelity import rich_doc, MALICIOUS

    page, load = browser_page
    doc = rich_doc()
    doc.original_html += MALICIOUS
    load(render_html(doc, AnnotationBundle(items=[]), tmp_path / "source.html"))
    assert page.evaluate("window.PWNED === undefined")
    assert page.locator(".source-content math").count() == 5
    assert page.locator(".source-content img").count() == 2
    assert (
        page.locator(
            ".source-content object, .source-content svg, .source-content script, .source-content iframe"
        ).count()
        == 0
    )
    page.locator('a[href="#source-fig"]').click()
    assert page.evaluate("location.hash") == "#source-fig"
    assert "University" in page.locator(".source-content").inner_text()


def test_source_aid_math_is_scoped_and_leaves_article_unchanged(tmp_path, browser_page):
    from tests.test_source_fidelity import rich_doc

    page, load = browser_page
    doc = rich_doc()
    doc.original_html += r"<p>Literal source delimiters \(leave_me\)</p>"
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
    doc.figures[next(iter(doc.figures))].gloss = r"Figure note \(y^2\)"
    plain = render_html(doc, AnnotationBundle(items=[]), tmp_path / "plain.html")
    load(plain)
    source_before = page.locator(".source-content").inner_html()
    aided = render_html(doc, bundle, tmp_path / "aided.html")
    load(aided)
    assert page.locator(".source-content").inner_html() == source_before
    assert page.locator(".source-content math").count() == 4
    assert page.locator(".source-content .katex").count() == 0
    assert page.locator(".source-reading-aids .katex").count() == 2
    assert page.locator(".source-figure-notes .katex").count() == 1


def test_pdf_metadata_math_does_not_rewrite_page_transcription(tmp_path, browser_page):
    from paper2html.models.document import NormalizedDocument, DocumentMetadata, Section
    from tests.test_source_fidelity import png

    page, load = browser_page
    doc = NormalizedDocument(
        metadata=DocumentMetadata(
            title=r"PDF \(x^2\)", authors=["Author"], abstract=r"Abstract \(y^2\)"
        ),
        page_assets=[png()],
        sections=[
            Section(id="sec-1", level=1, text_content=r"Original text \(not_latex\)")
        ],
    )
    load(render_html(doc, AnnotationBundle(items=[]), tmp_path / "pdf-math.html"))
    assert page.locator(".paper-header .katex").count() == 2
    assert page.locator(".source-content .katex").count() == 0
    assert (
        page.locator(".source-content pre").text_content()
        == r"Original text \(not_latex\)"
    )
