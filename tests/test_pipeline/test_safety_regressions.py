"""Offline regressions for evidence, validation and estimated spend admission."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from paper2html.pipeline._backoff import backoff_retry
from paper2html.pipeline.budget import BudgetTracker
from paper2html.pipeline.errors import BudgetExceededError, WorkerValidationError
from paper2html.pipeline.orchestrator import OrchestratorRunner
from paper2html.pipeline.providers.base import (
    Message,
    ProviderResponse,
    ToolCall,
    ToolDef,
    normalize_tool_arguments,
)
from paper2html.pipeline.providers.mock_provider import MockProvider
from paper2html.pipeline.schemas import PipelineConfig, WorkerInput
from paper2html.pipeline.worker import run_worker


class Scripted(MockProvider):
    def __init__(self, synthesis=None, tool=None):
        super().__init__()
        self.synthesis = synthesis
        self.tool = tool
        self.synth_calls = 0

    async def complete(
        self,
        messages,
        images,
        tools,
        *,
        model,
        max_output_tokens=8192,
        transport_retries=True,
        request_timeout_seconds=60.0,
    ):
        if "SYNTHESIS" in messages[-1].content and self.synthesis is not None:
            self.synth_calls += 1
            return ProviderResponse(content=self.synthesis)
        if tools and self.tool is not None:
            return ProviderResponse(
                content="",
                stop_reason="tool_use",
                tool_calls=[ToolCall("t", "spawn_subquery", self.tool)],
            )
        return await super().complete(
            messages, images, tools, model=model, max_output_tokens=max_output_tokens
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        "null",
        "[]",
        '"x"',
        '{"overall_summary":null}',
        '{"overall_summary": [], "key_contributions": 2, "terminology": []}',
        "bad",
    ],
)
async def test_synthesis_schema_retries_incomplete_not_cached(
    payload, synthetic_document, tmp_cache
):
    provider = Scripted(synthesis=payload)
    result = await OrchestratorRunner(
        provider, PipelineConfig(), BudgetTracker(), tmp_cache
    ).run(synthetic_document)
    assert provider.synth_calls == 2
    assert not result.complete and not result.synthesis_complete
    assert result.warnings and result.overall_summary == ""
    assert not list(tmp_cache)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args",
    [
        {"section_id": []},
        {"section_id": "unknown"},
        {"section_id": "sec-2", "focus_query": []},
        [1],
    ],
)
async def test_bad_planning_tool_fail_soft(args, synthetic_document):
    result = await OrchestratorRunner(
        Scripted(tool=args), PipelineConfig(), BudgetTracker()
    ).run(synthetic_document)
    assert len(result.section_analyses) == len(synthetic_document.sections)
    assert not result.planning_complete and result.warnings


@pytest.mark.asyncio
async def test_cache_corruption_and_old_incomplete_recovered(
    synthetic_document, tmp_cache
):
    provider = MockProvider()
    runner = OrchestratorRunner(provider, PipelineConfig(), BudgetTracker(), tmp_cache)
    key = runner._make_cache_key(synthetic_document)
    tmp_cache.set(key, '{"garbage":')
    result = await runner.run(synthetic_document)
    assert result.complete
    result.complete = False
    tmp_cache.set(key, result.model_dump_json())
    previous = provider.call_count
    assert (await runner.run(synthetic_document)).complete
    assert provider.call_count > previous


@pytest.mark.asyncio
async def test_synthesis_bound_never_silently_truncates(synthetic_document, tmp_cache):
    provider = Scripted(synthesis="null")
    result = await OrchestratorRunner(
        provider, PipelineConfig(max_synthesis_bytes=1), BudgetTracker(), tmp_cache
    ).run(synthetic_document)
    assert provider.synth_calls == 0
    assert result.section_analyses and not result.complete and not list(tmp_cache)
    assert any("not truncated" in warning for warning in result.warnings)


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", ["other", "unknown"])
async def test_worker_identity_retry_failure_keeps_original(identity):
    calls = []

    async def complete(**kwargs):
        calls.append(kwargs)
        return ProviderResponse(content=json.dumps({"section_id": identity}))

    with pytest.raises(WorkerValidationError) as error:
        await run_worker(
            WorkerInput(section_id="assigned", section_text="evidence"),
            SimpleNamespace(free_of_charge=False, complete=complete),
            BudgetTracker(),
            model="gpt-4o",
            max_output_tokens=123,
        )
    assert error.value.section_id == "assigned"
    assert len(calls) == 2 and all(call["max_output_tokens"] == 123 for call in calls)


def kwargs(**overrides):
    return dict(
        messages=[Message("user", "hello")],
        images=[],
        tools=[],
        model="gpt-4o",
        max_output_tokens=10,
        **overrides,
    )


@pytest.mark.asyncio
async def test_atomic_barrier_reservation_and_actual_reconciliation():
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def complete(**kw):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return ProviderResponse(content="", input_tokens=10, output_tokens=2)

    provider = SimpleNamespace(free_of_charge=False, complete=complete)
    budget = BudgetTracker(max_cost_usd=0.002)  # one ~.011 reservation fits; two do not
    first = asyncio.create_task(budget.complete(provider, **kwargs()))
    await entered.wait()
    with pytest.raises(BudgetExceededError):
        await budget.complete(provider, **kwargs())
    assert calls == 1
    release.set()
    await first
    assert budget._reserved == 0
    assert budget.total_cost_usd == pytest.approx(0.00008)
    assert not hasattr(provider, "disable_retries")


@pytest.mark.asyncio
async def test_cancel_before_admission_and_after_dispatch():
    entered = asyncio.Event()

    async def complete(**kw):
        entered.set()
        await asyncio.Event().wait()

    provider = SimpleNamespace(free_of_charge=False, complete=complete)
    budget = BudgetTracker(max_cost_usd=1)
    budget.max_concurrency = 1
    first = asyncio.create_task(budget.complete(provider, **kwargs()))
    await entered.wait()
    reserved = budget._reserved
    queued = asyncio.create_task(budget.complete(provider, **kwargs()))
    await asyncio.sleep(0)
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    assert budget._reserved == reserved
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert budget._reserved == reserved  # dispatched usage unknown; not refunded


@pytest.mark.asyncio
async def test_actual_overrun_halts_following_calls():
    calls = 0

    async def complete(**kw):
        nonlocal calls
        calls += 1
        return ProviderResponse(content="", input_tokens=1_000_000)

    budget = BudgetTracker(max_cost_usd=0.1)
    provider = SimpleNamespace(free_of_charge=False, complete=complete)
    for _ in range(2):
        with pytest.raises(BudgetExceededError):
            await budget.complete(provider, **kwargs())
    assert calls == 1 and budget.total_cost_usd == 5


@pytest.mark.asyncio
async def test_system_tools_images_are_reserved():
    async def complete(**kw):
        return ProviderResponse(content="")

    provider = SimpleNamespace(free_of_charge=False, complete=complete)
    for change in [
        dict(messages=[Message("system", "x" * 100000)]),
        dict(tools=[ToolDef("t", "x" * 100000, {})]),
        dict(images=[b"tiny compressed image"]),
    ]:
        request = kwargs()
        request.update(change)
        with pytest.raises((BudgetExceededError, ValueError)):
            await BudgetTracker(max_cost_usd=0.02).complete(provider, **request)


@pytest.mark.asyncio
async def test_concurrency_limit():
    active = peak = 0

    async def complete(**kw):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.001)
        active -= 1
        return ProviderResponse(content="")

    budget = BudgetTracker()
    budget.max_concurrency = 2
    await asyncio.gather(
        *(
            budget.complete(
                SimpleNamespace(free_of_charge=False, complete=complete), **kwargs()
            )
            for _ in range(10)
        )
    )
    assert peak == 2


@pytest.mark.asyncio
async def test_unknown_capped_model_rejected_mock_zero():
    async def complete(**kw):
        pytest.fail("Unknown pricing must not dispatch")

    request = kwargs()
    request["model"] = "unknown"
    with pytest.raises(ValueError, match="Unknown capped pricing"):
        await BudgetTracker(max_cost_usd=1).complete(
            SimpleNamespace(free_of_charge=False, complete=complete), **request
        )
    budget = BudgetTracker(max_cost_usd=1e-12)
    await budget.complete(MockProvider(), **request)
    assert budget.total_cost_usd == 0


@pytest.mark.asyncio
async def test_cache_usage_accounted_without_double_count():
    async def complete(**kw):
        return ProviderResponse(
            content="",
            input_tokens=100,
            cache_read_input_tokens=200,
            cache_creation_input_tokens=300,
            output_tokens=10,
        )

    budget = BudgetTracker()
    await budget.complete(
        SimpleNamespace(free_of_charge=False, complete=complete), **kwargs()
    )
    assert budget.total_input_tokens == 600
    assert budget.total_cost_usd == pytest.approx((900 * 5 + 10 * 15) / 1e6)


@pytest.mark.parametrize("value", [None, [], "null", "[]", "{bad", 1])
def test_tool_normalization(value):
    assert normalize_tool_arguments(value) == {}


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf")])
def test_invalid_limits(value):
    with pytest.raises(ValidationError):
        PipelineConfig(max_cost_usd=value)
    with pytest.raises(ValueError):
        BudgetTracker(max_cost_usd=value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,expected",
    [(400, 1), (401, 1), (404, 1), (408, 3), (409, 3), (429, 3), (500, 3), (503, 3)],
)
async def test_explicit_transient_status_and_bounded_retry_after(status, expected):
    class StatusError(Exception):
        status_code = status
        response = SimpleNamespace(headers={"retry-after": "99999"})

    calls, sleeps = [], []

    async def call():
        calls.append(1)
        raise StatusError()

    async def sleep(delay):
        sleeps.append(delay)

    with pytest.raises(StatusError):
        await backoff_retry(call, (StatusError,), delays=(1, 2), sleep_fn=sleep)
    assert len(calls) == expected
    assert all(delay <= 30 for delay in sleeps)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["openai", "anthropic"])
async def test_provider_forwarded_output_bound_and_malformed_tool_arguments(kind):
    from unittest.mock import AsyncMock
    from paper2html.pipeline.providers.openai_provider import OpenAIProvider
    from paper2html.pipeline.providers.anthropic_provider import AnthropicProvider

    if kind == "openai":
        message = SimpleNamespace(
            content="",
            tool_calls=[
                SimpleNamespace(
                    id="t",
                    function=SimpleNamespace(name="spawn_subquery", arguments="null"),
                )
            ],
        )
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=message, finish_reason="tool_calls")],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=2),
        )
        create = AsyncMock(return_value=response)
        provider = OpenAIProvider(
            client=SimpleNamespace(
                chat=SimpleNamespace(completions=SimpleNamespace(create=create))
            )
        )
    else:
        response = SimpleNamespace(
            content=[
                SimpleNamespace(
                    type="tool_use", id="t", name="spawn_subquery", input="{bad"
                )
            ],
            stop_reason="tool_use",
            usage=SimpleNamespace(
                input_tokens=1,
                output_tokens=2,
                cache_read_input_tokens=3,
                cache_creation_input_tokens=4,
            ),
        )
        create = AsyncMock(return_value=response)
        provider = AnthropicProvider(
            client=SimpleNamespace(messages=SimpleNamespace(create=create))
        )
    response = await provider.complete(
        [Message("user", "test")], [], [], model="test", max_output_tokens=123
    )
    assert create.call_args.kwargs["max_tokens"] == 123
    assert response.tool_calls[0].arguments == {}
    if kind == "anthropic":
        assert response.input_tokens == 1
        assert response.cache_read_input_tokens == 3
        assert response.cache_creation_input_tokens == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["openai", "anthropic"])
async def test_sdk_retry_disabled_and_owned_client_cleanup(kind, monkeypatch):
    from unittest.mock import AsyncMock, Mock
    from paper2html.pipeline.providers.openai_provider import OpenAIProvider
    from paper2html.pipeline.providers.anthropic_provider import AnthropicProvider

    client = SimpleNamespace(close=AsyncMock())
    constructor = Mock(return_value=client)
    monkeypatch.setattr(
        f"{kind}.Async{'OpenAI' if kind == 'openai' else 'Anthropic'}", constructor
    )
    provider_cls = OpenAIProvider if kind == "openai" else AnthropicProvider
    provider = provider_cls(api_key="unused")
    assert constructor.call_args.kwargs["max_retries"] == 0
    assert constructor.call_args.kwargs["timeout"] == 60
    await provider.aclose()
    client.close.assert_awaited_once()
    await provider_cls(client=client).aclose()
    client.close.assert_awaited_once()  # borrowed client was not closed again


@pytest.mark.asyncio
async def test_capped_request_no_internal_transport_retries():
    from unittest.mock import AsyncMock
    import httpx
    import openai
    from paper2html.pipeline.providers.openai_provider import OpenAIProvider
    from paper2html.pipeline.errors import ProviderError

    error = openai.RateLimitError(
        "busy",
        response=httpx.Response(
            429, request=httpx.Request("POST", "https://offline.invalid")
        ),
        body=None,
    )
    create = AsyncMock(side_effect=error)
    provider = OpenAIProvider(
        client=SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )
    )
    budget = BudgetTracker(max_cost_usd=1, retry_delays=())
    with pytest.raises(ProviderError):
        await budget.complete(provider, **kwargs())
    assert create.await_count == 1
    assert budget.uncertain_cost_usd == 0  # explicit 429 rejection is unbilled


@pytest.mark.asyncio
async def test_synthesis_corrective_recovery(synthetic_document):
    class Recovering(Scripted):
        async def complete(self, *args, **kw):
            if self.synth_calls:
                self.synthesis = json.dumps(
                    dict(overall_summary="ok", key_contributions=[], terminology={})
                )
            return await super().complete(*args, **kw)

    provider = Recovering(synthesis="null")
    result = await OrchestratorRunner(provider, PipelineConfig(), BudgetTracker()).run(
        synthetic_document
    )
    assert result.complete and result.overall_summary == "ok"
    assert provider.synth_calls == 2


def test_output_affecting_settings_change_cache_key(synthetic_document):
    configs = [
        PipelineConfig(),
        PipelineConfig(max_output_tokens=1),
        PipelineConfig(max_synthesis_bytes=1),
        PipelineConfig(prompt_schema_version="future"),
    ]
    keys = {
        OrchestratorRunner(MockProvider(), config, BudgetTracker())._make_cache_key(
            synthetic_document
        )
        for config in configs
    }
    assert len(keys) == len(configs)


@pytest.mark.parametrize(
    "option,value",
    [
        ("--max-cost-usd", "nan"),
        ("--max-cost-usd", "inf"),
        ("--max-cost-usd", "0"),
        ("--max-concurrency", "0"),
        ("--max-output-tokens", "-1"),
    ],
)
def test_cli_rejects_invalid_pipeline_limits(option, value):
    from typer.testing import CliRunner
    from paper2html.cli import app

    result = CliRunner().invoke(app, [option, value, "inspect", "test"])
    assert result.exit_code != 0
