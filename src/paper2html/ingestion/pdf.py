"""Provider-neutral PDF ingestion module.

Renders each page to a JPEG at 150 DPI and extracts embedded raster/SVG figures.
No provider SDK (anthropic/openai) is imported or used.
"""

from __future__ import annotations

import hashlib
import math
import os
import tempfile
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path

import pymupdf

from paper2html.ingestion._limits import LIMITS, validate_image_dimensions
from paper2html.ingestion._safety import (
    safe_path,
    atomic_write,
    cached_image,
    raster,
    svg_png,
)


@dataclass
class FigureImage:
    """A single figure extracted from a PDF page."""

    path: Path
    """Absolute path to the saved image file; never .svg extension."""

    page_number: int
    """1-based page number in document order."""

    bbox: tuple[float, float, float, float]
    """Bounding box in PDF points (x0, y0, x1, y1); (0,0,0,0) if unknown."""

    original_format: str
    """Extension string from pymupdf extract_image (e.g. 'png', 'jpeg', 'svg')."""


@dataclass
class PdfIngestionResult:
    """Result of ingesting a single PDF."""

    page_image_paths: list[Path]
    """JPEG paths for each page, ascending order."""

    figure_image_paths: list[FigureImage]
    """Extracted figure images (raster converted to JPEG/PNG; SVG → JPEG)."""

    page_count: int
    """Total number of pages in the document."""

    pdf_path: Path
    """Absolute path to the source PDF."""

    ingestion_warnings: list[str] = field(default_factory=list)
    """Non-fatal warnings accumulated during ingestion."""


_atomic_write = atomic_write


def _convert_svg_to_jpeg(
    raw: bytes, out: Path, warnings: list[str], *, root: Path | None = None
) -> bool:
    """Convert SVG bytes to a JPEG file at *out*.

    Returns True on success, False when cairosvg is unavailable or conversion fails.
    The caller must append any relevant warning messages to *warnings*.
    """
    try:
        from PIL import Image  # noqa: PLC0415

        png_bytes = svg_png(raw)
        buf = BytesIO()
        with Image.open(BytesIO(png_bytes)) as image:
            with image.convert("RGB") as rgb:
                rgb.save(buf, format="JPEG")
        _atomic_write(raster(buf.getvalue()), out, root=root)
        return True
    except Exception as exc:
        warnings.append(f"SVG conversion error: {exc}")
        return False


def _extract_figures(
    doc: pymupdf.Document,
    fig_dir: Path,
    warnings: list[str],
    *,
    root: Path | None = None,
) -> list[FigureImage]:
    """Extract all embedded images from *doc* into *fig_dir*."""
    figures: list[FigureImage] = []
    seen: set[int] = set()

    for page_idx in range(len(doc)):
        page = doc[page_idx]
        page_number = page_idx + 1  # 1-based

        try:
            image_list = page.get_images(full=True)
        except Exception as exc:
            warnings.append(f"page {page_number}: get_images failed: {exc}")
            continue

        for img_info in image_list:
            xref = img_info[0]
            if xref in seen:
                continue
            seen.add(xref)

            try:
                if len(img_info) > 3:
                    validate_image_dimensions(img_info[2], img_info[3], limits=LIMITS)
                info = doc.extract_image(xref)
                raw: bytes = info["image"]
                ext: str = info["ext"]
                if len(raw) > LIMITS.image_bytes:
                    raise ValueError("Extracted image byte limit exceeded")
                if ext not in {"png", "jpeg", "jpg", "gif", "webp", "svg"}:
                    raise ValueError("Unsupported extracted image format")

                # Detect SVG
                is_svg = (
                    ext == "svg"
                    or raw.lstrip()[:6] in (b"<?xml ", b"<svg ")
                    or raw.lstrip()[:4] == b"<svg"
                )

                # Determine bbox
                try:
                    rects = page.get_image_rects(xref)
                    bbox: tuple[float, float, float, float] = (
                        tuple(rects[0]) if rects else (0.0, 0.0, 0.0, 0.0)
                    )  # type: ignore[assignment]
                except Exception:
                    bbox = (0.0, 0.0, 0.0, 0.0)

                if is_svg:
                    out = fig_dir / f"fig_{page_number}_{xref}.jpg"
                    if not cached_image(out, root=root):
                        success = _convert_svg_to_jpeg(raw, out, warnings, root=root)
                        if not success:
                            continue
                    figures.append(
                        FigureImage(
                            path=out.resolve(),
                            page_number=page_number,
                            bbox=bbox,
                            original_format="svg",
                        )
                    )
                else:
                    out = fig_dir / f"fig_{page_number}_{xref}.{ext}"
                    if not cached_image(out, root=root):
                        raster(raw)
                        _atomic_write(raw, out, root=root)
                    figures.append(
                        FigureImage(
                            path=out.resolve(),
                            page_number=page_number,
                            bbox=bbox,
                            original_format=ext,
                        )
                    )

            except Exception as exc:
                warnings.append(f"figure xref {xref} page {page_number}: {exc}")
                continue

    return figures


def validate_pdf(path: Path) -> None:
    if path.stat().st_size > LIMITS.source_bytes:
        raise ValueError("PDF source byte limit exceeded")
    with pymupdf.open(str(path)) as doc:
        if not doc.is_pdf or doc.needs_pass or not 0 < len(doc) <= LIMITS.pages:
            raise ValueError("Encrypted or oversized PDF")


def ingest_pdf(
    pdf_path: Path, cache_dir: Path, *, extract_figures: bool = True
) -> PdfIngestionResult:
    """Bounded ingestion using a disk snapshot; cache identity is content + DPI/version.

    Direct model adapters are outside this ingestion boundary. Native PDF/SVG
    parsing has no in-process CPU timeout.
    """
    snapshot_dir = safe_path(cache_dir, "ingestion", "pdf")
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    fd, snapshot = tempfile.mkstemp(dir=snapshot_dir, suffix=".pdf")
    try:
        digest = hashlib.sha256(b"paper2html-pdf-v2-dpi150")
        with os.fdopen(fd, "wb") as output, pdf_path.open("rb") as source:
            before = os.fstat(source.fileno())
            if before.st_size > LIMITS.source_bytes:
                raise ValueError("PDF source byte limit exceeded")
            total = 0
            while chunk := source.read(65536):
                total += len(chunk)
                if total > LIMITS.source_bytes:
                    raise ValueError("PDF source byte limit exceeded")
                digest.update(chunk)
                output.write(chunk)
            after = os.fstat(source.fileno())
            current = pdf_path.stat()
            if (before.st_size, before.st_mtime_ns, before.st_ino) != (
                after.st_size,
                after.st_mtime_ns,
                after.st_ino,
            ) or (after.st_ino, after.st_mtime_ns) != (
                current.st_ino,
                current.st_mtime_ns,
            ):
                raise ValueError("PDF source changed during snapshot")
        pages_dir = safe_path(
            cache_dir, "ingestion", "pdf", digest.hexdigest(), "pages"
        )
        fig_dir = safe_path(
            cache_dir, "ingestion", "pdf", digest.hexdigest(), "figures"
        )
        pages_dir.mkdir(parents=True, exist_ok=True)
        fig_dir.mkdir(parents=True, exist_ok=True)
        with pymupdf.open(snapshot) as doc:
            if not doc.is_pdf:
                raise ValueError("Source is not a PDF")
            if doc.needs_pass:
                raise ValueError(f"encrypted PDF: {pdf_path}")
            if not 0 < len(doc) <= LIMITS.pages:
                raise ValueError("PDF page count limit exceeded")
            warnings = []
            paths = []
            for i, page in enumerate(doc):
                width = math.ceil(page.rect.width * 150 / 72)
                height = math.ceil(page.rect.height * 150 / 72)
                if (
                    width <= 0
                    or height <= 0
                    or width * height > LIMITS.pixels
                    or max(width, height) > LIMITS.dimension
                ):
                    raise ValueError("PDF raster pixel limit exceeded")
                out = safe_path(
                    cache_dir,
                    "ingestion",
                    "pdf",
                    digest.hexdigest(),
                    "pages",
                    f"{i:04d}.jpg",
                )
                if not cached_image(out, root=cache_dir):
                    pix = page.get_pixmap(dpi=150)
                    data = pix.tobytes("jpeg")
                    raster(data)
                    _atomic_write(data, out, root=cache_dir)
                paths.append(out.resolve())
            figures = (
                _extract_figures(doc, fig_dir, warnings, root=cache_dir)
                if extract_figures
                else []
            )
            return PdfIngestionResult(
                paths, figures, len(doc), pdf_path.resolve(), warnings
            )
    finally:
        os.unlink(snapshot)
