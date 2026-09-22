"""pytest configuration: auto-skip integration tests and reset rate-limiter singleton."""

from __future__ import annotations

import os

import pytest


def pytest_collection_modifyitems(items: list) -> None:
    """Skip integration-marked tests unless PAPER2HTML_INTEGRATION_TESTS=1."""
    if os.environ.get("PAPER2HTML_INTEGRATION_TESTS") == "1":
        return
    skip_marker = pytest.mark.skip(
        reason="integration test (set PAPER2HTML_INTEGRATION_TESTS=1 to run)"
    )
    for item in items:
        if item.get_closest_marker("integration"):
            item.add_marker(skip_marker)


@pytest.fixture(autouse=True)
def isolate_provider_credentials(request, monkeypatch):
    """Offline tests must never depend on, or accidentally use, personal keys."""
    if request.node.get_closest_marker("integration") is None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def reset_rate_limiter_singleton():
    """Reset the RateLimitedClient singleton before and after each test."""
    from paper2html.ingestion._client import _reset_singleton

    _reset_singleton()
    yield
    _reset_singleton()
