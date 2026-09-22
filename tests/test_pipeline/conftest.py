"""Shared fixtures for test_pipeline tests.

Note: ``pytestmark = pytest.mark.asyncio`` must be placed at the TOP of each
async test MODULE, not here — pytest-asyncio strict mode ignores pytestmark
defined in conftest.py.
"""

from __future__ import annotations

from io import StringIO

import pytest
from rich.console import Console

from paper2html.models.document import (
    DocumentMetadata,
    Figure,
    IngestionPath,
    NormalizedDocument,
    Section,
)
from paper2html.pipeline.providers.mock_provider import MockProvider
from paper2html.pipeline.schemas import PipelineConfig

# ---------------------------------------------------------------------------
# Real PNG header bytes — used by all tests that need actual image bytes.
# ASCII placeholders like b"PNG" pass byte tests falsely; use the real header.
# ---------------------------------------------------------------------------
_PNG_HEADER = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8  # IHDR chunk start


def _make_figure(fig_id: str, section_id: str) -> Figure:
    """Build a test figure with real PNG bytes."""
    return Figure(
        id=fig_id,
        caption=f"Caption for {fig_id}",
        image_bytes=_PNG_HEADER + fig_id.encode(),
        image_mime_type="image/png",
        referencing_sentence_ids=[f"{section_id}-sent-1"],
    )


def _make_section(sec_id: str, fig_ids: list[str] | None = None) -> Section:
    """Build a test section."""
    return Section(
        id=sec_id,
        title=f"Section {sec_id}",
        level=1,
        text_content=f"This is the unique text content for section {sec_id}. "
        f"It contains information specific to {sec_id} only.",
        figure_ids=fig_ids or [],
    )


@pytest.fixture
def synthetic_document() -> NormalizedDocument:
    """5 sections, 3 figures.

    Layout:
        fig-1, fig-2 → sec-2
        fig-3 → sec-4
        sec-1, sec-3, sec-5 have no figures.
    """
    sections = [_make_section(f"sec-{i}") for i in range(1, 6)]
    sections[1].figure_ids = ["fig-1", "fig-2"]  # sec-2
    sections[3].figure_ids = ["fig-3"]  # sec-4

    figures = {
        "fig-1": _make_figure("fig-1", "sec-2"),
        "fig-2": _make_figure("fig-2", "sec-2"),
        "fig-3": _make_figure("fig-3", "sec-4"),
    }

    return NormalizedDocument(
        metadata=DocumentMetadata(
            arxiv_id="2301.00001",
            title="Test Paper",
            abstract="A test abstract.",
            ingestion_path=IngestionPath.ARXIV_HTML,
        ),
        sections=sections,
        figures=figures,
    )


@pytest.fixture
def synthetic_document_pdf() -> NormalizedDocument:
    """5-section doc from the PDF ingestion path (no arxiv_id, no referencing_sentence_ids)."""
    sections = [_make_section(f"sec-{i}") for i in range(1, 6)]
    sections[1].figure_ids = ["fig-1"]

    figures = {
        "fig-1": Figure(
            id="fig-1",
            caption="PDF figure",
            image_bytes=_PNG_HEADER,
            image_mime_type="image/png",
            referencing_sentence_ids=[],  # PDF path: empty
        ),
    }

    return NormalizedDocument(
        metadata=DocumentMetadata(
            arxiv_id=None,  # PDF path: no arXiv id
            title="PDF Paper",
            abstract="",
            ingestion_path=IngestionPath.PDF,
        ),
        sections=sections,
        figures=figures,
    )


@pytest.fixture
def large_document() -> NormalizedDocument:
    """10 sections, 4 figures — used for context-slicing tests."""
    sections = [_make_section(f"sec-{i}") for i in range(1, 11)]
    sections[2].figure_ids = ["fig-1", "fig-2"]  # sec-3
    sections[6].figure_ids = ["fig-3", "fig-4"]  # sec-7

    figures = {
        "fig-1": _make_figure("fig-1", "sec-3"),
        "fig-2": _make_figure("fig-2", "sec-3"),
        "fig-3": _make_figure("fig-3", "sec-7"),
        "fig-4": _make_figure("fig-4", "sec-7"),
    }

    return NormalizedDocument(
        metadata=DocumentMetadata(
            arxiv_id="2301.99999",
            title="Large Test Paper",
            abstract="Large test abstract.",
        ),
        sections=sections,
        figures=figures,
    )


@pytest.fixture
def mock_provider() -> MockProvider:
    """Default MockProvider with no failure injection."""
    return MockProvider()


@pytest.fixture
def default_config() -> PipelineConfig:
    """Default PipelineConfig (no budget cap)."""
    return PipelineConfig()


@pytest.fixture
def tiny_budget_config() -> PipelineConfig:
    """PipelineConfig with a deliberately tiny budget cap."""
    return PipelineConfig(max_cost_usd=0.000_001)


@pytest.fixture
def tmp_cache(tmp_path):
    """A diskcache.Cache backed by a temp directory."""
    import diskcache

    return diskcache.Cache(str(tmp_path / "cache"))


@pytest.fixture
def captured_console() -> tuple[Console, StringIO]:
    """Rich Console writing to a StringIO buffer, plus the buffer."""
    buf = StringIO()
    console = Console(file=buf, force_terminal=False, width=120)
    return console, buf
