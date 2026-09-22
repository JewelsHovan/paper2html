"""OpenAI SDK implementation of ``ProviderInterface``.

Design notes
------------
* The ``AsyncOpenAI`` client is constructed with ``max_retries=0`` so that the
  SDK's built-in retry logic is disabled; our ``backoff_retry`` governs timing.
* Images are passed as ``image_url`` content blocks with a ``data:`` URI.
* ``tool_calls[i].function.arguments`` is a JSON STRING in the OpenAI response
  — it is ``json.loads()``-ed here before returning.
* Token field names differ from Anthropic: ``prompt_tokens`` → ``input_tokens``,
  ``completion_tokens`` → ``output_tokens``.
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
    import openai  # noqa: F401

logger = logging.getLogger(__name__)

_MAX_TOKENS = 8192


class OpenAIProvider(ProviderInterface):
    """OpenAI SDK provider.

    Args:
        api_key: API key.  Falls back to ``OPENAI_API_KEY`` env var when
            ``None``.
        client: Pre-built ``AsyncOpenAI`` instance.  When provided, ``api_key``
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
            from openai import AsyncOpenAI  # pylint: disable=import-outside-toplevel

            # Disable SDK retries so our backoff_retry wrapper governs
            self._client = AsyncOpenAI(api_key=api_key, max_retries=0, timeout=60.0)
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
    ) -> ProviderResponse:
        """Send a completion request to the OpenAI API.

        Args:
            messages: Conversation history.
            images: Figure image bytes passed as ``image_url`` content blocks.
            tools: Tool definitions.
            model: Model identifier.

        Returns:
            Normalised ``ProviderResponse``.

        Raises:
            ProviderError: After 3 failed retries.
        """
        validate_request_limits(max_output_tokens, request_timeout_seconds)
        import openai  # pylint: disable=import-outside-toplevel

        retryable = (
            TimeoutError,
            openai.RateLimitError,
            openai.APIStatusError,
            openai.APIConnectionError,
            openai.APITimeoutError,
        )

        sleep_fn_kwargs: dict[str, Any] = {}
        if self._sleep_fn is not None:
            sleep_fn_kwargs["sleep_fn"] = self._sleep_fn

        sdk_messages = self._build_messages(messages, images)
        sdk_tools = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.input_schema,
                },
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
                "messages": sdk_messages,
            }
            if sdk_tools:
                kwargs["tools"] = sdk_tools

            response = await self._client.chat.completions.create(**kwargs)
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
                f"OpenAI API call failed after retries: {exc}",
                provider="openai",
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

    def _build_messages(
        self,
        messages: list[Message],
        images: list[bytes],
    ) -> list[dict[str, Any]]:
        """Convert ``Message`` list to OpenAI chat format.

        Images are appended to the last user message as ``image_url`` blocks.

        Args:
            messages: Conversation messages.
            images: Figure image bytes.

        Returns:
            List of OpenAI-format message dicts.
        """
        sdk_messages: list[dict[str, Any]] = []
        for msg in messages:
            sdk_messages.append({"role": msg.role, "content": msg.content})

        if images:
            if not sdk_messages or sdk_messages[-1]["role"] != "user":
                sdk_messages.append({"role": "user", "content": ""})

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
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{detect_media_type(img_bytes)};base64,{b64}"
                        },
                    }
                )

        return sdk_messages

    def _normalize(self, response: Any) -> ProviderResponse:
        """Normalise an OpenAI SDK response to ``ProviderResponse``.

        Args:
            response: Raw ``openai.types.chat.ChatCompletion`` object.

        Returns:
            Provider-agnostic ``ProviderResponse``.
        """
        choice = response.choices[0]
        message = choice.message

        content_text = message.content or ""
        tool_calls: list[ToolCall] = []

        if message.tool_calls:
            for tc in message.tool_calls:
                args = normalize_tool_arguments(tc.function.arguments)
                tool_calls.append(
                    ToolCall(id=tc.id, name=tc.function.name, arguments=args)
                )

        finish_reason = choice.finish_reason or "stop"
        stop_reason = "tool_use" if finish_reason == "tool_calls" else "end_turn"

        return ProviderResponse(
            content=content_text,
            tool_calls=tool_calls,
            input_tokens=response.usage.prompt_tokens,
            output_tokens=response.usage.completion_tokens,
            stop_reason=stop_reason,
        )
