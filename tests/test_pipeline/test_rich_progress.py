"""Rich progress output tests.

AC-9: Progress bar increments per completed worker; cost line in footer.
"""

from __future__ import annotations

from io import StringIO

import pytest
from rich.console import Console

from paper2html.models.document import NormalizedDocument
from paper2html.pipeline.budget import BudgetTracker
from paper2html.pipeline.orchestrator import OrchestratorRunner
from paper2html.pipeline.providers.mock_provider import FailMode, MockProvider
from paper2html.pipeline.schemas import PipelineConfig

pytestmark = pytest.mark.asyncio


async def test_progress_output_contains_cost_line(
    synthetic_document: NormalizedDocument,
    default_config: PipelineConfig,
    captured_console: tuple[Console, StringIO],
) -> None:
    """Progress output contains at least one cost line with '$' symbol."""
    console, buf = captured_console
    provider = MockProvider()
    budget = BudgetTracker()
    runner = OrchestratorRunner(provider, default_config, budget, console=console)
    await runner.run(synthetic_document)

    output = buf.getvalue()
    assert "$" in output, f"Expected '$' in console output. Got:\n{output!r}"


async def test_progress_shows_per_worker_advancement(
    synthetic_document: NormalizedDocument,
    default_config: PipelineConfig,
) -> None:
    """Workers complete individually — progress advances once per section."""
    buf = StringIO()
    # Use force_terminal=True so Rich renders progress bar control sequences
    console = Console(file=buf, force_terminal=True, width=120)
    provider = MockProvider()
    budget = BudgetTracker()
    runner = OrchestratorRunner(provider, default_config, budget, console=console)
    await runner.run(synthetic_document)

    # All 5 sections completed means 5 cost prints happened
    output = buf.getvalue()
    # Count '$' occurrences — one per section advancement
    dollar_count = output.count("$")
    assert dollar_count >= 5, (
        f"Expected at least 5 cost prints (one per section), got {dollar_count}. "
        f"Output:\n{output!r}"
    )


async def test_progress_completes_when_all_workers_fail(
    synthetic_document: NormalizedDocument,
    default_config: PipelineConfig,
) -> None:
    """Regression: the bar must reach 100% even when every worker fails.

    Reproduces the real-run symptom where all section workers errored
    (e.g. on unparseable JSON) and the bar stayed pinned at 0%, hiding that
    the sections were attempted. ``progress.advance`` runs in a finally block,
    so a fully-failing wave still renders a completed bar.
    """
    buf = StringIO()
    console = Console(file=buf, force_terminal=True, width=120)
    provider = MockProvider(fail_mode=FailMode.ALL_CALLS)
    budget = BudgetTracker()
    runner = OrchestratorRunner(provider, default_config, budget, console=console)

    result = await runner.run(synthetic_document)

    # Every section failed (exhausted retries) — yet the run completes...
    assert len(result.failed_sections) == 5
    assert len(result.section_analyses) == 0
    # ...and the progress bar rendered completion, not a stuck 0%.
    assert "100%" in buf.getvalue(), (
        "Progress bar should reach 100% even when all workers fail. "
        f"Output:\n{buf.getvalue()!r}"
    )


async def test_progress_does_not_suppress_result(
    synthetic_document: NormalizedDocument,
    default_config: PipelineConfig,
    captured_console: tuple[Console, StringIO],
) -> None:
    """Progress output does not interfere with the returned PipelineResult."""
    console, _ = captured_console
    provider = MockProvider()
    budget = BudgetTracker()
    runner = OrchestratorRunner(provider, default_config, budget, console=console)
    result = await runner.run(synthetic_document)

    # Result must still be complete
    assert len(result.section_analyses) == 5
