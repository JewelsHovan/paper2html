"""Provider interface and transport dataclasses for the agent pipeline.

Design notes
------------
* All types are plain dataclasses — intentionally NOT Pydantic so the interface
  is fully decoupled from the pipeline schema layer.
* ``complete()`` is async because workers run via ``asyncio.gather``; the
  ``AsyncAnthropic`` and ``AsyncOpenAI`` clients are used underneath.
* The top-level ``images`` param on ``complete()`` is the canonical channel for
  figure bytes (one list per call, parallel to the message list).
  ``Message.image_bytes`` remains on the dataclass for future use but is NOT
  consumed by any provider in v1 — document this here so callers don't rely on
  it silently.
* The interface must NOT leak provider-specific SDK types.  All return types are
  defined in this module.
"""

from __future__ import annotations

import json
import math

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Message:
    """A single message in the conversation.

    Attributes:
        role: One of ``"user"``, ``"assistant"``, or ``"system"``.
        content: Text content of the message.
        image_bytes: Attached image bytes.  Declared for API completeness but
            NOT consumed by providers in v1 — pass figure bytes via the top-level
            ``images`` argument to ``complete()`` instead.
    """

    role: str
    content: str
    image_bytes: list[bytes] = field(default_factory=list)


@dataclass
class ToolDef:
    """Tool / function definition provided to the model.

    Attributes:
        name: Unique tool name (snake_case).
        description: Human-readable description for the model.
        input_schema: JSON Schema dict describing the tool's arguments.
    """

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass
class ToolCall:
    """A tool invocation returned by the model.

    Attributes:
        id: Unique call identifier (provider-assigned).
        name: Name of the tool being called.
        arguments: Parsed argument dict (providers normalise ``json.loads``).
    """

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ProviderResponse:
    """Normalised response from any provider.

    Attributes:
        content: Text content (may be empty when ``stop_reason == "tool_use"``).
        tool_calls: Tool invocations requested by the model.
        input_tokens: Number of input tokens consumed.
        output_tokens: Number of output tokens generated.
        stop_reason: ``"end_turn"`` or ``"tool_use"``.
    """

    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str = "end_turn"
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


class ProviderInterface(ABC):
    """Minimal async provider seam.

    Concrete implementations (Anthropic, OpenAI, Mock) must subclass this.
    The interface is intentionally narrow — provider-specific concerns
    (prompt caching, Files API, image encoding) live entirely inside each
    implementation.
    """

    # Explicit cost capability; production/custom providers are metered by default.
    free_of_charge: bool = False

    async def aclose(self) -> None:
        """Release provider-owned resources (none by default)."""

    @abstractmethod
    async def complete(
        self,
        messages: list[Message],
        images: list[bytes],
        tools: list[ToolDef],
        *,
        model: str,
        max_output_tokens: int = 8192,
        transport_retries: bool = True,
        request_timeout_seconds: float = 60.0,
    ) -> ProviderResponse:
        """Send a completion request to the provider.

        Args:
            messages: Conversation history in order.
            images: Figure image bytes to attach as vision content blocks.
            tools: Tool / function definitions available to the model.
            model: Model identifier string (passed explicitly so a single
                provider instance can serve both orchestrator and worker tiers).

        Returns:
            Normalised ``ProviderResponse`` — never a provider-SDK type.
        """


def normalize_tool_arguments(value: Any) -> dict[str, Any]:
    """Malformed tools are inert, never transport failures."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return {}
    return value if isinstance(value, dict) else {}


def validate_request_limits(
    max_output_tokens: int, request_timeout_seconds: float
) -> None:
    if type(max_output_tokens) is not int or max_output_tokens <= 0:
        raise ValueError("max_output_tokens must be a positive integer")
    if (
        isinstance(request_timeout_seconds, bool)
        or not math.isfinite(request_timeout_seconds)
        or request_timeout_seconds <= 0
    ):
        raise ValueError("request_timeout_seconds must be finite and positive")


def detect_media_type(img_bytes: bytes) -> str:
    """Detect the MIME media type from image magic bytes.

    Checks the leading bytes of ``img_bytes`` to identify the image format.
    Falls back to ``"image/png"`` for unrecognised formats.

    Args:
        img_bytes: Raw image bytes to inspect.

    Returns:
        MIME type string: ``"image/jpeg"``, ``"image/png"``, ``"image/gif"``,
        or the fallback ``"image/png"``.
    """
    if img_bytes[:2] == b"\xff\xd8":
        return "image/jpeg"
    if img_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if img_bytes[:4] == b"RIFF" and img_bytes[8:12] == b"WEBP":
        return "image/webp"
    if img_bytes[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return "image/png"
