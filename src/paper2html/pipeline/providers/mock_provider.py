"""Deterministic mock provider for CI testing.

Design notes
------------
* Failure tracking is per content-key (sha256 of user message content + model),
  NOT per overall ``call_count``.  ``FailMode.FIRST_CALL`` / ``TARGET_SECTION``
  return malformed JSON on the FIRST call for that content key and valid JSON on
  subsequent calls.  This ensures the retry prefix changes the hash, so the
  second call (with the "return valid JSON" prefix) is a new key and gets valid
  JSON.
* The mock branches on marker strings in the user prompt:
  - ``PLANNING`` marker + ``spawn_subquery`` in tools → planning call
  - ``SYNTHESIS`` marker → synthesis call
  - Otherwise → worker call (section_id extracted from ``SECTION_ID: <id>`` line)
* ``spawn_subquery_section`` (set on the mock): when set AND the planning call
  is being processed, the mock returns a ``tool_use`` stop with a
  ``spawn_subquery`` ``ToolCall`` for that section.
"""

from __future__ import annotations

import hashlib
import json
import logging
from enum import Enum
from typing import Any

from paper2html.pipeline.providers.base import (
    validate_request_limits,
    Message,
    ProviderInterface,
    ProviderResponse,
    ToolCall,
    ToolDef,
)

logger = logging.getLogger(__name__)


class FailMode(Enum):
    """Controls when the mock returns malformed JSON."""

    NONE = "none"
    """Always return valid responses."""

    FIRST_CALL = "first_call"
    """Return malformed JSON on the first call for each unique content key."""

    ALL_CALLS = "all_calls"
    """Always return malformed JSON (test exhausted-retry path)."""

    TARGET_SECTION = "target_section"
    """Return malformed JSON only for a specific section (first call only)."""


_MALFORMED_JSON = 'not valid json {"'


class MockProvider(ProviderInterface):
    """Deterministic mock provider — no network calls, no API keys required.

    Args:
        fail_mode: Controls failure injection strategy.
        target_section_id: Required when ``fail_mode == TARGET_SECTION``.
            Malformed JSON is returned on the first worker call for this section.
        spawn_subquery_section: When set, the planning call returns a
            ``spawn_subquery`` tool call targeting this section id.
    """

    free_of_charge = True

    def __init__(
        self,
        fail_mode: FailMode = FailMode.NONE,
        target_section_id: str | None = None,
        spawn_subquery_section: str | None = None,
    ) -> None:
        self.fail_mode = fail_mode
        self.target_section_id = target_section_id
        self.spawn_subquery_section = spawn_subquery_section

        self.call_count: int = 0
        self._calls: list[tuple[list[Message], list[bytes], list[ToolDef], str]] = []
        # Tracks how many times each content-key has been called
        self._key_call_counts: dict[str, int] = {}
        # Tracks per-section call counts (for TARGET_SECTION)
        self._section_call_counts: dict[str, int] = {}

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
        """Return a deterministic response based on the message content.

        Args:
            messages: Conversation history.
            images: Ignored (mock does not process images).
            tools: Tool definitions (used to detect planning call).
            model: Model identifier (included in the response hash).

        Returns:
            Deterministic ``ProviderResponse``.
        """
        validate_request_limits(max_output_tokens, request_timeout_seconds)
        self.call_count += 1
        self._calls.append((messages, images, tools, model))

        user_content = messages[-1].content if messages else ""
        content_key = hashlib.sha256((user_content + model).encode()).hexdigest()

        # Increment per-key count
        self._key_call_counts[content_key] = (
            self._key_call_counts.get(content_key, 0) + 1
        )

        # Detect call type from markers
        is_planning = "PLANNING" in user_content and any(
            t.name == "spawn_subquery" for t in tools
        )
        is_synthesis = "SYNTHESIS" in user_content

        # Extract section id for worker calls
        section_id = self._extract_section_id(user_content)
        if section_id:
            self._section_call_counts[section_id] = (
                self._section_call_counts.get(section_id, 0) + 1
            )

        # Decide whether to return malformed JSON
        if self._should_fail(content_key, section_id, is_planning, is_synthesis):
            return ProviderResponse(
                content=_MALFORMED_JSON,
                input_tokens=10,
                output_tokens=5,
                stop_reason="end_turn",
            )

        # Return deterministic valid response based on call type
        if is_planning:
            return self._planning_response(section_id or "")
        if is_synthesis:
            return self._synthesis_response()
        return self._worker_response(section_id or "unknown")

    # ------------------------------------------------------------------
    # Private helpers — failure logic
    # ------------------------------------------------------------------

    def _should_fail(
        self,
        content_key: str,
        section_id: str | None,
        is_planning: bool,
        is_synthesis: bool,
    ) -> bool:
        """Return True if this call should return malformed JSON.

        For FIRST_CALL and TARGET_SECTION modes the failure is tracked per
        section id so that a retry (which changes the content hash via the
        "return valid JSON" prefix) is correctly treated as a second call for
        the same section and returns valid JSON — regardless of the new key.

        Args:
            content_key: SHA-256 key for this specific message content.
            section_id: Extracted section id (may be None for planning/synthesis).
            is_planning: Whether this is a planning call.
            is_synthesis: Whether this is a synthesis call.

        Returns:
            True if malformed JSON should be returned.
        """
        if self.fail_mode == FailMode.NONE:
            return False
        if self.fail_mode == FailMode.ALL_CALLS:
            return True
        if self.fail_mode == FailMode.FIRST_CALL:
            # Track per section_id (not content key) so that the retry prefix
            # (which changes the hash) is still treated as "second call for this section"
            if section_id is not None:
                return self._section_call_counts.get(section_id, 0) == 1
            # Non-worker calls (planning/synthesis): track by content key
            return self._key_call_counts.get(content_key, 0) == 1
        if self.fail_mode == FailMode.TARGET_SECTION:
            if section_id != self.target_section_id:
                return False
            # Fail only on first call for the target section
            return self._section_call_counts.get(section_id, 0) == 1
        return False

    # ------------------------------------------------------------------
    # Private helpers — response construction
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_section_id(content: str) -> str | None:
        """Extract section id from a ``SECTION_ID: <id>`` marker line.

        Args:
            content: User message content.

        Returns:
            Section id string, or ``None`` if not found.
        """
        for line in content.splitlines():
            line = line.strip()
            if line.startswith("SECTION_ID:"):
                return line.split(":", 1)[1].strip()
        return None

    def _planning_response(self, section_id: str) -> ProviderResponse:
        """Return a planning-phase response.

        When ``spawn_subquery_section`` is set on the mock, returns a
        ``tool_use`` response with a ``spawn_subquery`` call.

        Args:
            section_id: Section id in the planning prompt (unused here).

        Returns:
            Planning ``ProviderResponse``.
        """
        if self.spawn_subquery_section:
            tool_call = ToolCall(
                id="tc-mock-001",
                name="spawn_subquery",
                arguments={
                    "section_id": self.spawn_subquery_section,
                    "focus_query": "What are the main technical contributions?",
                    "text_slice": "mock text slice for sub-query",
                },
            )
            return ProviderResponse(
                content="",
                tool_calls=[tool_call],
                input_tokens=100,
                output_tokens=50,
                stop_reason="tool_use",
            )
        # Plain text plan — no tool call
        return ProviderResponse(
            content="PLANNING: proceed with flat section dispatch.",
            input_tokens=100,
            output_tokens=30,
            stop_reason="end_turn",
        )

    @staticmethod
    def _synthesis_response() -> ProviderResponse:
        """Return a synthesis-phase response with overall summary JSON.

        Returns:
            Synthesis ``ProviderResponse``.
        """
        payload = {
            "overall_summary": "Mock overall paper summary.",
            "key_contributions": ["Contribution A", "Contribution B"],
            "terminology": {"term_a": "definition a", "term_b": "definition b"},
        }
        return ProviderResponse(
            content=json.dumps(payload),
            input_tokens=200,
            output_tokens=80,
            stop_reason="end_turn",
        )

    @staticmethod
    def _worker_response(section_id: str) -> ProviderResponse:
        """Return a valid ``WorkerOutput``-shaped JSON response.

        Args:
            section_id: The section id to embed in the response.

        Returns:
            Worker ``ProviderResponse``.
        """
        payload: dict[str, Any] = {
            "schema_version": "1.0",
            "section_id": section_id,
            "summary": f"Mock summary for section {section_id}.",
            "key_claims": [f"Claim 1 for {section_id}", f"Claim 2 for {section_id}"],
            "term_definitions": [
                {"term": f"term_{section_id}", "definition": "mock definition"}
            ],
            "summary_skim": f"Mock skim summary for {section_id}.",
            "summary_read": f"Mock read summary for {section_id}.",
            "summary_deep": f"Mock deep summary for {section_id}.",
            "margin_notes": [f"Mock margin note for {section_id}."],
        }
        return ProviderResponse(
            content=json.dumps(payload),
            input_tokens=50,
            output_tokens=40,
            stop_reason="end_turn",
        )
