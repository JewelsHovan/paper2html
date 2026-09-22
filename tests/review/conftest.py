"""pytest fixtures and helpers for review stage tests.

Design notes
------------
* ``FakeProvider`` uses a per-instance ordered FIFO queue of JSON response
  strings.  It records received prompts (full message content strings) and
  the model identifier passed to each call.
* We do NOT reuse the pipeline ``MockProvider`` because it is content-hash-keyed
  and cannot express two-provider disagreement.
* All test fixtures are synchronous; reviewer tests use the sync ``review()``
  facade so no pytestmark is needed.
"""

from __future__ import annotations

import json
from collections import deque


from paper2html.pipeline.providers.base import (
    Message,
    ProviderInterface,
    ProviderResponse,
    ToolDef,
)
from paper2html.review.schemas import Annotation

# ---------------------------------------------------------------------------
# Source text constants
# ---------------------------------------------------------------------------

SECTION_A_TEXT: str = (
    "The transformer architecture introduced multi-head self-attention, "
    "which allows the model to jointly attend to information from different "
    "representation subspaces at different positions."
)

SECTION_B_TEXT: str = (
    "Convolutional neural networks use shared weight filters to detect "
    "local patterns in the input, enabling translation invariance and "
    "parameter efficiency across spatial dimensions."
)


# ---------------------------------------------------------------------------
# FakeProvider
# ---------------------------------------------------------------------------


class FakeProvider(ProviderInterface):
    """Test double for ``ProviderInterface``.

    Attributes:
        received_prompts: List of message content strings (all roles) from
            every ``complete()`` call, in order.
        received_models: List of model identifier strings from every call.
    """

    def __init__(self, responses: list[str]) -> None:
        """
        Args:
            responses: Ordered list of JSON strings to return.  Each call
                pops the next item.
        """
        self._queue: deque[str] = deque(responses)
        self.received_prompts: list[str] = []
        self.received_models: list[str] = []

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
        """Return the next queued response, recording prompt and model.

        Raises:
            IndexError: If the response queue is exhausted.
        """
        for msg in messages:
            self.received_prompts.append(msg.content)
        self.received_models.append(model)
        response_content = self._queue.popleft()
        return ProviderResponse(
            content=response_content,
            tool_calls=[],
            input_tokens=10,
            output_tokens=20,
            stop_reason="end_turn",
        )

    def assert_exhausted(self) -> None:
        """Assert that all queued responses have been consumed.

        Raises:
            AssertionError: If the queue is non-empty.
        """
        assert len(self._queue) == 0, (
            f"FakeProvider still has {len(self._queue)} unconsumed response(s): "
            f"{list(self._queue)}"
        )


# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------


def make_annotation(
    annotation_id: str = "ann-1",
    section_id: str = "sec-1",
    kind: str = "claim",
    text: str = "multi-head self-attention attends to different representation subspaces",
) -> Annotation:
    """Return a simple ``Annotation`` with sensible defaults."""
    return Annotation(
        annotation_id=annotation_id,
        section_id=section_id,
        kind=kind,
        text=text,
    )


def supported_response(
    quote: str | None = None,
    confidence: float = 0.9,
    notes: str = "",
) -> str:
    """Return a JSON string representing a ``supported`` verdict."""
    return json.dumps(
        {
            "verdict": "supported",
            "confidence": confidence,
            "anchor_quote": quote,
            "reviewer_notes": notes,
        }
    )


def unsupported_response(confidence: float = 0.8, notes: str = "") -> str:
    """Return a JSON string representing an ``unsupported`` verdict."""
    return json.dumps(
        {
            "verdict": "unsupported",
            "confidence": confidence,
            "anchor_quote": None,
            "reviewer_notes": notes,
        }
    )


def partial_response(
    quote: str | None = None,
    confidence: float = 0.5,
    notes: str = "",
) -> str:
    """Return a JSON string representing a ``partial`` verdict."""
    return json.dumps(
        {
            "verdict": "partial",
            "confidence": confidence,
            "anchor_quote": quote,
            "reviewer_notes": notes,
        }
    )
