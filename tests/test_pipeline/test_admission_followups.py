"""Offline admission policy tests, not live billing benchmarks."""

import asyncio
import io
import random
from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest
from PIL import Image

from paper2html.pipeline._backoff import retry_after_seconds
from paper2html.pipeline._estimates import image_tokens
from paper2html.pipeline.budget import BudgetTracker
from paper2html.pipeline.errors import BudgetExceededError, ProviderError
from paper2html.pipeline.providers.base import (
    ProviderInterface,
    ProviderResponse,
    Message,
)
from paper2html.pipeline.providers.mock_provider import MockProvider
from paper2html.pipeline.schemas import PipelineConfig
from paper2html.pipeline.orchestrator import OrchestratorRunner


class MeteredProvider(ProviderInterface):
    def __init__(self, errors=()):
        self.errors = list(errors)
        self.calls = []
        self.costs_at_dispatch = []
        self.budget = None

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
        self.calls.append(
            (max_output_tokens, transport_retries, request_timeout_seconds)
        )
        if self.budget:
            self.costs_at_dispatch.append(self.budget.total_cost_usd)
        if self.errors:
            error = self.errors.pop(0)
            if error:
                raise error
        return ProviderResponse(content="{}", input_tokens=100, output_tokens=10)


def request(images=None, model="gpt-4o"):
    return dict(
        messages=[
            Message("system", "System instruction"),
            Message("user", "Analyze the evidence"),
        ],
        tools=[],
        images=images or [],
        model=model,
        max_output_tokens=100,
    )


def picture(fmt="PNG", size=(1200, 800)):
    # Real pixels/codec, not a header stub or opaque 200KB padding. Noise makes
    # compressed sizes realistic and verifies billing isn't proportional to bytes.
    rng = random.Random(42)
    image = Image.frombytes("RGB", size, rng.randbytes(size[0] * size[1] * 3))
    output = io.BytesIO()
    image.save(output, format=fmt)
    return output.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model", ["claude-opus-4-8", "claude-haiku-4-5", "gpt-4o", "gpt-4o-mini"]
)
@pytest.mark.parametrize("fmt", ["PNG", "JPEG"])
async def test_three_realistic_images_admit_under_five_dollars(model, fmt):
    data = picture(fmt)
    assert len(data) > 200_000
    provider = MeteredProvider()
    budget = BudgetTracker(max_cost_usd=5)
    await budget.complete(provider, **request([data] * 3, model))
    assert len(provider.calls) == 1
    with pytest.raises(BudgetExceededError):
        await BudgetTracker(max_cost_usd=0.000001).complete(
            provider, **request([data] * 3, model)
        )
    assert len(provider.calls) == 1


def test_dimensions_not_encoded_bytes_and_provider_specific_tiles():
    png, jpeg = picture("PNG"), picture("JPEG")
    assert image_tokens(png, "gpt-4o") == image_tokens(jpeg, "gpt-4o")
    assert image_tokens(png, "gpt-4o-mini") > image_tokens(png, "gpt-4o") * 20
    assert image_tokens(png, "claude-haiku-4-5") < 2000


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [b"bad", b"\x89PNG\r\n\x1a\n", b"<svg/>"])
async def test_invalid_images_rejected_without_dispatch(data):
    provider = MeteredProvider()
    with pytest.raises(ValueError, match="image"):
        await BudgetTracker(max_cost_usd=5).complete(provider, **request([data]))
    assert not provider.calls


def test_image_bounds_reject_not_clamp():
    data = picture(size=(8001, 1))
    with pytest.raises(ValueError, match="8000"):
        image_tokens(data, "gpt-4o")


@pytest.mark.asyncio
@pytest.mark.parametrize("status,unbilled", [(429, True), (503, False)])
async def test_retry_success_reserved_per_attempt_without_shared_mutation(
    status, unbilled
):
    error = ProviderError(
        "transient",
        retryable=True,
        status_code=status,
        known_unbilled=unbilled,
        retry_after=999,
    )
    provider = MeteredProvider([error, None])
    sleeps = AsyncMock()
    budget = BudgetTracker(max_cost_usd=5, sleep_fn=sleeps)
    provider.budget = budget
    before = vars(provider).keys()
    await budget.complete(provider, **request())
    assert len(provider.calls) == 2
    assert all(not retries for _, retries, _ in provider.calls)
    assert "disable_retries" not in vars(provider) and vars(provider).keys() == before
    assert sleeps.call_args.args == (30,)
    first, second = provider.costs_at_dispatch
    assert second == pytest.approx(first if unbilled else first * 2)
    assert bool(budget.uncertain_cost_usd) == (not unbilled)


@pytest.mark.asyncio
async def test_uncertain_retry_readmission_can_reject():
    provider = MeteredProvider([ProviderError("503", retryable=True)])
    budget = BudgetTracker(max_cost_usd=0.004, sleep_fn=AsyncMock())
    with pytest.raises(BudgetExceededError):
        await budget.complete(provider, **request())
    assert len(provider.calls) == 1 and budget.uncertain_cost_usd > 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 404])
async def test_permanent_rejected_reservation_released(status):
    provider = MeteredProvider(
        [ProviderError("permanent", status_code=status, known_unbilled=True)]
    )
    budget = BudgetTracker(max_cost_usd=5, sleep_fn=AsyncMock())
    with pytest.raises(ProviderError):
        await budget.complete(provider, **request())
    assert len(provider.calls) == 1 and budget.total_cost_usd == 0


@pytest.mark.asyncio
async def test_exhausted_retry_and_timeout_liability():
    provider = MeteredProvider([TimeoutError()] * 4)
    budget = BudgetTracker(max_cost_usd=5, sleep_fn=AsyncMock())
    provider.budget = budget
    with pytest.raises(TimeoutError):
        await budget.complete(provider, **request())
    assert len(provider.calls) == 4
    assert budget.uncertain_cost_usd == pytest.approx(provider.costs_at_dispatch[0] * 4)


@pytest.mark.asyncio
async def test_cancel_in_backoff_does_not_add_an_attempt():
    sleeping = asyncio.Event()

    async def sleep(delay):
        sleeping.set()
        await asyncio.Event().wait()

    provider = MeteredProvider(
        [ProviderError("429", retryable=True, known_unbilled=True)]
    )
    budget = BudgetTracker(max_cost_usd=5, sleep_fn=sleep)
    task = asyncio.create_task(budget.complete(provider, **request()))
    await sleeping.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(provider.calls) == 1 and budget.total_cost_usd == 0


@pytest.mark.parametrize(
    "headers", [None, [], 42, {"retry-after": None}, {"retry-after": "NaN"}]
)
def test_bad_retry_after_headers(headers):
    error = ProviderError("test")
    error.response = SimpleNamespace(headers=headers)
    assert retry_after_seconds(error) is None


def test_operational_settings_not_in_cache_key(synthetic_document):
    base = PipelineConfig()
    settings = [
        base,
        base.model_copy(update={"max_cost_usd": 1}),
        base.model_copy(update={"max_concurrency": 1}),
        base.model_copy(update={"request_timeout_seconds": 180}),
    ]
    keys = [
        OrchestratorRunner(MockProvider(), config, BudgetTracker())._make_cache_key(
            synthetic_document
        )
        for config in settings
    ]
    assert len(set(keys)) == 1


@pytest.mark.asyncio
async def test_timeout_and_output_forwarded_without_hidden_lower_limit():
    provider = MeteredProvider()
    await BudgetTracker().complete(provider, **request(), request_timeout_seconds=180)
    assert provider.calls == [(100, False, 180)]


@pytest.mark.asyncio
async def test_figure_vision_uses_shared_admission():
    from paper2html.figure_vision import FigureVisionWorker
    from paper2html.models.document import Figure

    provider = MeteredProvider()
    budget = BudgetTracker(max_cost_usd=0.000001)
    figure = Figure(id="fig-1", image_bytes=picture("JPEG"))
    worker = FigureVisionWorker(
        provider,
        model="gpt-4o",
        budget=budget,
        max_output_tokens=100,
        request_timeout_seconds=180,
    )
    with pytest.raises(BudgetExceededError):
        await worker._call_with_retry_inner(figure, [Message("user", "analyze")])
    assert not provider.calls


@pytest.mark.parametrize("failure", [False, True])
def test_runner_cleans_cache_and_provider(synthetic_document, monkeypatch, failure):
    import paper2html.pipeline.runner as module

    cache = SimpleNamespace(
        get=lambda key: None,
        set=lambda *args: None,
        close=lambda: closed.append("cache"),
    )
    closed = []

    class Owned(MockProvider):
        async def aclose(self):
            closed.append("provider")

    async def run(self, doc):
        if failure:
            raise RuntimeError("run failure")
        return "result"

    monkeypatch.setattr(module, "get_cache", lambda _: cache)
    monkeypatch.setattr(module.OrchestratorRunner, "run", run)
    if failure:
        with pytest.raises(RuntimeError):
            module.run_pipeline(synthetic_document, PipelineConfig(), Owned())
    else:
        assert (
            module.run_pipeline(synthetic_document, PipelineConfig(), Owned())
            == "result"
        )
    assert closed == ["provider", "cache"]


@pytest.mark.asyncio
async def test_all_orchestrator_stages_use_transport_readmission(synthetic_document):
    class TransientOnce(MockProvider):
        def __init__(self):
            super().__init__()
            self.seen = set()
            self.attempts = []

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
            key = messages[-1].content
            self.attempts.append((key, transport_retries))
            if key not in self.seen:
                self.seen.add(key)
                raise ProviderError("429", retryable=True, known_unbilled=True)
            return await super().complete(messages, images, tools, model=model)

    provider = TransientOnce()
    budget = BudgetTracker(max_cost_usd=5, sleep_fn=AsyncMock())
    result = await OrchestratorRunner(provider, PipelineConfig(), budget).run(
        synthetic_document
    )
    assert result.complete
    assert len(provider.attempts) == 2 * (len(synthetic_document.sections) + 2)
    assert all(not retry for _, retry in provider.attempts)


@pytest.mark.asyncio
async def test_real_timeout_retains_reservation():
    class Slow(MeteredProvider):
        async def complete(self, **kwargs):
            await asyncio.Event().wait()

    budget = BudgetTracker(max_cost_usd=5, retry_delays=())
    with pytest.raises(TimeoutError):
        await budget.complete(Slow(), **request(), request_timeout_seconds=0.001)
    assert budget.uncertain_cost_usd > 0


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["openai", "anthropic"])
async def test_sdk_timeout_override_metadata_and_single_attempt(kind):
    import httpx
    import importlib
    from paper2html.pipeline.providers.openai_provider import OpenAIProvider
    from paper2html.pipeline.providers.anthropic_provider import AnthropicProvider

    sdk = importlib.import_module(kind)
    error = sdk.RateLimitError(
        "busy",
        response=httpx.Response(
            429,
            request=httpx.Request("POST", "https://offline.invalid"),
            headers={"retry-after": "9999"},
        ),
        body=None,
    )
    create = AsyncMock(side_effect=error)
    if kind == "openai":
        provider = OpenAIProvider(
            client=SimpleNamespace(
                chat=SimpleNamespace(completions=SimpleNamespace(create=create))
            )
        )
    else:
        provider = AnthropicProvider(
            client=SimpleNamespace(messages=SimpleNamespace(create=create))
        )
    with pytest.raises(ProviderError) as caught:
        await provider.complete(
            **request(), request_timeout_seconds=180, transport_retries=False
        )
    assert create.await_count == 1 and create.call_args.kwargs["timeout"] == 180
    assert caught.value.retryable and caught.value.known_unbilled
    assert caught.value.status_code == 429 and caught.value.retry_after == 30


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [0, -1, 1.5, True])
async def test_invalid_output_bound_before_dispatch(value):
    provider = MeteredProvider()
    args = request()
    args["max_output_tokens"] = value
    with pytest.raises(ValueError):
        await BudgetTracker().complete(provider, **args)
    assert not provider.calls


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf")])
def test_invalid_timeout_config(value):
    with pytest.raises(ValueError):
        PipelineConfig(request_timeout_seconds=value)


def test_runner_closes_provider_on_cache_open_failure(synthetic_document, monkeypatch):
    import paper2html.pipeline.runner as module

    closed = []

    class Owned(MockProvider):
        async def aclose(self):
            closed.append(True)

    def fail(_):
        raise OSError("cache unavailable")

    monkeypatch.setattr(module, "get_cache", fail)
    with pytest.raises(OSError):
        module.run_pipeline(synthetic_document, PipelineConfig(), Owned())
    assert closed == [True]


def test_runner_closes_cache_even_if_provider_close_fails(
    synthetic_document, monkeypatch
):
    import paper2html.pipeline.runner as module

    closed = []

    class Owned(MockProvider):
        async def aclose(self):
            raise OSError("close failed")

    cache = SimpleNamespace(
        get=lambda _: None, set=lambda *args: None, close=lambda: closed.append(True)
    )
    monkeypatch.setattr(module, "get_cache", lambda _: cache)
    with pytest.raises(OSError):
        module.run_pipeline(synthetic_document, PipelineConfig(), Owned())
    assert closed == [True]
