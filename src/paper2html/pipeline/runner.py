"""Synchronous entry point for the pipeline.

Design notes
------------
* ``run_pipeline`` is a thin sync bridge that builds the async components and
  calls ``asyncio.run()``.  Tests that need to inspect async internals should
  construct ``OrchestratorRunner`` and ``await runner.run(document)`` directly.
* ``config.max_cost_usd`` is the single source of truth for the budget cap —
  there is no separate ``max_cost_usd`` argument here (AI Review amendment #9).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from paper2html.cache import get_cache
from paper2html.pipeline.budget import BudgetTracker
from paper2html.pipeline.orchestrator import OrchestratorRunner
from paper2html.pipeline.schemas import PipelineConfig, PipelineResult

if TYPE_CHECKING:
    from rich.console import Console

    from paper2html.models.document import NormalizedDocument
    from paper2html.pipeline.providers.base import ProviderInterface


def run_pipeline(
    document: "NormalizedDocument",
    config: PipelineConfig,
    provider: "ProviderInterface",
    cache_dir: Path | None = None,
    console: "Console | None" = None,
) -> PipelineResult:
    """Run the full pipeline synchronously.

    This one-shot entry point closes provider-owned SDK clients and its cache.
    Injected SDK clients remain caller-owned. Tests should use ``OrchestratorRunner.run()``
    directly (``await``-able) to avoid ``asyncio.run()`` inside an existing loop.

    Args:
        document: Normalised document to analyse.
        config: Pipeline configuration (``config.max_cost_usd`` is the cap).
        provider: Provider instance to use.
        cache_dir: Cache directory (defaults to ``~/.cache/paper2html``).
        console: Optional Rich console for progress output.

    Returns:
        Validated ``PipelineResult``.

    Raises:
        BudgetExceededError: If the budget cap is exceeded.
    """

    async def run_and_close() -> PipelineResult:
        cache = None
        try:
            cache = get_cache(cache_dir)
            budget = BudgetTracker(
                max_cost_usd=config.max_cost_usd, max_concurrency=config.max_concurrency
            )
            runner = OrchestratorRunner(
                provider=provider,
                config=config,
                budget=budget,
                cache=cache,
                console=console,
            )
            return await runner.run(document)
        finally:
            try:
                await provider.aclose()
            finally:
                if cache is not None:
                    cache.close()

    return asyncio.run(run_and_close())
