"""Concurrency queue time must not consume a provider request's deadline."""

from __future__ import annotations

import asyncio

import pytest

from paper2html.pipeline.budget import BudgetTracker
from paper2html.pipeline.providers.base import ProviderInterface, ProviderResponse


@pytest.mark.asyncio
async def test_many_queued_calls_each_receive_full_request_deadline():
    class SlowProvider(ProviderInterface):
        free_of_charge = True

        async def complete(self, messages, images, tools, *, model, **kwargs):
            await asyncio.sleep(0.05)
            return ProviderResponse(content="ok")

    tracker = BudgetTracker(max_concurrency=2, retry_delays=())
    provider = SlowProvider()
    results = await asyncio.gather(
        *(
            tracker.complete(
                provider,
                messages=[],
                images=[],
                tools=[],
                model="test",
                request_timeout_seconds=0.2,
            )
            for _ in range(20)
        )
    )
    assert len(results) == 20
    assert all(result.content == "ok" for result in results)
    assert tracker.summary()["call_count"] == 20
    assert tracker.uncertain_cost_usd == 0
