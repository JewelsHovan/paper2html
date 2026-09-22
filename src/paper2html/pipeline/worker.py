"""Section worker: receives a sliced ``WorkerInput``, calls the provider, validates output.

Design notes
------------
* The user prompt contains a ``SECTION_ID: <id>`` marker so the ``MockProvider``
  (and real providers during debugging) can echo the section id back.
* On JSON validation failure the worker retries ONCE with a
  ``"IMPORTANT: return valid JSON matching the schema.\\n"`` prefix prepended to
  the last user message (``messages[-1]``).  The retry prefix changes the hash,
  so the ``MockProvider`` with ``FailMode.FIRST_CALL`` returns valid JSON on
  the second call.
* ``budget.complete()`` admits and reserves every provider attempt.
* Worker raises ``WorkerValidationError`` (NOT ``ProviderError``) after 2
  consecutive validation failures.
"""

from __future__ import annotations

import logging

from pydantic import ValidationError

from paper2html.pipeline._json import loads_lenient
from paper2html.pipeline.budget import BudgetTracker
from paper2html.pipeline.errors import WorkerValidationError
from paper2html.pipeline.providers.base import Message, ProviderInterface
from paper2html.pipeline.schemas import WorkerInput, WorkerOutput

logger = logging.getLogger(__name__)

WORKER_SYSTEM_PROMPT: str = (
    "You are a precise academic paper analysis assistant.\n"
    "You receive one section of a research paper and must return a structured JSON"
    " analysis of that section only.\n"
    "Your response MUST be valid JSON with exactly the following keys:\n"
    '  "schema_version": "1.0",\n'
    '  "section_id": <string>,\n'
    '  "summary": <string>,\n'
    '  "key_claims": [<string>, ...],\n'
    '  "term_definitions": [{"term": <string>, "definition": <string>}, ...],\n'
    '  "summary_skim": <string, a 1-2 sentence skim-level summary>,\n'
    '  "summary_read": <string, a one-paragraph read-level summary>,\n'
    '  "summary_deep": <string, a comprehensive deep-level summary>,\n'
    '  "margin_notes": [<string, short margin annotation>, ...]\n'
    "Do not include any text outside the JSON object."
)

_RETRY_PREFIX: str = (
    "IMPORTANT: return valid JSON matching the schema exactly. "
    "Your previous response was not valid JSON.\n"
)


def build_worker_user_prompt(worker_input: WorkerInput) -> str:
    """Build the user message content for a section worker.

    The prompt embeds the ``SECTION_ID`` marker so providers can extract the
    section id from the raw content.

    Args:
        worker_input: Sliced context for this worker.

    Returns:
        User message content string.
    """
    parts: list[str] = [
        f"SECTION_ID: {worker_input.section_id}",
        f"Document: {worker_input.doc_title}",
        "",
        f"Section: {worker_input.section_title}",
        "",
        worker_input.section_text,
    ]
    if worker_input.focus_query:
        parts.append(
            f"Focus guidance (retain all section evidence): {worker_input.focus_query}"
        )
    if worker_input.figures:
        parts.append("")
        parts.append("Figures referenced in this section:")
        for fig in worker_input.figures:
            parts.append(f"  - {fig.figure_id}: {fig.caption}")

    parts.append("")
    parts.append(
        "Analyse this section and return a JSON object as described in the system prompt."
    )
    return "\n".join(parts)


async def run_worker(
    worker_input: WorkerInput,
    provider: ProviderInterface,
    budget: BudgetTracker,
    *,
    model: str,
    max_output_tokens: int = 8192,
    request_timeout_seconds: float = 60.0,
) -> WorkerOutput:
    """Run one section worker.

    Args:
        worker_input: Sliced context (one section + its figures only).
        provider: Provider to call.
        budget: Atomic admission and reservation tracker.
        model: Worker model identifier.

    Returns:
        Validated ``WorkerOutput``.

    Raises:
        WorkerValidationError: If two consecutive calls return invalid JSON.
        BudgetExceededError: If the budget cap would be exceeded.
        ProviderError: If the provider call fails after retries.
    """
    system_msg = Message(role="system", content=WORKER_SYSTEM_PROMPT)
    user_content = build_worker_user_prompt(worker_input)
    user_msg = Message(role="user", content=user_content)
    messages = [system_msg, user_msg]

    figure_bytes: list[bytes] = [
        fig.image_bytes for fig in worker_input.figures if fig.image_bytes
    ]

    for attempt in range(2):
        # Budget check BEFORE every provider call

        response = await budget.complete(
            provider,
            messages=messages,
            images=figure_bytes,
            tools=[],
            model=model,
            max_output_tokens=max_output_tokens,
            request_timeout_seconds=request_timeout_seconds,
        )

        try:
            parsed = loads_lenient(response.content)
            output = WorkerOutput.model_validate(parsed)
            if output.section_id != worker_input.section_id:
                raise ValueError("Worker section identity mismatch")
            return output
        except (ValueError, ValidationError) as exc:
            logger.warning(
                "Worker validation failure for section '%s' (attempt %d/2): %s",
                worker_input.section_id,
                attempt + 1,
                exc,
            )
            if attempt == 0:
                # Retry: prepend the "return valid JSON" prefix to the user message
                retry_content = _RETRY_PREFIX + messages[-1].content
                messages[-1] = Message(role="user", content=retry_content)
            else:
                raise WorkerValidationError(
                    section_id=worker_input.section_id,
                    raw_response=response.content,
                )

    # Should never reach here
    raise WorkerValidationError(  # pragma: no cover
        section_id=worker_input.section_id, raw_response=""
    )
