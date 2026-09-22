"""arXiv and PDF ingestion package."""

from __future__ import annotations

from paper2html.ingestion._client import close_client
from paper2html.ingestion._limits import IngestionLimits
from paper2html.ingestion._metadata import normalise_id
from paper2html.ingestion.arxiv import ingest_arxiv
from paper2html.ingestion._types import (
    ArxivIngestResult,
    ArxivMetadata,
    Figure,
    MathMLNode,
    PdfFallbackResult,
    Section,
)
from paper2html.ingestion.pdf import FigureImage, PdfIngestionResult, ingest_pdf

__all__ = [
    "close_client",
    "ingest_arxiv",
    "normalise_id",
    "IngestionLimits",
    "ArxivIngestResult",
    "ArxivMetadata",
    "Figure",
    "MathMLNode",
    "PdfFallbackResult",
    "Section",
    "FigureImage",
    "PdfIngestionResult",
    "ingest_pdf",
]
