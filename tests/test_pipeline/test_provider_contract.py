"""Provider contract tests.

Verifies that MockProvider, AnthropicProvider (fake client), and OpenAIProvider
(fake client) all satisfy the ProviderInterface contract.

AC-1: ProviderInterface ABC is defined; both providers pass shared contract tests.
AC-10: Default model tiers and model kwarg forwarding.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from paper2html.pipeline.providers.base import (
    Message,
    ProviderInterface,
    ProviderResponse,
)
from paper2html.pipeline.providers.mock_provider import FailMode, MockProvider

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _simple_messages() -> list[Message]:
    return [Message(role="user", content="SECTION_ID: sec-1\nAnalyse this.")]


def _make_worker_json(section_id: str = "sec-1") -> str:
    return json.dumps(
        {
            "schema_version": "1.0",
            "section_id": section_id,
            "summary": "Test summary",
            "key_claims": ["claim1"],
            "term_definitions": [],
            "summary_skim": f"Skim summary for {section_id}.",
            "summary_read": f"Read summary for {section_id}.",
            "summary_deep": f"Deep summary for {section_id}.",
            "margin_notes": [f"Margin note for {section_id}."],
        }
    )


# ---------------------------------------------------------------------------
# Shared contract helper
# ---------------------------------------------------------------------------


async def _assert_contract(
    provider: ProviderInterface, model: str = "claude-haiku-4-5"
) -> None:
    """Assert that a provider satisfies the basic contract."""
    response = await provider.complete(
        messages=_simple_messages(),
        images=[],
        tools=[],
        model=model,
    )
    assert isinstance(response, ProviderResponse)
    assert isinstance(response.content, str)
    assert isinstance(response.input_tokens, int)
    assert isinstance(response.output_tokens, int)
    assert isinstance(response.tool_calls, list)
    assert response.stop_reason in ("end_turn", "tool_use")


# ---------------------------------------------------------------------------
# MockProvider tests
# ---------------------------------------------------------------------------


async def test_mock_provider_satisfies_contract(mock_provider: MockProvider) -> None:
    """MockProvider returns a valid ProviderResponse."""
    await _assert_contract(mock_provider)


async def test_mock_provider_is_deterministic(mock_provider: MockProvider) -> None:
    """Same input → same WorkerOutput JSON every time."""
    messages = _simple_messages()
    r1 = await mock_provider.complete(messages=messages, images=[], tools=[], model="m")
    r2 = await mock_provider.complete(messages=messages, images=[], tools=[], model="m")
    assert r1.content == r2.content


async def test_mock_provider_tracks_call_count(mock_provider: MockProvider) -> None:
    """call_count increments on each complete() call."""
    assert mock_provider.call_count == 0
    await mock_provider.complete(
        messages=_simple_messages(), images=[], tools=[], model="m"
    )
    assert mock_provider.call_count == 1
    await mock_provider.complete(
        messages=_simple_messages(), images=[], tools=[], model="m"
    )
    assert mock_provider.call_count == 2


async def test_mock_fail_mode_all_calls_returns_invalid_json() -> None:
    """FailMode.ALL_CALLS always returns malformed JSON."""
    provider = MockProvider(fail_mode=FailMode.ALL_CALLS)
    r = await provider.complete(
        messages=_simple_messages(), images=[], tools=[], model="m"
    )
    # Should not be valid JSON
    with pytest.raises(json.JSONDecodeError):
        json.loads(r.content)


async def test_mock_fail_mode_first_call_then_valid() -> None:
    """FailMode.FIRST_CALL: first call for a section fails; second returns valid JSON.

    Tracking is per section_id so that the retry prefix (which changes the
    content hash) still counts as "second call for the same section" → valid.
    """
    provider = MockProvider(fail_mode=FailMode.FIRST_CALL)
    # First call for sec-x → malformed
    msgs = [Message(role="user", content="SECTION_ID: sec-x\nTest")]
    r1 = await provider.complete(messages=msgs, images=[], tools=[], model="m")
    with pytest.raises(json.JSONDecodeError):
        json.loads(r1.content)

    # Second call for sec-x (same section, different content hash via retry prefix) → valid
    msgs2 = [
        Message(
            role="user",
            content="IMPORTANT: return valid JSON.\nSECTION_ID: sec-x\nTest",
        )
    ]
    r2 = await provider.complete(messages=msgs2, images=[], tools=[], model="m")
    data = json.loads(r2.content)
    assert data["section_id"] == "sec-x"

    # First call for a different section → malformed
    msgs3 = [Message(role="user", content="SECTION_ID: sec-y\nDifferent")]
    r3 = await provider.complete(messages=msgs3, images=[], tools=[], model="m")
    with pytest.raises(json.JSONDecodeError):
        json.loads(r3.content)


async def test_mock_target_section_fail() -> None:
    """FailMode.TARGET_SECTION fails only for the first call to target section."""
    provider = MockProvider(
        fail_mode=FailMode.TARGET_SECTION, target_section_id="sec-3"
    )
    # Non-target section → valid
    r_other = await provider.complete(
        messages=[Message(role="user", content="SECTION_ID: sec-1\nOk")],
        images=[],
        tools=[],
        model="m",
    )
    data = json.loads(r_other.content)
    assert data["section_id"] == "sec-1"

    # Target section first call → malformed
    r_target_1 = await provider.complete(
        messages=[Message(role="user", content="SECTION_ID: sec-3\nFail me")],
        images=[],
        tools=[],
        model="m",
    )
    with pytest.raises(json.JSONDecodeError):
        json.loads(r_target_1.content)

    # Target section second call → valid
    r_target_2 = await provider.complete(
        messages=[Message(role="user", content="SECTION_ID: sec-3\nRetry")],
        images=[],
        tools=[],
        model="m",
    )
    data2 = json.loads(r_target_2.content)
    assert data2["section_id"] == "sec-3"


# ---------------------------------------------------------------------------
# AnthropicProvider with injected fake client
# ---------------------------------------------------------------------------


def _make_anthropic_fake_response(
    content: str = "", model: str = "claude-haiku-4-5"
) -> Any:
    """Build a minimal SDK-shaped Anthropic message object."""
    from types import SimpleNamespace

    usage = SimpleNamespace(input_tokens=10, output_tokens=5)
    block = SimpleNamespace(type="text", text=content)
    return SimpleNamespace(
        content=[block],
        stop_reason="end_turn",
        usage=usage,
        model=model,
    )


async def test_anthropic_provider_contract() -> None:
    """AnthropicProvider with an injected fake async client satisfies the contract."""
    from paper2html.pipeline.providers.anthropic_provider import AnthropicProvider

    fake_response = _make_anthropic_fake_response(_make_worker_json())
    fake_client = MagicMock()
    fake_client.messages = MagicMock()
    fake_client.messages.create = AsyncMock(return_value=fake_response)

    provider = AnthropicProvider(client=fake_client)
    await _assert_contract(provider)


async def test_anthropic_provider_model_forwarded() -> None:
    """AnthropicProvider forwards the model kwarg to the SDK client."""
    from paper2html.pipeline.providers.anthropic_provider import AnthropicProvider

    fake_response = _make_anthropic_fake_response(_make_worker_json())
    fake_client = MagicMock()
    fake_client.messages = MagicMock()
    fake_client.messages.create = AsyncMock(return_value=fake_response)

    provider = AnthropicProvider(client=fake_client)
    await provider.complete(
        messages=_simple_messages(),
        images=[],
        tools=[],
        model="claude-opus-4-8",
    )
    call_kwargs = fake_client.messages.create.call_args.kwargs
    assert call_kwargs["model"] == "claude-opus-4-8"


async def test_anthropic_provider_token_normalization() -> None:
    """AnthropicProvider extracts input_tokens and output_tokens from usage."""
    from paper2html.pipeline.providers.anthropic_provider import AnthropicProvider

    from types import SimpleNamespace

    usage = SimpleNamespace(input_tokens=42, output_tokens=17)
    block = SimpleNamespace(type="text", text="hello")
    fake_response = SimpleNamespace(
        content=[block], stop_reason="end_turn", usage=usage
    )
    fake_client = MagicMock()
    fake_client.messages = MagicMock()
    fake_client.messages.create = AsyncMock(return_value=fake_response)

    provider = AnthropicProvider(client=fake_client)
    resp = await provider.complete(
        messages=_simple_messages(), images=[], tools=[], model="m"
    )
    assert resp.input_tokens == 42
    assert resp.output_tokens == 17


# ---------------------------------------------------------------------------
# OpenAIProvider with injected fake client
# ---------------------------------------------------------------------------


def _make_openai_fake_response(content: str = "") -> Any:
    """Build a minimal SDK-shaped OpenAI chat completion object."""
    from types import SimpleNamespace

    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5)
    message = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(message=message, finish_reason="stop")
    return SimpleNamespace(choices=[choice], usage=usage)


async def test_openai_provider_contract() -> None:
    """OpenAIProvider with an injected fake async client satisfies the contract."""
    from paper2html.pipeline.providers.openai_provider import OpenAIProvider

    fake_response = _make_openai_fake_response(_make_worker_json())
    fake_client = MagicMock()
    fake_client.chat = MagicMock()
    fake_client.chat.completions = MagicMock()
    fake_client.chat.completions.create = AsyncMock(return_value=fake_response)

    provider = OpenAIProvider(client=fake_client)
    await _assert_contract(provider, model="gpt-4o-mini")


async def test_openai_provider_model_forwarded() -> None:
    """OpenAIProvider forwards the model kwarg to the SDK client."""
    from paper2html.pipeline.providers.openai_provider import OpenAIProvider

    fake_response = _make_openai_fake_response(_make_worker_json())
    fake_client = MagicMock()
    fake_client.chat = MagicMock()
    fake_client.chat.completions = MagicMock()
    fake_client.chat.completions.create = AsyncMock(return_value=fake_response)

    provider = OpenAIProvider(client=fake_client)
    await provider.complete(
        messages=_simple_messages(),
        images=[],
        tools=[],
        model="gpt-4o",
    )
    call_kwargs = fake_client.chat.completions.create.call_args.kwargs
    assert call_kwargs["model"] == "gpt-4o"


async def test_openai_provider_token_normalization() -> None:
    """OpenAIProvider normalises prompt_tokens/completion_tokens → input/output."""
    from paper2html.pipeline.providers.openai_provider import OpenAIProvider

    from types import SimpleNamespace

    usage = SimpleNamespace(prompt_tokens=33, completion_tokens=11)
    message = SimpleNamespace(content="hello", tool_calls=None)
    choice = SimpleNamespace(message=message, finish_reason="stop")
    fake_response = SimpleNamespace(choices=[choice], usage=usage)

    fake_client = MagicMock()
    fake_client.chat = MagicMock()
    fake_client.chat.completions = MagicMock()
    fake_client.chat.completions.create = AsyncMock(return_value=fake_response)

    provider = OpenAIProvider(client=fake_client)
    resp = await provider.complete(
        messages=_simple_messages(), images=[], tools=[], model="gpt-4o-mini"
    )
    assert resp.input_tokens == 33
    assert resp.output_tokens == 11


# ---------------------------------------------------------------------------
# AnthropicProvider image media_type detection (ticket 012)
# ---------------------------------------------------------------------------


def _make_anthropic_provider_with_images(
    images: list[bytes],
) -> tuple[Any, Any]:
    """Build an AnthropicProvider with a fake client and invoke complete().

    Returns ``(provider, fake_client)`` so callers can inspect call_args.
    The fake_client is pre-wired with a valid response.
    """
    from paper2html.pipeline.providers.anthropic_provider import AnthropicProvider

    fake_response = _make_anthropic_fake_response(_make_worker_json())
    fake_client = MagicMock()
    fake_client.messages = MagicMock()
    fake_client.messages.create = AsyncMock(return_value=fake_response)
    return AnthropicProvider(client=fake_client), fake_client


async def test_anthropic_provider_jpeg_media_type() -> None:
    """JPEG magic bytes produce 'image/jpeg' in the image block media_type."""
    jpeg_bytes = b"\xff\xd8\xff\xe0" + b"\x00" * 100
    provider, fake_client = _make_anthropic_provider_with_images([jpeg_bytes])

    await provider.complete(
        messages=_simple_messages(),
        images=[jpeg_bytes],
        tools=[],
        model="claude-haiku-4-5",
    )

    sdk_messages = fake_client.messages.create.call_args.kwargs["messages"]
    image_block = next(
        b for b in sdk_messages[-1]["content"] if b.get("type") == "image"
    )
    assert image_block["source"]["media_type"] == "image/jpeg"


async def test_anthropic_provider_png_media_type() -> None:
    """PNG magic bytes produce 'image/png' in the image block media_type."""
    png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
    provider, fake_client = _make_anthropic_provider_with_images([png_bytes])

    await provider.complete(
        messages=_simple_messages(),
        images=[png_bytes],
        tools=[],
        model="claude-haiku-4-5",
    )

    sdk_messages = fake_client.messages.create.call_args.kwargs["messages"]
    image_block = next(
        b for b in sdk_messages[-1]["content"] if b.get("type") == "image"
    )
    assert image_block["source"]["media_type"] == "image/png"


async def test_anthropic_provider_unknown_bytes_media_type_fallback() -> None:
    """Unknown magic bytes fall back to 'image/png' in the image block media_type."""
    unknown_bytes = b"\x00\x01\x02\x03" + b"\x00" * 100
    provider, fake_client = _make_anthropic_provider_with_images([unknown_bytes])

    await provider.complete(
        messages=_simple_messages(),
        images=[unknown_bytes],
        tools=[],
        model="claude-haiku-4-5",
    )

    sdk_messages = fake_client.messages.create.call_args.kwargs["messages"]
    image_block = next(
        b for b in sdk_messages[-1]["content"] if b.get("type") == "image"
    )
    assert image_block["source"]["media_type"] == "image/png"
