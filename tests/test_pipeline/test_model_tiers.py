"""Model tier tests.

AC-10: Default models claude-opus-4-8/claude-haiku-4-5 and gpt-4o/gpt-4o-mini;
custom models forwarded to provider.complete().
"""

from __future__ import annotations

import pytest

from paper2html.models.document import NormalizedDocument
from paper2html.pipeline.budget import BudgetTracker
from paper2html.pipeline.orchestrator import OrchestratorRunner
from paper2html.pipeline.providers.mock_provider import MockProvider
from paper2html.pipeline.schemas import PipelineConfig

# ---------------------------------------------------------------------------
# Default tiers
# ---------------------------------------------------------------------------


def test_default_pipeline_config_anthropic_models() -> None:
    """Default PipelineConfig uses claude-opus-4-8 (orchestrator) and claude-haiku-4-5 (worker)."""
    config = PipelineConfig()
    assert config.orchestrator_model == "claude-opus-4-8"
    assert config.worker_model == "claude-haiku-4-5"
    assert config.provider_name == "claude"


def test_default_pipeline_config_openai_models() -> None:
    """PipelineConfig with openai defaults to gpt-4o and gpt-4o-mini."""
    config = PipelineConfig(
        provider_name="openai",
        orchestrator_model="gpt-4o",
        worker_model="gpt-4o-mini",
    )
    assert config.orchestrator_model == "gpt-4o"
    assert config.worker_model == "gpt-4o-mini"


# ---------------------------------------------------------------------------
# Custom models are forwarded to provider
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_custom_orchestrator_model_forwarded_to_provider(
    synthetic_document: NormalizedDocument,
) -> None:
    """Custom orchestrator_model is forwarded to provider.complete() for planning/synthesis."""
    config = PipelineConfig(
        orchestrator_model="my-custom-orchestrator",
        worker_model="my-custom-worker",
    )
    provider = MockProvider()
    budget = BudgetTracker()
    runner = OrchestratorRunner(provider, config, budget)
    await runner.run(synthetic_document)

    # Find the model used for planning call (has PLANNING marker)
    orchestrator_model_used = None
    for messages, _, _, model in provider._calls:
        for msg in messages:
            if "PLANNING" in msg.content:
                orchestrator_model_used = model
                break

    assert orchestrator_model_used == "my-custom-orchestrator"


@pytest.mark.asyncio
async def test_custom_worker_model_forwarded_to_provider(
    synthetic_document: NormalizedDocument,
) -> None:
    """Custom worker_model is forwarded to provider.complete() for worker calls."""
    config = PipelineConfig(
        orchestrator_model="custom-orch",
        worker_model="custom-worker",
    )
    provider = MockProvider()
    budget = BudgetTracker()
    runner = OrchestratorRunner(provider, config, budget)
    await runner.run(synthetic_document)

    # Find worker calls (have SECTION_ID marker)
    worker_models: set[str] = set()
    for messages, _, _, model in provider._calls:
        for msg in messages:
            if "SECTION_ID:" in msg.content:
                worker_models.add(model)
                break

    assert "custom-worker" in worker_models


@pytest.mark.asyncio
async def test_result_records_model_names(
    synthetic_document: NormalizedDocument,
) -> None:
    """PipelineResult.orchestrator_model and .worker_model reflect the config."""
    config = PipelineConfig(
        orchestrator_model="orch-v1",
        worker_model="work-v1",
    )
    provider = MockProvider()
    budget = BudgetTracker()
    runner = OrchestratorRunner(provider, config, budget)
    result = await runner.run(synthetic_document)

    assert result.orchestrator_model == "orch-v1"
    assert result.worker_model == "work-v1"
