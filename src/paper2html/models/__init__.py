"""Pydantic v2 document model for paper2html.

Re-exports the canonical document model and both ingestion adapters.
"""

from __future__ import annotations

from paper2html.models.document import (
    Citation,
    DocumentMetadata,
    Equation,
    Figure,
    IngestionPath,
    NormalizedDocument,
    Section,
    from_arxiv_html,
    from_pdf,
)
from paper2html.models.figure_analysis import FigureAnalysis

__all__ = [
    "Citation",
    "DocumentMetadata",
    "Equation",
    "Figure",
    "FigureAnalysis",
    "IngestionPath",
    "NormalizedDocument",
    "Section",
    "from_arxiv_html",
    "from_pdf",
]
