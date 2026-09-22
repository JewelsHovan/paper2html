"""Pytest fixtures for ingestion tests."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pymupdf
import pytest
from PIL import Image


@pytest.fixture()
def sample_pdf(tmp_path: Path) -> Path:
    """Create a 2-page sample PDF with an embedded raster image on page 1."""
    doc = pymupdf.open()

    # Page 1: portrait 595x842, with text and an embedded red PNG
    page1 = doc.new_page(-1, width=595, height=842)
    page1.insert_text((50, 100), "Hello, world! Page 1")
    img = Image.new("RGB", (20, 20), (255, 0, 0))
    buf = BytesIO()
    img.save(buf, format="PNG")
    page1.insert_image(pymupdf.Rect(10, 30, 50, 70), stream=buf.getvalue())

    # Page 2: landscape 842x595, text only
    page2 = doc.new_page(-1, width=842, height=595)
    page2.insert_text((50, 100), "Hello, world! Page 2")

    pdf_path = tmp_path / "sample.pdf"
    doc.save(str(pdf_path))
    doc.close()
    return pdf_path


@pytest.fixture()
def cache_dir(tmp_path: Path) -> Path:
    """Return a cache directory path — NOT pre-created; ingest_pdf must mkdir."""
    return tmp_path / "cache"
