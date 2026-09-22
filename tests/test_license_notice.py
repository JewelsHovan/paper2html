"""Self-contained HTML must carry notices for the third-party code it embeds."""

from paper2html.renderer import render_html
from tests.test_renderer import make_empty_bundle, make_pdf_doc


def test_embedded_katex_retains_full_mit_notice(tmp_path):
    output = render_html(make_pdf_doc(), make_empty_bundle(), tmp_path / "paper.html")
    html = output.read_text(encoding="utf-8")
    assert "The MIT License (MIT)" in html
    assert "Copyright (c) 2013-2020 Khan Academy and other contributors" in html
    assert (
        "The above copyright notice and this permission notice shall be included"
        in html
    )
    assert 'THE SOFTWARE IS PROVIDED "AS IS"' in html
