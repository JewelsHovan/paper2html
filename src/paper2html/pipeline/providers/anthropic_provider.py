"""Anthropic SDK implementation of ``ProviderInterface``.

Design notes
------------
* Heavy SDK import (``anthropic``) is deferred to the constructor so that
  importing this module does NOT pull in the SDK at module level.  TYPE_CHECKING
  guard is used for type annotations only.
* Prompt caching: the system message gets a ``cache_control: {"type":"ephemeral"}``
  block so the shared prefix is cached across parallel workers in the same run.
  Per-section user text is NOT cached (it changes per worker).
* Files API: when ``pdf_file_id`` is provided the call routes through
  ``client.beta.messages.create`` with ``betas=["files-api-2025-04-14"]``.
* Retries: up to 3 retries with 2-4-8s exponential backoff on
  ``RateLimitError``, ``APIStatusError``, ``APIConnectionError``,
  ``APITimeoutError``.  SDK retries are disabled; this wrapper is the only retry owner. Pipeline
  admission disables transport retries to account for each dispatched attempt.
"""

from __future__ import annotations

import base64
import logging
from typing import TYPE_CHECKING, Any

from paper2html.pipeline._backoff import (
    backoff_retry,
    retry_after_seconds,
    transient_status,
)
from paper2html.pipeline.errors import ProviderError
from paper2html.pipeline.providers.base import (
    normalize_tool_arguments,
    validate_request_limits,
    detect_media_type,
    Message,
    ProviderInterface,
    ProviderResponse,
    ToolCall,
    ToolDef,
)

if TYPE_CHECKING:
    import anthropic  # noqa: F401

logger = logging.getLogger(__name__)

_MAX_TOKENS = 8192


class AnthropicProvider(ProviderInterface):
    """Anthropic SDK provider.

    Args:
        api_key: API key.  Falls back to ``ANTHROPIC_API_KEY`` env var when
            ``None``.
        client: Pre-built ``AsyncAnthropic`` instance.  When provided, ``api_key``
            is ignored.  Useful for injecting test doubles.
        sleep_fn: Async sleep function injected during tests to avoid real waits.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        client: Any = None,
        sleep_fn: Any = None,
    ) -> None:
        self._owns_client = client is None
        if client is not None:
            self._client = client
        else:
            from anthropic import AsyncAnthropic  # pylint: disable=import-outside-toplevel

            self._client = AsyncAnthropic(api_key=api_key, max_retries=0, timeout=60.0)
        self._sleep_fn = sleep_fn

    async def aclose(self) -> None:
        """Close SDK clients created here; injected clients remain caller-owned."""
        if self._owns_client:
            await self._client.close()

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
        pdf_file_id: str | None = None,
    ) -> ProviderResponse:
        """Send a completion request to the Anthropic API.

        Args:
            messages: Conversation history.
            images: Figure image bytes passed as vision content blocks.
            tools: Tool definitions.
            model: Model identifier.
            pdf_file_id: Optional Files API file id; triggers ``beta.messages``
                route with ``betas=["files-api-2025-04-14"]``.

        Returns:
            Normalised ``ProviderResponse``.

        Raises:
            ProviderError: After 3 failed retries.
        """
        validate_request_limits(max_output_tokens, request_timeout_seconds)
        import anthropic  # pylint: disable=import-outside-toplevel

        retryable = (
            TimeoutError,
            anthropic.RateLimitError,
            anthropic.APIStatusError,
            anthropic.APIConnectionError,
            anthropic.APITimeoutError,
        )

        sleep_fn_kwargs: dict[str, Any] = {}
        if self._sleep_fn is not None:
            sleep_fn_kwargs["sleep_fn"] = self._sleep_fn

        # Build system blocks (with cache_control for shared-prefix caching)
        system_blocks, sdk_messages = self._build_payload(messages, images, pdf_file_id)

        # Build tool list
        sdk_tools = [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
            }
            for t in tools
        ]

        attempts = 0

        async def _call() -> ProviderResponse:
            nonlocal attempts
            attempts += 1
            kwargs: dict[str, Any] = {
                "model": model,
                "max_tokens": max_output_tokens,
                "timeout": request_timeout_seconds,
                "system": system_blocks,
                "messages": sdk_messages,
            }
            if sdk_tools:
                kwargs["tools"] = sdk_tools

            if pdf_file_id:
                response = await self._client.beta.messages.create(
                    **kwargs,
                    betas=["files-api-2025-04-14"],
                )
            else:
                response = await self._client.messages.create(**kwargs)

            return self._normalize(response)

        try:
            return await backoff_retry(
                _call,
                retryable,
                delays=(2.0, 4.0, 8.0) if transport_retries else (),
                request_timeout_seconds=request_timeout_seconds,
                **sleep_fn_kwargs,
            )
        except retryable as exc:
            raise ProviderError(
                f"Anthropic API call failed after retries: {exc}",
                provider="anthropic",
                retries=max(0, attempts - 1),
                retryable=transient_status(getattr(exc, "status_code", None))
                or getattr(exc, "status_code", None) is None,
                status_code=getattr(exc, "status_code", None),
                retry_after=retry_after_seconds(exc),
                known_unbilled=getattr(exc, "status_code", None)
                in (400, 401, 403, 404, 422, 429),
            ) from exc

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_payload(
        self,
        messages: list[Message],
        images: list[bytes],
        pdf_file_id: str | None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Convert ``Message`` list to Anthropic-SDK format.

        System messages become the ``system`` block list with cache_control.
        User/assistant messages become the ``messages`` list.
        Images are appended to the last user message as base64 content blocks.

        Args:
            messages: Conversation messages.
            images: Figure image bytes.
            pdf_file_id: Optional Files API document file id.

        Returns:
            Tuple of ``(system_blocks, sdk_messages)``.
        """
        system_blocks: list[dict[str, Any]] = []
        sdk_messages: list[dict[str, Any]] = []

        for msg in messages:
            if msg.role == "system":
                system_blocks.append(
                    {
                        "type": "text",
                        "text": msg.content,
                        "cache_control": {"type": "ephemeral"},
                    }
                )
            else:
                sdk_messages.append({"role": msg.role, "content": msg.content})

        # Append image blocks to the last user message
        if images or pdf_file_id:
            # Ensure there's a user message to append to
            if not sdk_messages or sdk_messages[-1]["role"] != "user":
                sdk_messages.append({"role": "user", "content": ""})

            # Convert content to list form if it's a plain string
            last = sdk_messages[-1]
            if isinstance(last["content"], str):
                last["content"] = (
                    [{"type": "text", "text": last["content"]}]
                    if last["content"]
                    else []
                )

            for img_bytes in images:
                b64 = base64.b64encode(img_bytes).decode()
                last["content"].append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": detect_media_type(img_bytes),
                            "data": b64,
                        },
                    }
                )

            if pdf_file_id:
                last["content"].append(
                    {
                        "type": "document",
                        "source": {"type": "file", "file_id": pdf_file_id},
                    }
                )

        return system_blocks, sdk_messages

    def _normalize(self, response: Any) -> ProviderResponse:
        """Normalise an Anthropic SDK response to ``ProviderResponse``.

        Args:
            response: Raw ``anthropic.types.Message`` object.

        Returns:
            Provider-agnostic ``ProviderResponse``.
        """
        content_text = ""
        tool_calls: list[ToolCall] = []

        for block in response.content:
            if block.type == "text":
                content_text += block.text
            elif block.type == "tool_use":
                args = normalize_tool_arguments(block.input)
                tool_calls.append(
                    ToolCall(id=block.id, name=block.name, arguments=args)
                )

        return ProviderResponse(
            content=content_text,
            tool_calls=tool_calls,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cache_read_input_tokens=getattr(
                response.usage, "cache_read_input_tokens", 0
            )
            or 0,
            cache_creation_input_tokens=getattr(
                response.usage, "cache_creation_input_tokens", 0
            )
            or 0,
            stop_reason=response.stop_reason or "end_turn",
        )
