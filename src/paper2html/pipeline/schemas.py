"""Pydantic v2 schemas for the agent pipeline.

Design notes
------------
* ``_PipelineBase`` owns the ``ConfigDict`` with base64 bytes serialisation so
  that ``WorkerInput.figures[n].image_bytes`` round-trips through JSON without
  raising on binary data (PNG/JPEG bytes).
* All new-field additions to ``WorkerOutput`` and ``PipelineResult`` MUST have
  defaults — this keeps the schema versioning non-breaking.
* ``PipelineConfig`` uses ``"claude"`` / ``"openai"`` as provider names (matching
  the existing ``Config.provider`` convention), not SDK names.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ---------------------------------------------------------------------------
# Shared base
# ---------------------------------------------------------------------------


class _PipelineBase(BaseModel):
    """All pipeline schemas inherit this for consistent bytes serialisation."""

    model_config = ConfigDict(ser_json_bytes="base64", val_json_bytes="base64")


# ---------------------------------------------------------------------------
# Figure context (sliced per worker)
# ---------------------------------------------------------------------------


class FigureContext(_PipelineBase):
    """A figure referenced by one section, carried in ``WorkerInput``.

    Attributes:
        figure_id: Unique figure id (e.g. ``"fig-1"``).
        caption: Figure caption text.
        image_bytes: Raw image bytes (PNG / JPEG).  Base64 in JSON mode.
        image_mime_type: MIME type of ``image_bytes``.
        referencing_sentence_ids: Sentence ids that reference this figure
            (populated on arXiv-HTML path; empty on PDF path).
    """

    figure_id: str
    caption: str = ""
    image_bytes: bytes = b""
    image_mime_type: str = "image/png"
    referencing_sentence_ids: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Worker I/O
# ---------------------------------------------------------------------------


class WorkerInput(_PipelineBase):
    """Context slice sent to a section worker.

    Attributes:
        section_id: The section being processed.
        section_title: Section heading (may be empty).
        section_text: Full text of this section ONLY — no other section's text.
        figures: Figures whose ``figure_id`` appears in ``section.figure_ids``.
        doc_title: Document title (shared header, not secret).
        doc_abstract: Document abstract (shared header, not secret).
    """

    section_id: str
    section_title: str = ""
    section_text: str
    focus_query: str = ""
    figures: list[FigureContext] = Field(default_factory=list)
    doc_title: str = ""
    doc_abstract: str = ""


class WorkerOutput(_PipelineBase):
    """Structured result from one section worker.

    Adding new fields is always non-breaking when they have defaults.

    Attributes:
        schema_version: Schema version for forward-compatibility.
        section_id: The section this output corresponds to.
        summary: One-paragraph section summary.
        key_claims: Bullet-point list of key claims.
        term_definitions: List of ``{"term": ..., "definition": ...}`` dicts.
        summary_skim: 1-2 sentence skim-level summary for the summary toggle.
        summary_read: One-paragraph read-level summary for the summary toggle.
        summary_deep: Comprehensive deep-level summary for the summary toggle.
        margin_notes: Short free-text margin annotations emitted as ``<aside>``
            elements alongside the main body.
    """

    schema_version: str = "1.0"
    section_id: str
    summary: str = ""
    key_claims: list[str] = Field(default_factory=list)
    term_definitions: list[dict[str, str]] = Field(default_factory=list)
    summary_skim: str = ""
    summary_read: str = ""
    summary_deep: str = ""
    margin_notes: list[str] = Field(default_factory=list)

    @field_validator("summary_skim", "summary_read", "summary_deep", mode="before")
    @classmethod
    def _coerce_summary_none(cls, v: object) -> str:
        """Coerce ``None`` from LLM payloads to empty string."""
        if v is None:
            return ""
        return v  # type: ignore[return-value]

    @field_validator("margin_notes", mode="before")
    @classmethod
    def _coerce_margin_notes(cls, v: object) -> list[str]:
        """Coerce ``None`` to empty list and drop non-string entries."""
        if v is None:
            return []
        if isinstance(v, list):
            return [item for item in v if isinstance(item, str)]
        return []


# ---------------------------------------------------------------------------
# Pipeline result
# ---------------------------------------------------------------------------


class SectionFailure(_PipelineBase):
    """Record of a section that could not be processed.

    Attributes:
        section_id: The failing section id.
        error: Human-readable error description.
    """

    section_id: str
    error: str


class SynthesisOutput(_PipelineBase):
    overall_summary: str
    key_contributions: list[str]
    terminology: dict[str, str]


class PipelineResult(_PipelineBase):
    """Top-level output of ``OrchestratorRunner.run()``.

    Downstream tickets (007, 008, 009) consume this type directly.

    Attributes:
        schema_version: Schema version for cache invalidation.
        doc_id: Stable document identifier (arXiv id or SHA-256 prefix).
        provider: Provider name used for this run.
        orchestrator_model: Model used for planning and synthesis.
        worker_model: Model used for section workers.
        section_analyses: One ``WorkerOutput`` per successfully processed section.
        failed_sections: Sections that failed (partial results are NOT cached).
        overall_summary: Orchestrator synthesis: overall paper summary.
        key_contributions: Orchestrator synthesis: top-level contributions list.
        terminology: Orchestrator synthesis: merged terminology map.
        total_input_tokens: Aggregate input tokens across all provider calls.
        total_output_tokens: Aggregate output tokens across all provider calls.
        estimated_cost_usd: Total estimated cost in USD.
        figures: All document figures (carried for ticket 007 figure vision).
    """

    schema_version: str = "1.0"
    doc_id: str
    provider: str
    orchestrator_model: str
    worker_model: str
    section_analyses: list[WorkerOutput] = Field(default_factory=list)
    failed_sections: list[SectionFailure] = Field(default_factory=list)
    overall_summary: str = ""
    key_contributions: list[str] = Field(default_factory=list)
    terminology: dict[str, str] = Field(default_factory=dict)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    estimated_cost_usd: float = 0.0
    figures: list[FigureContext] = Field(default_factory=list)
    planning_complete: bool = False
    synthesis_complete: bool = False
    workers_complete: bool = False
    complete: bool = False
    warnings: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Pipeline configuration
# ---------------------------------------------------------------------------


class PipelineConfig(_PipelineBase):
    """Configuration for one pipeline run.

    Attributes:
        orchestrator_model: Model for orchestrator planning and synthesis.
        worker_model: Model for section workers.
        max_cost_usd: Estimated spending admission cap, not an invoice guarantee.
            ``None`` means unlimited.
        provider_name: ``"claude"`` or ``"openai"``.
        prompt_schema_version: Version of prompt templates (part of cache key).
    """

    orchestrator_model: str = "claude-opus-4-8"
    worker_model: str = "claude-haiku-4-5"
    max_cost_usd: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    request_timeout_seconds: float = Field(default=60.0, gt=0, allow_inf_nan=False)
    max_concurrency: int = Field(default=4, gt=0, strict=True)
    max_output_tokens: int = Field(default=8192, gt=0, strict=True)
    max_synthesis_bytes: int = Field(default=1_000_000, gt=0)
    provider_name: str = "claude"
    prompt_schema_version: str = "1.1"
