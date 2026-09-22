"""Top-level arXiv ingestion entry point."""

from __future__ import annotations

import logging
from pathlib import Path

from paper2html.ingestion._client import get_client
from paper2html.ingestion._figure_fetcher import build_figures
from paper2html.ingestion._html_parser import fetch_html_page, parse_sections
from paper2html.ingestion._metadata import fetch_metadata, normalise_id
from paper2html.ingestion._types import ArxivIngestResult, PdfFallbackResult

logger = logging.getLogger(__name__)


def ingest_arxiv(
    paper_id: str, cache_dir: Path
) -> ArxivIngestResult | PdfFallbackResult:
    """Ingest an arXiv paper, returning structured content or a PDF fallback.

    Args:
        paper_id: An arXiv ID in any supported form (plain ID, versioned,
                  or full abs/pdf URL).
        cache_dir: Root directory for all cached files.  Nothing is written
                   outside this directory.

    Returns:
        ArxivIngestResult if the native HTML version is available, or
        PdfFallbackResult if arXiv returns 404 for the HTML page.
    """
    arxiv_id = normalise_id(paper_id)
    logger.info("Ingesting arXiv paper %s (normalised from %r)", arxiv_id, paper_id)

    metadata = fetch_metadata(arxiv_id)

    html_text, page_url = fetch_html_page(arxiv_id)

    if html_text is None:
        logger.info("HTML unavailable for %s, falling back to PDF", arxiv_id)
        pdf_path = _download_pdf(arxiv_id, cache_dir)
        return PdfFallbackResult(metadata=metadata, pdf_path=pdf_path)

    # Parse sections with their figure refs, then fetch figures.
    section_tuples, orphan_refs = parse_sections(html_text, page_url)  # type: ignore[arg-type]

    asset_cache = {}
    warnings: list[str] = []
    sections = []
    for section, fig_refs in section_tuples:
        section.figures = build_figures(fig_refs, asset_cache, warnings)
        sections.append(section)

    orphan_figures = build_figures(orphan_refs, asset_cache, warnings)

    return ArxivIngestResult(
        metadata=metadata,
        sections=sections,
        raw_html=html_text,
        orphan_figures=orphan_figures,
        ingestion_warnings=warnings,
    )


def _download_pdf(arxiv_id: str, cache_dir: Path) -> Path:
    """Download the PDF for *arxiv_id* into *cache_dir*/ingestion/downloads/<id>.pdf.

    The file is written atomically (temp file + os.replace) so a crash
    during download does not leave a partial file at the target path.

    Raises ValueError if the resolved target path escapes *cache_dir*.
    """
    from paper2html.ingestion._safety import safe_path, atomic_write
    from paper2html.ingestion._limits import LIMITS
    from paper2html.ingestion.pdf import validate_pdf

    safe_id = normalise_id(arxiv_id).replace("/", "_")
    target = safe_path(cache_dir, "ingestion", "downloads", f"{safe_id}.pdf")
    if target.exists():
        try:
            validate_pdf(target)
            return target
        except (ValueError, RuntimeError):
            pass
    response = get_client().get(
        f"https://arxiv.org/pdf/{arxiv_id}", max_bytes=LIMITS.source_bytes
    )
    response.raise_for_status()
    # Validate before publishing the download.
    import pymupdf

    with pymupdf.open(stream=response.content, filetype="pdf") as doc:
        if not doc.is_pdf or doc.needs_pass or not 0 < len(doc) <= LIMITS.pages:
            raise ValueError("Encrypted or oversized PDF download")
    atomic_write(response.content, target, root=cache_dir)
    return target
