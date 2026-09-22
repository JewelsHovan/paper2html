"""Tests for paper2html.ingestion.pdf — AC-1 through AC-7 + invariants."""

from __future__ import annotations

import os
import sys
from io import BytesIO
from pathlib import Path

import pymupdf
import pytest
from PIL import Image

from paper2html.ingestion.pdf import ingest_pdf


# ---------------------------------------------------------------------------
# Test 1 — AC-1 & AC-2: page rasterization count and dimensions
# ---------------------------------------------------------------------------


def test_page_rasterization(sample_pdf: Path, cache_dir: Path) -> None:
    """AC-1: exactly N pages; AC-2: JPEG dimensions match 150 DPI render."""
    result = ingest_pdf(sample_pdf, cache_dir)

    assert result.page_count == 2
    assert len(result.page_image_paths) == 2

    # Open original doc to get reference dimensions
    doc = pymupdf.open(str(sample_pdf))
    for i, page_path in enumerate(result.page_image_paths):
        assert page_path.exists(), f"Missing page JPEG: {page_path}"
        page = doc[i]
        expected_w = round(page.rect.width / 72 * 150)
        expected_h = round(page.rect.height / 72 * 150)

        # Verify via a fresh open (Image.verify() closes the file handle)
        with Image.open(page_path) as img_for_verify:
            img_for_verify.verify()

        with Image.open(page_path) as img:
            actual_w, actual_h = img.size

        assert abs(actual_w - expected_w) <= 2, (
            f"Page {i} width: got {actual_w}, expected {expected_w} ±2"
        )
        assert abs(actual_h - expected_h) <= 2, (
            f"Page {i} height: got {actual_h}, expected {expected_h} ±2"
        )
    doc.close()


# ---------------------------------------------------------------------------
# Test 2 — AC-3: raster figure extraction
# ---------------------------------------------------------------------------


def test_figure_extraction(sample_pdf: Path, cache_dir: Path) -> None:
    """AC-3: ≥1 figure; files exist; valid JPEG/PNG; page_number == 1."""
    result = ingest_pdf(sample_pdf, cache_dir)

    assert len(result.figure_image_paths) >= 1, "Expected at least one figure"

    for fig in result.figure_image_paths:
        assert fig.path.exists(), f"Figure file missing: {fig.path}"
        assert fig.path.suffix.lower() in {".jpg", ".jpeg", ".png"}, (
            f"Unexpected figure extension: {fig.path.suffix}"
        )
        assert fig.page_number == 1, f"Expected page 1, got {fig.page_number}"

        # Pillow verify on a fresh open
        with Image.open(fig.path) as img_v:
            img_v.verify()


# ---------------------------------------------------------------------------
# Test 3 — AC-4: SVG figure rasterisation (mocked — never imports real cairosvg)
# ---------------------------------------------------------------------------


def test_svg_rasterization(
    sample_pdf: Path, cache_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-4: SVG figure → original_format=='svg', path.suffix=='.jpg', valid JPEG."""
    # Build a tiny valid PNG in memory to be returned by our mock svg2png
    tiny_png_buf = BytesIO()
    Image.new("RGB", (4, 4), (0, 128, 255)).save(tiny_png_buf, format="PNG")
    tiny_png_bytes = tiny_png_buf.getvalue()

    from paper2html.ingestion import _svg

    monkeypatch.setattr(_svg, "_convert", lambda *args: tiny_png_bytes)

    # Patch extract_image on the Document class so one xref returns SVG bytes
    svg_payload = b"<svg xmlns='http://www.w3.org/2000/svg' width='4' height='4'/>"

    original_extract_image = pymupdf.Document.extract_image

    call_count: dict[str, int] = {"n": 0}

    def patched_extract_image(self: pymupdf.Document, xref: int) -> dict:
        result_dict = original_extract_image(self, xref)
        if call_count["n"] == 0:
            call_count["n"] += 1
            return {"image": svg_payload, "ext": "svg"}
        return result_dict

    monkeypatch.setattr(pymupdf.Document, "extract_image", patched_extract_image)

    result = ingest_pdf(sample_pdf, cache_dir)

    svg_figs = [f for f in result.figure_image_paths if f.original_format == "svg"]
    assert len(svg_figs) >= 1, "Expected at least one SVG figure in results"

    fig = svg_figs[0]
    assert fig.original_format == "svg"
    assert fig.path.suffix == ".jpg", f"Expected .jpg, got {fig.path.suffix}"
    assert fig.path.exists(), f"SVG-converted JPEG missing: {fig.path}"
    assert not str(fig.path).endswith(".svg"), "Path must never be .svg"

    # Verify it is a valid JPEG
    with Image.open(fig.path) as img_v:
        img_v.verify()


# ---------------------------------------------------------------------------
# Test 4 — AC-5: cache hit (second run reuses files, mtimes unchanged)
# ---------------------------------------------------------------------------


def test_cache_hit(sample_pdf: Path, cache_dir: Path) -> None:
    """AC-5: second run returns identical paths with identical mtimes."""
    result1 = ingest_pdf(sample_pdf, cache_dir)

    all_paths1 = result1.page_image_paths + [f.path for f in result1.figure_image_paths]
    mtimes1 = {p: os.stat(p).st_mtime_ns for p in all_paths1}

    result2 = ingest_pdf(sample_pdf, cache_dir)

    all_paths2 = result2.page_image_paths + [f.path for f in result2.figure_image_paths]

    assert all_paths1 == all_paths2, "Paths differ between runs"

    for p in all_paths2:
        assert os.stat(p).st_mtime_ns == mtimes1[p], f"mtime changed on second run: {p}"


# ---------------------------------------------------------------------------
# Test 5 — AC-7: missing PDF raises FileNotFoundError
# ---------------------------------------------------------------------------


def test_missing_pdf(cache_dir: Path) -> None:
    """AC-7: ingest_pdf raises FileNotFoundError for non-existent PDF."""
    with pytest.raises(FileNotFoundError):
        ingest_pdf(Path("/nonexistent/path/paper.pdf"), cache_dir)


# ---------------------------------------------------------------------------
# Test 6 — AC-6: provider isolation (no anthropic/openai import)
# ---------------------------------------------------------------------------


def test_provider_isolation(tmp_path: Path) -> None:
    """AC-6: module imports cleanly even when anthropic/openai are blocked."""
    code = """
import sys

class _BlockProviders:
    _BLOCKED = frozenset(["anthropic", "openai"])

    def find_module(self, fullname, path=None):
        if any(fullname == b or fullname.startswith(b + ".") for b in self._BLOCKED):
            return self

    def load_module(self, fullname):
        raise ImportError(f"provider SDK blocked: {fullname}")

sys.meta_path.insert(0, _BlockProviders())

from paper2html.ingestion.pdf import ingest_pdf
print("OK")
"""
    import subprocess

    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        f"Subprocess failed (rc={proc.returncode}):\nstdout: {proc.stdout}\nstderr: {proc.stderr}"
    )
    assert "OK" in proc.stdout


# ---------------------------------------------------------------------------
# Test 7 — Invariant: no .svg files in output
# ---------------------------------------------------------------------------


def test_no_svg_in_output(sample_pdf: Path, cache_dir: Path) -> None:
    """Invariant: no path has .svg suffix; no output file contains SVG markup."""
    result = ingest_pdf(sample_pdf, cache_dir)

    all_paths = result.page_image_paths + [f.path for f in result.figure_image_paths]

    for p in all_paths:
        assert p.suffix.lower() != ".svg", f"SVG file found in output: {p}"
        with open(p, "rb") as fh:
            header = fh.read(64)
        stripped = header.lstrip()
        assert not stripped.startswith(b"<svg"), f"SVG content in output file: {p}"
        assert not stripped.startswith(b"<?xml"), f"XML/SVG content in output file: {p}"


# ---------------------------------------------------------------------------
# Test 8 — Collision safety: two different PDFs named "paper.pdf"
# ---------------------------------------------------------------------------


def test_stem_collision(tmp_path: Path, cache_dir: Path) -> None:
    """Two PDFs with the same stem go to disjoint output directories."""

    def _make_pdf(subdir: str, width: int, height: int) -> Path:
        d = tmp_path / subdir
        d.mkdir()
        doc = pymupdf.open()
        page = doc.new_page(-1, width=width, height=height)
        page.insert_text((50, 100), f"Page from {subdir}")
        path = d / "paper.pdf"
        doc.save(str(path))
        doc.close()
        return path

    pdf_a = _make_pdf("dir_a", 595, 842)
    pdf_b = _make_pdf("dir_b", 400, 600)

    result_a = ingest_pdf(pdf_a, cache_dir)
    result_b = ingest_pdf(pdf_b, cache_dir)

    # Output dirs must be different
    dir_a = result_a.page_image_paths[0].parent
    dir_b = result_b.page_image_paths[0].parent
    assert dir_a != dir_b, "Collision: both PDFs wrote to the same pages/ dir"

    # Each result's paths must not appear in the other result
    paths_a = set(result_a.page_image_paths)
    paths_b = set(result_b.page_image_paths)
    assert paths_a.isdisjoint(paths_b), "Paths overlap between different PDFs"


# ---------------------------------------------------------------------------
# Test 9 — Corrupt PDF raises an exception
# ---------------------------------------------------------------------------


def test_corrupt_pdf(tmp_path: Path, cache_dir: Path) -> None:
    """A file with garbage content must raise an exception, never return silently."""
    bad_pdf = tmp_path / "bad.pdf"
    bad_pdf.write_bytes(b"not a pdf")

    with pytest.raises(Exception):
        ingest_pdf(bad_pdf, cache_dir)
