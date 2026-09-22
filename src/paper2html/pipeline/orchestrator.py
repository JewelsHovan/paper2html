"""Evidence-preserving planning, bounded worker dispatch and validated synthesis.

Planning supplies focus guidance only. Full authoritative section text is always
retained. Stage completeness is explicit; only fully complete, validated results
are cached under a versioned, configuration-sensitive namespace. Every model
attempt enters the budget's atomic admission boundary.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from io import StringIO
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError
from rich.console import Console
from rich.progress import BarColumn, Progress, TaskID, TextColumn, TimeElapsedColumn

from paper2html.pipeline._json import loads_lenient
from paper2html.pipeline.budget import BudgetTracker
from paper2html.pipeline.errors import BudgetExceededError, ProviderError
from paper2html.pipeline.providers.base import Message, ProviderInterface, ToolDef
from paper2html.pipeline.schemas import (
    FigureContext,
    PipelineConfig,
    PipelineResult,
    SectionFailure,
    SynthesisOutput,
    WorkerInput,
    WorkerOutput,
)
from paper2html.pipeline.worker import run_worker

if TYPE_CHECKING:
    from paper2html.models.document import NormalizedDocument

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# spawn_subquery tool definition
# ---------------------------------------------------------------------------

SPAWN_SUBQUERY_TOOL = ToolDef(
    name="spawn_subquery",
    description=(
        "Add focus guidance for a worker that retains the full authoritative section. "
        "Only the orchestrator may call this tool; workers cannot spawn further workers."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "section_id": {
                "type": "string",
                "description": "The section id to re-analyse with narrowed context.",
            },
            "focus_query": {
                "type": "string",
                "description": "Focused question for the sub-worker.",
            },
        },
        "required": ["section_id", "focus_query"],
    },
)

# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

_ORCHESTRATOR_SYSTEM_PROMPT = (
    "You are a research paper orchestrator. "
    "Given a list of section ids and titles, your job is to plan the analysis. "
    "You may call spawn_subquery for any section that requires deeper focused analysis. "
    "Return your plan as plain text or invoke spawn_subquery tools as needed."
)

_SYNTHESIS_SYSTEM_PROMPT = (
    "You are a research paper synthesiser. "
    "Given JSON summaries from all sections, produce an overall synthesis. "
    "Return a JSON object with keys: overall_summary (string), "
    "key_contributions (list of strings), terminology (dict string→string). "
    "Return ONLY the JSON object, with no surrounding prose or code fences."
)

_SYNTHESIS_RETRY_PREFIX = (
    "IMPORTANT: return valid JSON matching the schema exactly, with no prose "
    "or code fences. Your previous response could not be parsed as JSON.\n"
)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class OrchestratorRunner:
    """Orchestrates the full planning → worker dispatch → synthesis pipeline.

    Args:
        provider: Provider instance (shared by orchestrator and workers).
        config: Pipeline configuration.
        budget: Pre-constructed ``BudgetTracker``.
        cache: Optional ``diskcache.Cache`` for result caching.
        console: Optional Rich ``Console`` for progress output.
    """

    def __init__(
        self,
        provider: ProviderInterface,
        config: PipelineConfig,
        budget: BudgetTracker,
        cache: Any = None,  # diskcache.Cache | None
        console: Console | None = None,
    ) -> None:
        self._provider = provider
        self._config = config
        self._budget = budget
        self._budget.max_concurrency = config.max_concurrency
        self._cache = cache
        self._console = console or Console(file=StringIO(), force_terminal=False)

    async def run(self, document: "NormalizedDocument") -> PipelineResult:
        """Run the full pipeline on ``document``.

        Args:
            document: Normalised document to analyse.

        Returns:
            Validated ``PipelineResult``.

        Raises:
            BudgetExceededError: If the budget cap is breached at any stage.
        """
        for model in (self._config.orchestrator_model, self._config.worker_model):
            self._budget.validate_model(self._provider, model)
        serialized = document.model_dump_json()
        doc_id = (
            document.metadata.arxiv_id
            or hashlib.sha256(serialized.encode()).hexdigest()[:16]
        )
        cache_key = self._make_cache_key(document, serialized)
        warnings = []
        planning_complete = False
        synthesis_complete = False

        # ------------------------------------------------------------------
        # 1. Cache check
        # ------------------------------------------------------------------
        if self._cache is not None:
            cached = self._cache.get(cache_key)
            if cached is not None:
                logger.info("OrchestratorRunner: cache hit for key %s", cache_key)
                try:
                    hit = PipelineResult.model_validate_json(cached)
                    if hit.complete:
                        return hit
                except (ValueError, TypeError):
                    logger.warning("Ignoring corrupt pipeline cache")

        # ------------------------------------------------------------------
        # 2. Build WorkerInputs (context slicing)
        # ------------------------------------------------------------------
        doc_title = document.metadata.title
        doc_abstract = document.metadata.abstract
        all_figures = list(document.figures.values())

        # Collect all FigureContext objects for PipelineResult.figures field
        pipeline_figures: list[FigureContext] = [
            FigureContext(
                figure_id=fig.id,
                caption=fig.caption,
                image_bytes=fig.image_bytes,
                image_mime_type=fig.image_mime_type,
                referencing_sentence_ids=list(fig.referencing_sentence_ids),
            )
            for fig in all_figures
        ]

        # section_id → WorkerInput mapping
        section_inputs: dict[str, WorkerInput] = {}
        for section in document.sections:
            figs = [
                FigureContext(
                    figure_id=document.figures[fid].id,
                    caption=document.figures[fid].caption,
                    image_bytes=document.figures[fid].image_bytes,
                    image_mime_type=document.figures[fid].image_mime_type,
                    referencing_sentence_ids=list(
                        document.figures[fid].referencing_sentence_ids
                    ),
                )
                for fid in section.figure_ids
                if fid in document.figures
            ]
            section_inputs[section.id] = WorkerInput(
                section_id=section.id,
                section_title=section.title,
                section_text=section.text_content,
                figures=figs,
                doc_title=doc_title,
                doc_abstract=doc_abstract,
            )

        # ------------------------------------------------------------------
        # 3. Planning call (budget-checked BEFORE call)
        # ------------------------------------------------------------------
        planning_prompt = self._build_planning_prompt(document)
        planning_msg = Message(role="user", content=planning_prompt)

        subquery_inputs: dict[str, WorkerInput] = {}
        try:
            plan_response = await self._budget.complete(
                self._provider,
                messages=[
                    Message(role="system", content=_ORCHESTRATOR_SYSTEM_PROMPT),
                    planning_msg,
                ],
                images=[],
                tools=[SPAWN_SUBQUERY_TOOL],
                model=self._config.orchestrator_model,
                max_output_tokens=self._config.max_output_tokens,
                request_timeout_seconds=self._config.request_timeout_seconds,
            )
            planning_complete = True

            # Handle spawn_subquery tool calls from the planning step
            if plan_response.stop_reason == "tool_use":
                for tc in plan_response.tool_calls:
                    if tc.name != "spawn_subquery":
                        planning_complete = False
                        warnings.append("Ignored unsupported planning tool")
                        continue
                    args = tc.arguments if isinstance(tc.arguments, dict) else {}
                    sid = args.get("section_id")
                    focus = args.get("focus_query")
                    if (
                        not isinstance(sid, str)
                        or sid not in section_inputs
                        or not isinstance(focus, str)
                        or not focus.strip()
                        or len(focus) > 4000
                    ):
                        warnings.append("Ignored malformed or unknown planning tool")
                        planning_complete = False
                        logger.warning("spawn_subquery: unknown section_id '%s'", sid)
                        continue
                    parent_input = section_inputs[sid]
                    subquery_inputs[sid] = WorkerInput(
                        section_id=sid,
                        section_title=parent_input.section_title,
                        section_text=parent_input.section_text,
                        focus_query=focus,
                        figures=parent_input.figures,
                        doc_title=doc_title,
                        doc_abstract=doc_abstract,
                    )

        except BudgetExceededError:
            raise  # Re-raise immediately; no cache write
        except (ProviderError, TimeoutError) as exc:
            warnings.append(f"Planning failed: {exc}")
            # Planning call ProviderError: log warning, fall back to flat dispatch
            logger.warning(
                "Planning call ProviderError (falling back to flat dispatch): %s", exc
            )

        # ------------------------------------------------------------------
        # 4. Dispatch workers in parallel
        # ------------------------------------------------------------------
        # For sections with a subquery, use the subquery input
        dispatch_inputs = {
            sid: (subquery_inputs[sid] if sid in subquery_inputs else wi)
            for sid, wi in section_inputs.items()
        }

        section_ids = [s.id for s in document.sections]

        with Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TimeElapsedColumn(),
            console=self._console,
            transient=False,
        ) as progress:
            task_id: TaskID = progress.add_task(
                "Analysing sections…", total=len(section_ids)
            )

            async def _run_one(sid: str) -> WorkerOutput:
                # Advance in finally so the bar reaches 100% even when a worker
                # raises (e.g. WorkerValidationError / ProviderError). Otherwise a
                # wave where every section fails leaves the bar pinned at 0%,
                # masking that work was actually attempted.
                try:
                    result = await run_worker(
                        dispatch_inputs[sid],
                        self._provider,
                        self._budget,
                        model=self._config.worker_model,
                        max_output_tokens=self._config.max_output_tokens,
                        request_timeout_seconds=self._config.request_timeout_seconds,
                    )
                    progress.print(f"  cost so far: ${self._budget.total_cost_usd:.6f}")
                    return result
                finally:
                    progress.advance(task_id)

            raw_results = await asyncio.gather(
                *(_run_one(sid) for sid in section_ids),
                return_exceptions=True,
            )

        # ------------------------------------------------------------------
        # 5. Sweep for BudgetExceededError FIRST (re-raise, no cache write)
        # ------------------------------------------------------------------
        for res in raw_results:
            if isinstance(res, BudgetExceededError):
                raise res

        # ------------------------------------------------------------------
        # 6. Collect results and failures
        # ------------------------------------------------------------------
        section_analyses: list[WorkerOutput] = []
        failed_sections: list[SectionFailure] = []

        for sid, res in zip(section_ids, raw_results):
            if isinstance(res, BaseException):
                failed_sections.append(SectionFailure(section_id=sid, error=str(res)))
            else:
                # Merge subquery outputs into the parent section's result
                if sid in subquery_inputs:
                    res = self._merge_subquery(res)
                section_analyses.append(res)

        # ------------------------------------------------------------------
        # 7. Synthesis call (budget-checked BEFORE call)
        # ------------------------------------------------------------------
        overall_summary = ""
        key_contributions: list[str] = []
        terminology: dict[str, str] = {}

        synthesis_input = json.dumps(
            [a.model_dump() for a in section_analyses], default=str
        )
        synth_messages = [
            Message(role="system", content=_SYNTHESIS_SYSTEM_PROMPT),
            Message(
                role="user",
                content=(
                    f"SYNTHESIS\n\nSection analyses:\n{synthesis_input}\n\n"
                    "Produce the overall synthesis as JSON."
                ),
            ),
        ]

        # Mirror the worker's parse-then-retry-once contract: a model that wraps
        # its JSON in fences is handled by loads_lenient without a round-trip; a
        # genuinely malformed response triggers ONE retry with a corrective
        # prefix before falling back to empty synthesis fields (fail-soft —
        # the run still returns a PipelineResult).
        oversized = (
            sum(len(m.content.encode()) for m in synth_messages)
            + len(_SYNTHESIS_RETRY_PREFIX.encode())
        ) > self._config.max_synthesis_bytes
        if oversized:
            warnings.append(
                "Synthesis payload exceeds configured bound; evidence was not truncated"
            )
        for attempt in range(0 if oversized else 2):
            try:
                synth_response = await self._budget.complete(
                    self._provider,
                    messages=synth_messages,
                    images=[],
                    tools=[],
                    model=self._config.orchestrator_model,
                    max_output_tokens=self._config.max_output_tokens,
                    request_timeout_seconds=self._config.request_timeout_seconds,
                )
                synth_data = SynthesisOutput.model_validate(
                    loads_lenient(synth_response.content)
                )
                overall_summary = synth_data.overall_summary
                key_contributions = synth_data.key_contributions
                terminology = synth_data.terminology
                synthesis_complete = True
                break
            except BudgetExceededError:
                raise  # Re-raise — no cache write
            except (ValueError, ValidationError) as exc:
                if attempt == 0:
                    logger.warning(
                        "Synthesis returned unparseable JSON (attempt 1/2): %s"
                        " — retrying",
                        exc,
                    )
                    synth_messages[-1] = Message(
                        role="user",
                        content=_SYNTHESIS_RETRY_PREFIX + synth_messages[-1].content,
                    )
                    continue
                logger.warning(
                    "Synthesis failed (returning empty synthesis fields): %s", exc
                )
            except Exception as exc:  # noqa: BLE001 — fail-soft, never abort the run
                logger.warning(
                    "Synthesis failed (returning empty synthesis fields): %s", exc
                )
                break

        # ------------------------------------------------------------------
        # 8. Build PipelineResult
        # ------------------------------------------------------------------
        if not synthesis_complete:
            warnings.append("Synthesis incomplete")
        if self._budget.uncertain_cost_usd:
            warnings.append(
                f"Dispatched calls with unknown usage retain ${self._budget.uncertain_cost_usd:.6f} estimated liability"
            )
        budget_summary = self._budget.summary()
        result = PipelineResult(
            doc_id=doc_id,
            provider=self._config.provider_name,
            orchestrator_model=self._config.orchestrator_model,
            worker_model=self._config.worker_model,
            section_analyses=section_analyses,
            failed_sections=failed_sections,
            overall_summary=overall_summary,
            key_contributions=key_contributions,
            terminology=terminology,
            total_input_tokens=int(budget_summary["total_input_tokens"]),
            total_output_tokens=int(budget_summary["total_output_tokens"]),
            estimated_cost_usd=float(budget_summary["total_cost_usd"]),
            figures=pipeline_figures,
            planning_complete=planning_complete,
            synthesis_complete=synthesis_complete,
            workers_complete=not failed_sections,
            complete=planning_complete
            and synthesis_complete
            and not failed_sections
            and not self._budget.uncertain_cost_usd,
            warnings=warnings,
        )

        # ------------------------------------------------------------------
        # 9. Cache write — ONLY when no failures and no partial results
        # ------------------------------------------------------------------
        if self._cache is not None and result.complete:
            self._cache.set(cache_key, result.model_dump_json())

        return result

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _make_cache_key(
        self, document: "NormalizedDocument", serialized: str | None = None
    ) -> str:
        """Build a deterministic cache key for this run.

        Key components: doc_json + provider_name + orchestrator_model +
        worker_model + prompt_schema_version.

        Args:
            document: The normalised document.

        Returns:
            Hex-digest cache key string.
        """
        raw = (
            "pipeline-v3:"
            + (serialized or document.model_dump_json())
            + self._config.model_dump_json(
                include={
                    "provider_name",
                    "orchestrator_model",
                    "worker_model",
                    "prompt_schema_version",
                    "max_output_tokens",
                    "max_synthesis_bytes",
                }
            )
        )
        return hashlib.sha256(raw.encode()).hexdigest()

    @staticmethod
    def _build_planning_prompt(document: "NormalizedDocument") -> str:
        """Build the planning-phase user prompt.

        Args:
            document: The normalised document.

        Returns:
            Planning prompt string containing ``PLANNING`` marker plus
            section ids and titles.
        """
        lines = [
            "PLANNING",
            "",
            f"Document: {document.metadata.title}",
            "",
            "Sections:",
        ]
        for sec in document.sections:
            lines.append(f"  {sec.id}: {sec.title}")
        lines.append(
            "\nDecide if any sections need spawn_subquery for deeper analysis."
        )
        return "\n".join(lines)

    @staticmethod
    def _merge_subquery(output: WorkerOutput) -> WorkerOutput:
        """Merge a sub-worker output into the parent section output.

        Currently a no-op since the sub-query result IS the primary output
        (dispatched in place of the parent).  The sub-output's key_claims
        and term_definitions are already part of the returned WorkerOutput.

        Args:
            output: The sub-query ``WorkerOutput``.

        Returns:
            The same output (pass-through in v1).
        """
        return output
