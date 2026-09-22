"""Integration tests for figure_vision (ticket 007).

These tests make real provider API calls and are auto-skipped unless
``PAPER2HTML_INTEGRATION_TESTS=1`` is set (see tests/conftest.py).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from paper2html.figure_vision import FigureVisionWorker
from paper2html.models import Figure, NormalizedDocument
from paper2html.models.document import DocumentMetadata
from paper2html.pipeline.providers.anthropic_provider import AnthropicProvider

pytestmark = pytest.mark.asyncio

_FIXTURES_DIR = Path(__file__).parent / "fixtures"
_JPEG_PATH = _FIXTURES_DIR / "figure_100x100.jpg"

_MODEL = "claude-haiku-4-5"


@pytest.mark.integration
async def test_integration_figure_vision_real_provider() -> None:
    """End-to-end: real Anthropic provider + fixture JPEG → non-empty FigureAnalysis."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        pytest.skip("ANTHROPIC_API_KEY not set")

    jpeg_bytes = _JPEG_PATH.read_bytes()
    fig = Figure(
        id="fig-1",
        caption="Figure 1: Sample figure for integration test",
        image_bytes=jpeg_bytes,
        image_mime_type="image/jpeg",
    )
    doc = NormalizedDocument(
        metadata=DocumentMetadata(title="Integration Test Paper"),
        sections=[],
    )

    provider = AnthropicProvider(api_key=api_key)
    worker = FigureVisionWorker(provider, model=_MODEL)
    result = await worker.analyze(fig, doc)

    assert result.alt_text, "alt_text must be non-empty"
    assert result.gloss, "gloss must be non-empty"
    assert len(result.alt_text) <= 150, "alt_text must be ≤ 150 chars"
    assert result.figure_id == "fig-1"
    assert result.provider_model == _MODEL
    assert isinstance(result.source_anchors, list)
