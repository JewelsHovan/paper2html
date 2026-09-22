"""Shared data types for the arXiv ingestion pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ArxivMetadata:
    """Bibliographic metadata for an arXiv paper."""

    arxiv_id: str
    title: str
    authors: list[str] = field(default_factory=list)
    abstract: str = ""
    categories: list[str] = field(default_factory=list)
    published: str = ""
    updated: str = ""


@dataclass
class MathMLNode:
    """A single MathML element extracted from the document."""

    raw_mathml: str


@dataclass
class Figure:
    """Validated raster bytes, preserved in their original format; SVG becomes PNG."""

    caption: str
    asset_bytes: bytes
    mathml_nodes: list[MathMLNode] = field(default_factory=list)
    # Raw ``<img src>`` attribute (pre-urljoin), used to key figure_assets when
    # bridging into NormalizedDocument.from_arxiv_html.
    raw_src: str | None = None
    mime_type: str = "image/png"


@dataclass
class Section:
    """One section of the paper, with its child figures and equations."""

    heading_level: int
    heading_text: str
    body_html: str
    equation_refs: list[str] = field(default_factory=list)
    figures: list[Figure] = field(default_factory=list)
    mathml_nodes: list[MathMLNode] = field(default_factory=list)


@dataclass
class ArxivIngestResult:
    """Successful ingestion result when native HTML is available."""

    metadata: ArxivMetadata
    sections: list[Section] = field(default_factory=list)
    raw_html: str = ""
    orphan_figures: list[Figure] = field(default_factory=list)
    ingestion_warnings: list[str] = field(default_factory=list)

    @property
    def figures(self) -> list[Figure]:
        """All figures: flattened from sections plus top-level orphans."""
        result: list[Figure] = []
        for section in self.sections:
            result.extend(section.figures)
        result.extend(self.orphan_figures)
        return result


@dataclass
class PdfFallbackResult:
    """Fallback result when arXiv HTML is unavailable (404)."""

    metadata: ArxivMetadata
    pdf_path: Path


@dataclass
class FigureRef:
    """Intermediate reference to a figure before bytes are fetched."""

    caption: str
    src_url: str | None = None
    inline_svg: bytes | None = None
    caption_mathml: list[str] = field(default_factory=list)
    # Raw ``<img src>`` attribute value (before urljoin resolution).
    raw_src: str | None = None
