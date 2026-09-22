"""The CLI must not bypass ingestion image limits when it normalizes PDFs."""

from pathlib import Path
from unittest.mock import Mock

import pymupdf
import pytest

from paper2html.ingestion._types import ArxivMetadata, PdfFallbackResult
from paper2html.ingestion.pdf import _extract_figures
from paper2html.models.document import from_pdf
from tests.test_cli_process import _invoke, _make_fixture_pdf
from tests.test_renderer import _TINY_PNG


@pytest.mark.parametrize("dimensions", [(30_000, 30_000), (16_385, 1), (0, 10)])
def test_ingestion_then_normalization_never_extracts_oversized_image(
    tmp_path, dimensions
):
    page = Mock()
    page.get_images.return_value = [(7, 0, *dimensions)]
    page.get_text.return_value = "Source prose survives a skipped figure."

    # Both stages consume the same deliberately oversized native image descriptor.
    class Document:
        extract_image = Mock(side_effect=AssertionError("unsafe native extraction"))

        def __len__(self):
            return 1

        def __getitem__(self, index):
            return page

    document = Document()
    warnings = []
    assert _extract_figures(document, tmp_path / "figures", warnings) == []
    document.extract_image.assert_not_called()
    normalized = from_pdf(document, Path("paper.pdf"))
    document.extract_image.assert_not_called()
    assert normalized.sections[0].text_content == page.get_text.return_value
    assert normalized.figures["fig-1"].image_bytes == b""
    assert any("pixel limit" in message for message in normalized.ingestion_warnings)


@pytest.mark.parametrize("fallback", [False, True])
def test_cli_never_reextracts_rejected_pdf_image(tmp_path, monkeypatch, fallback):
    source = _make_fixture_pdf(tmp_path)
    with pymupdf.open(source) as document:
        document[0].insert_image(pymupdf.Rect(40, 150, 60, 170), stream=_TINY_PNG)
        document.saveIncr()
    original_images = pymupdf.Page.get_images

    def oversized_images(page, *args, **kwargs):
        return [
            (*image[:2], 30_000, 30_000, *image[4:])
            for image in original_images(page, *args, **kwargs)
        ]

    extract = Mock(
        side_effect=AssertionError("oversized image reached native extraction")
    )
    monkeypatch.setattr(pymupdf.Page, "get_images", oversized_images)
    monkeypatch.setattr(pymupdf.Document, "extract_image", extract)
    if fallback:
        monkeypatch.setattr(
            "paper2html.cli.ingest_arxiv",
            lambda *args: PdfFallbackResult(
                ArxivMetadata(arxiv_id="2301.00001", title="Fallback paper"), source
            ),
        )
    output = tmp_path / "result.html"
    result = _invoke(
        tmp_path / "cache",
        "--provider",
        "mock",
        "process",
        "2301.00001" if fallback else str(source),
        "-o",
        str(output),
    )
    assert result.exit_code == 0, result.output
    extract.assert_not_called()
    assert output.is_file()
    assert "pixel limit" in result.output
