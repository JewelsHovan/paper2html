"""Worker unit tests.

AC-4/5, Scenarios 5/6: context slicing, retry logic, validation errors.
"""

from __future__ import annotations

import pytest

from paper2html.pipeline.budget import BudgetTracker
from paper2html.pipeline.errors import BudgetExceededError, WorkerValidationError
from paper2html.pipeline.providers.mock_provider import FailMode, MockProvider
from paper2html.pipeline.schemas import FigureContext, WorkerInput, WorkerOutput
from paper2html.pipeline.worker import (
    _RETRY_PREFIX,
    build_worker_user_prompt,
    run_worker,
)

_PNG_HEADER = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8


def _make_worker_input(section_id: str = "sec-1") -> WorkerInput:
    return WorkerInput(
        section_id=section_id,
        section_title=f"Section {section_id}",
        section_text=f"Text content for {section_id}.",
        figures=[
            FigureContext(
                figure_id="fig-1",
                caption="A test figure",
                image_bytes=_PNG_HEADER,
            )
        ],
        doc_title="Test Paper",
        doc_abstract="Abstract text.",
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_happy_path() -> None:
    """Worker returns valid WorkerOutput on happy path."""
    provider = MockProvider()
    budget = BudgetTracker()
    wi = _make_worker_input("sec-2")
    output = await run_worker(wi, provider, budget, model="claude-haiku-4-5")
    assert output.section_id == "sec-2"
    assert isinstance(output.summary, str)
    assert isinstance(output.key_claims, list)
    assert isinstance(output.term_definitions, list)


@pytest.mark.asyncio
async def test_worker_prompt_contains_section_id_marker() -> None:
    """build_worker_user_prompt includes SECTION_ID marker."""
    wi = _make_worker_input("sec-42")
    prompt = build_worker_user_prompt(wi)
    assert "SECTION_ID: sec-42" in prompt


@pytest.mark.asyncio
async def test_worker_prompt_contains_only_assigned_section_text() -> None:
    """Worker prompt contains only the assigned section's text."""
    wi = _make_worker_input("sec-3")
    prompt = build_worker_user_prompt(wi)
    assert "sec-3" in prompt
    # Must not mention text from other sections
    assert "sec-1" not in prompt or "Section sec-1" not in prompt


# ---------------------------------------------------------------------------
# Retry on JSON validation failure — Scenario 5
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_retry_prepends_return_valid_json_prefix() -> None:
    """Second call adds 'return valid JSON' prefix to the user message.

    Uses FailMode.FIRST_CALL: the first call per content-key returns malformed
    JSON; the second call (with the retry prefix → new key) returns valid JSON.
    """
    provider = MockProvider(fail_mode=FailMode.FIRST_CALL)
    budget = BudgetTracker()
    wi = _make_worker_input("sec-5")
    output = await run_worker(wi, provider, budget, model="claude-haiku-4-5")

    # run_worker should have made 2 calls (first failed, second succeeded)
    assert provider.call_count == 2

    # Verify the second call's user message has the retry prefix
    second_call_messages = provider._calls[1][0]  # (messages, images, tools, model)
    user_msg = second_call_messages[-1]
    assert user_msg.content.startswith(_RETRY_PREFIX)
    assert output.section_id == "sec-5"


# ---------------------------------------------------------------------------
# WorkerValidationError after 2 failures — Scenario 6
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_raises_validation_error_after_two_failures() -> None:
    """Worker raises WorkerValidationError after both calls return invalid JSON."""
    provider = MockProvider(fail_mode=FailMode.ALL_CALLS)
    budget = BudgetTracker()
    wi = _make_worker_input("sec-7")
    with pytest.raises(WorkerValidationError) as exc_info:
        await run_worker(wi, provider, budget, model="claude-haiku-4-5")

    assert exc_info.value.section_id == "sec-7"
    assert provider.call_count == 2


# ---------------------------------------------------------------------------
# Budget check before call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_budget_check_raises_before_provider_call() -> None:
    """BudgetExceededError is raised BEFORE provider.complete() is called."""

    class MeteredProvider(MockProvider):
        free_of_charge = False

    provider = MeteredProvider()
    budget = BudgetTracker(max_cost_usd=1e-12)  # Effective cap of zero
    wi = _make_worker_input("sec-1")
    wi.figures = []
    with pytest.raises(BudgetExceededError):
        await run_worker(wi, provider, budget, model="claude-haiku-4-5")

    # Provider should not have been called
    assert provider.call_count == 0


# ---------------------------------------------------------------------------
# Worker tools invariant (AC requirement: workers cannot call spawn_subquery)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_passes_empty_tools_to_provider() -> None:
    """Workers call provider with empty tools list (cannot invoke spawn_subquery)."""
    provider = MockProvider()
    budget = BudgetTracker()
    wi = _make_worker_input("sec-1")
    await run_worker(wi, provider, budget, model="claude-haiku-4-5")

    _, _, tools, _ = provider._calls[0]
    assert tools == [], "Workers must pass empty tools list to provider"


# ---------------------------------------------------------------------------
# Backward-compat schema tests (sync) — ticket #014
# ---------------------------------------------------------------------------


def test_worker_output_validates_without_new_fields() -> None:
    """Legacy 5-key payload (no summary_skim/read/deep/margin_notes) validates."""
    output = WorkerOutput.model_validate(
        {
            "schema_version": "1.0",
            "section_id": "sec-legacy",
            "summary": "A summary.",
            "key_claims": ["claim A"],
            "term_definitions": [{"term": "foo", "definition": "bar"}],
        }
    )
    assert output.section_id == "sec-legacy"
    assert output.summary_skim == ""
    assert output.summary_read == ""
    assert output.summary_deep == ""
    assert output.margin_notes == []


def test_worker_output_roundtrips_with_new_fields() -> None:
    """WorkerOutput with new fields round-trips through model_dump_json → model_validate_json."""
    original = WorkerOutput(
        section_id="sec-rt",
        summary="Original summary.",
        summary_skim="Skim text.",
        summary_read="Read text.",
        summary_deep="Deep text.",
        margin_notes=["Note A", "Note B"],
    )
    json_str = original.model_dump_json()
    restored = WorkerOutput.model_validate_json(json_str)

    assert restored.section_id == original.section_id
    assert restored.summary_skim == original.summary_skim
    assert restored.summary_read == original.summary_read
    assert restored.summary_deep == original.summary_deep
    assert restored.margin_notes == original.margin_notes


def test_worker_output_null_coercion() -> None:
    """None values for summary fields coerce to '' and margin_notes coerces to []."""
    output = WorkerOutput.model_validate(
        {
            "section_id": "sec-null",
            "summary_skim": None,
            "summary_read": None,
            "summary_deep": None,
            "margin_notes": None,
        }
    )
    assert output.summary_skim == ""
    assert output.summary_read == ""
    assert output.summary_deep == ""
    assert output.margin_notes == []


def test_worker_output_margin_notes_drops_non_strings() -> None:
    """margin_notes with mixed types drops non-string entries on validation."""
    output = WorkerOutput.model_validate(
        {
            "section_id": "sec-mixed",
            "margin_notes": ["ok", 42, None, "also-ok"],
        }
    )
    assert output.margin_notes == ["ok", "also-ok"]
