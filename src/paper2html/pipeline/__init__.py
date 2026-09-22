"""Agent pipeline package.

Re-exports the public surface for downstream consumers.
"""

from __future__ import annotations

from paper2html.pipeline.errors import (
    BudgetExceededError,
    PipelineError,
    ProviderError,
    WorkerValidationError,
)
from paper2html.pipeline.orchestrator import OrchestratorRunner
from paper2html.pipeline.runner import run_pipeline
from paper2html.pipeline.schemas import (
    FigureContext,
    PipelineConfig,
    PipelineResult,
    SectionFailure,
    WorkerInput,
    WorkerOutput,
)

__all__ = [
    "BudgetExceededError",
    "FigureContext",
    "OrchestratorRunner",
    "PipelineConfig",
    "PipelineError",
    "PipelineResult",
    "ProviderError",
    "SectionFailure",
    "WorkerInput",
    "WorkerOutput",
    "WorkerValidationError",
    "run_pipeline",
]
