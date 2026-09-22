"""Accuracy-review reviewers for the paper2html pipeline.

Two reviewer types:
    ``SingleProviderReviewer``  — one provider, one deterministic pass.
    ``ConsensusReviewer``       — two providers (e.g. Claude + OpenAI); a claim
                                  is supported only when BOTH agree.

Factory:
    ``ReviewerFactory.build(config)`` — key-presence dispatch.

Design notes
------------
* Provider JSON is parsed by hand (json.loads + key validation), NOT Pydantic.
* Provider span integers are intentionally IGNORED — anchor spans are computed
  locally from ``str.find()`` to prevent fabrication.
* ``review()`` is a synchronous facade over ``review_async()``; async callers
  should use ``review_async()`` directly to avoid nested event-loop issues.
* Single-provider temperature: the #006 provider seam does not expose a
  temperature parameter, so single-provider mode is one deterministic pass.
  Callers requiring stochastic sampling must build their own retry logic.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

from paper2html.pipeline.providers.base import Message, ProviderInterface
from paper2html.review.prompts import (
    REVIEW_SYSTEM_PROMPT,
    _RETRY_PREFIX,
    build_review_user_prompt,
)
from paper2html.review.schemas import (
    Annotation,
    AnnotationBundle,
    ReviewedAnnotation,
    ReviewVerdict,
    SourceAnchor,
)

if TYPE_CHECKING:
    from paper2html.config import Config

logger = logging.getLogger(__name__)

# Per-provider default model constants (used when ConsensusReviewer needs to
# call a provider whose model was NOT configured by the user).
# config.py defines PROVIDER_DEFAULTS but we also keep local constants here for
# the cross-provider case where the other side must use its own default.
_DEFAULT_ANTHROPIC_MODEL: str = "claude-opus-4-8"
_DEFAULT_OPENAI_MODEL: str = "gpt-4o"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_provider_json(raw: str) -> dict:
    """Parse a provider response string into a dict.

    Args:
        raw: Raw string content from ``ProviderResponse.content``.

    Returns:
        Parsed dict.

    Raises:
        ValueError: If ``raw`` is not valid JSON or not a JSON object.
    """
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Provider response is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"Provider response is not a JSON object: {type(parsed)}")
    return parsed


def _build_anchor(
    quote: str | None, source_text: str, section_id: str
) -> SourceAnchor | None:
    """Build a ``SourceAnchor`` from a provider-supplied quote.

    Fabrication guard: returns ``None`` whenever the quote cannot be located
    verbatim in ``source_text``.  Provider-supplied span integers are NEVER
    trusted — offsets are computed locally.

    Args:
        quote: Verbatim substring candidate supplied by the provider.
        source_text: Plain-text of the annotation's source section.
        section_id: Section identifier for the anchor.

    Returns:
        A ``SourceAnchor`` if the quote is found; ``None`` otherwise.
    """
    if not quote:
        return None
    quote = quote.strip()
    if not quote:
        return None
    # Truncate to 120 chars AFTER stripping
    quote = quote[:120]
    if quote not in source_text:
        return None
    span_start = source_text.find(quote)
    span_end = span_start + len(quote)
    return SourceAnchor(
        section_id=section_id,
        span_start=span_start,
        span_end=span_end,
        quote=quote,
    )


async def _call_one(
    provider: ProviderInterface,
    annotation: Annotation,
    section_text: str,
    *,
    model: str,
) -> dict:
    """Call a single provider to review one annotation.

    On JSON parse failure retries once with ``_RETRY_PREFIX``.  A second
    failure returns a safe ``unsupported`` fallback dict — never raises.

    Args:
        provider: Provider to call.
        annotation: The annotation to review.
        section_text: Plain-text of the annotation's own section.
        model: Model identifier to use for this call.

    Returns:
        A dict with at least a ``"verdict"`` key.
    """
    system_msg = Message(role="system", content=REVIEW_SYSTEM_PROMPT)
    user_content = build_review_user_prompt(
        annotation_id=annotation.annotation_id,
        annotation_kind=annotation.kind,
        annotation_text=annotation.text,
        section_text=section_text,
    )
    user_msg = Message(role="user", content=user_content)
    messages = [system_msg, user_msg]

    _safe_fallback: dict = {
        "verdict": "unsupported",
        "confidence": 0.0,
        "anchor_quote": None,
        "reviewer_notes": "parse failure after retry",
    }

    # Review is a standalone API with caller-owned provider lifecycle. It does
    # not share a pipeline BudgetTracker or its cap; transport retry defaults
    # apply here, unlike admitted pipeline/figure requests.
    for attempt in range(2):
        response = await provider.complete(
            messages=messages,
            images=[],
            tools=[],
            model=model,
        )
        try:
            parsed = _parse_provider_json(response.content)
            return parsed
        except ValueError as exc:
            logger.warning(
                "Review JSON parse failure for annotation '%s' (attempt %d/2): %s",
                annotation.annotation_id,
                attempt + 1,
                exc,
            )
            if attempt == 0:
                retry_content = _RETRY_PREFIX + messages[-1].content
                messages[-1] = Message(role="user", content=retry_content)
            else:
                return _safe_fallback

    return _safe_fallback  # pragma: no cover


def _make_verdict(
    annotation: Annotation,
    raw: dict,
    source_text: str,
) -> ReviewVerdict:
    """Convert a raw provider response dict into a ``ReviewVerdict``.

    Business rules applied here:
    - ``verdict`` strictly required; invalid/missing → ``"unsupported"``.
    - ``confidence`` missing or invalid → 0.0; clamped to [0.0, 1.0].
    - Provider span integers are always ignored; anchor built from quote only.
    - ``"supported"`` with no valid anchor → downgraded to ``"unsupported"``.
    - ``"partial"`` anchor: validated if quote present, else ``None``.

    Args:
        annotation: Source annotation.
        raw: Parsed provider JSON dict.
        source_text: Plain-text of the annotation's own section.

    Returns:
        A ``ReviewVerdict`` with clamped confidence and validated anchor.
    """
    _valid_verdicts = {"supported", "unsupported", "partial"}
    raw_verdict = raw.get("verdict")
    verdict: str = raw_verdict if raw_verdict in _valid_verdicts else "unsupported"

    raw_confidence = raw.get("confidence", 0.0)
    try:
        confidence = float(raw_confidence)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    anchor_quote = raw.get("anchor_quote")  # may be None or absent
    reviewer_notes: str = raw.get("reviewer_notes") or ""

    anchor: SourceAnchor | None = None
    if verdict in ("supported", "partial"):
        anchor = _build_anchor(anchor_quote, source_text, annotation.section_id)

    # Downgrade: supported with no valid anchor → unsupported
    if verdict == "supported" and anchor is None:
        verdict = "unsupported"

    return ReviewVerdict(
        annotation_id=annotation.annotation_id,
        verdict=verdict,  # type: ignore[arg-type]
        anchor=anchor,
        confidence=confidence,
        reviewer_notes=reviewer_notes,
    )


def _finalize_verdict(
    annotation: Annotation,
    verdict: ReviewVerdict,
) -> ReviewVerdict:
    """Log a DEBUG line for every non-supported final verdict and return unchanged.

    Logging lives here (NOT in ``AnnotationBundle.accepted()``) so the drop is
    recorded exactly once at verdict-finalization time.

    Args:
        annotation: Source annotation (used for the log message only).
        verdict: The finalized verdict.

    Returns:
        The same ``verdict`` object, unchanged.
    """
    if verdict.verdict != "supported":
        logger.debug(
            "Dropped annotation %s: verdict=%s",
            annotation.annotation_id,
            verdict.verdict,
        )
    return verdict


# ---------------------------------------------------------------------------
# Consensus merge helpers
# ---------------------------------------------------------------------------

_CONSENSUS_TABLE: dict[tuple[str, str], str] = {
    ("supported", "supported"): "supported",
    ("partial", "partial"): "partial",
    # All other combinations → unsupported
}


def _merge_consensus(
    annotation: Annotation,
    raw_a: dict,
    raw_b: dict,
    source_text: str,
) -> ReviewVerdict:
    """Merge two provider verdicts using the 9-cell consensus table.

    Full merge table:
        (supported, supported) → supported   (anchor: A then B, else partial)
        (partial,   partial)   → partial
        all other combinations → unsupported

    Confidence = min(a, b).

    Args:
        annotation: Source annotation.
        raw_a: Parsed JSON dict from provider A.
        raw_b: Parsed JSON dict from provider B.
        source_text: Plain-text of the annotation's own section.

    Returns:
        Merged ``ReviewVerdict``.
    """
    _valid = {"supported", "unsupported", "partial"}

    v_a_raw = raw_a.get("verdict")
    v_b_raw = raw_b.get("verdict")
    v_a: str = v_a_raw if v_a_raw in _valid else "unsupported"
    v_b: str = v_b_raw if v_b_raw in _valid else "unsupported"

    merged_verdict_str: str = _CONSENSUS_TABLE.get((v_a, v_b), "unsupported")

    # Confidence
    def _clamp_conf(raw: dict) -> float:
        try:
            c = float(raw.get("confidence", 0.0))
        except (TypeError, ValueError):
            c = 0.0
        return max(0.0, min(1.0, c))

    conf_a = _clamp_conf(raw_a)
    conf_b = _clamp_conf(raw_b)
    confidence = min(conf_a, conf_b)

    reviewer_notes = (
        f"A={raw_a.get('reviewer_notes', '')} B={raw_b.get('reviewer_notes', '')}"
    ).strip()

    anchor: SourceAnchor | None = None
    if merged_verdict_str == "supported":
        # Try A's anchor first, then B's
        anchor = _build_anchor(
            raw_a.get("anchor_quote"), source_text, annotation.section_id
        )
        if anchor is None:
            anchor = _build_anchor(
                raw_b.get("anchor_quote"), source_text, annotation.section_id
            )
        # spec rule 5: supported with no valid anchor → downgrade to partial
        if anchor is None:
            merged_verdict_str = "partial"
    elif merged_verdict_str == "partial":
        # Try to get an anchor for partial
        anchor = _build_anchor(
            raw_a.get("anchor_quote"), source_text, annotation.section_id
        )
        if anchor is None:
            anchor = _build_anchor(
                raw_b.get("anchor_quote"), source_text, annotation.section_id
            )

    return ReviewVerdict(
        annotation_id=annotation.annotation_id,
        verdict=merged_verdict_str,  # type: ignore[arg-type]
        anchor=anchor,
        confidence=confidence,
        reviewer_notes=reviewer_notes,
    )


# ---------------------------------------------------------------------------
# Reviewer classes
# ---------------------------------------------------------------------------


class SingleProviderReviewer:
    """Verifies annotations using a single provider.

    Note on temperature: the #006 provider seam does not expose a temperature
    parameter, so this reviewer performs one deterministic pass per annotation.
    Callers requiring stochastic sampling must build their own retry logic.

    Args:
        provider: The ``ProviderInterface`` to call.
        model: Model identifier string.
    """

    def __init__(self, provider: ProviderInterface, model: str) -> None:
        self._provider = provider
        self._model = model

    def review(
        self,
        annotations: list[Annotation],
        source_sections: dict[str, str],
    ) -> AnnotationBundle:
        """Synchronous facade over ``review_async``.

        Note: raises ``RuntimeError`` if called from inside a running event
        loop.  Async callers (e.g. the pipeline orchestrator) should call
        ``review_async`` directly.

        Args:
            annotations: Annotations to review.
            source_sections: Mapping of section_id → plain-text.

        Returns:
            ``AnnotationBundle`` with all reviewed annotations.
        """
        return asyncio.run(self.review_async(annotations, source_sections))

    async def review_async(
        self,
        annotations: list[Annotation],
        source_sections: dict[str, str],
    ) -> AnnotationBundle:
        """Async implementation of the review pass.

        Args:
            annotations: Annotations to review.
            source_sections: Mapping of section_id → plain-text.

        Returns:
            ``AnnotationBundle`` with all reviewed annotations.
        """
        reviewed: list[ReviewedAnnotation] = []
        for annotation in annotations:
            section_text = source_sections.get(annotation.section_id)
            if section_text is None:
                logger.debug(
                    "Dropped annotation %s: verdict=unsupported (section '%s' not found)",
                    annotation.annotation_id,
                    annotation.section_id,
                )
                verdict = ReviewVerdict(
                    annotation_id=annotation.annotation_id,
                    verdict="unsupported",
                    anchor=None,
                    confidence=0.0,
                    reviewer_notes=f"section '{annotation.section_id}' not in source_sections",
                )
                reviewed.append(
                    ReviewedAnnotation(annotation=annotation, verdict=verdict)
                )
                continue

            raw = await _call_one(
                self._provider, annotation, section_text, model=self._model
            )
            verdict = _make_verdict(annotation, raw, section_text)
            verdict = _finalize_verdict(annotation, verdict)
            reviewed.append(ReviewedAnnotation(annotation=annotation, verdict=verdict))

        return AnnotationBundle(items=reviewed)


class ConsensusReviewer:
    """Verifies annotations using two providers in parallel.

    A claim is ``"supported"`` only when both providers agree it is supported.
    See ``_merge_consensus`` for the full 9-cell merge table.

    Args:
        provider_a: First provider (typically Anthropic/Claude).
        provider_b: Second provider (typically OpenAI).
        model_a: Model string for provider A.
        model_b: Model string for provider B.
    """

    def __init__(
        self,
        provider_a: ProviderInterface,
        provider_b: ProviderInterface,
        model_a: str,
        model_b: str,
    ) -> None:
        self._provider_a = provider_a
        self._provider_b = provider_b
        self._model_a = model_a
        self._model_b = model_b

    def review(
        self,
        annotations: list[Annotation],
        source_sections: dict[str, str],
    ) -> AnnotationBundle:
        """Synchronous facade over ``review_async``.

        Note: raises ``RuntimeError`` if called from inside a running event
        loop.  Async callers should call ``review_async`` directly.

        Args:
            annotations: Annotations to review.
            source_sections: Mapping of section_id → plain-text.

        Returns:
            ``AnnotationBundle`` with all reviewed annotations.
        """
        return asyncio.run(self.review_async(annotations, source_sections))

    async def review_async(
        self,
        annotations: list[Annotation],
        source_sections: dict[str, str],
    ) -> AnnotationBundle:
        """Async implementation using parallel provider calls.

        Args:
            annotations: Annotations to review.
            source_sections: Mapping of section_id → plain-text.

        Returns:
            ``AnnotationBundle`` with all reviewed annotations.
        """
        reviewed: list[ReviewedAnnotation] = []
        for annotation in annotations:
            section_text = source_sections.get(annotation.section_id)
            if section_text is None:
                logger.debug(
                    "Dropped annotation %s: verdict=unsupported (section '%s' not found)",
                    annotation.annotation_id,
                    annotation.section_id,
                )
                verdict = ReviewVerdict(
                    annotation_id=annotation.annotation_id,
                    verdict="unsupported",
                    anchor=None,
                    confidence=0.0,
                    reviewer_notes=f"section '{annotation.section_id}' not in source_sections",
                )
                reviewed.append(
                    ReviewedAnnotation(annotation=annotation, verdict=verdict)
                )
                continue

            raw_a, raw_b = await asyncio.gather(
                _call_one(
                    self._provider_a, annotation, section_text, model=self._model_a
                ),
                _call_one(
                    self._provider_b, annotation, section_text, model=self._model_b
                ),
            )

            verdict = _merge_consensus(annotation, raw_a, raw_b, section_text)
            verdict = _finalize_verdict(annotation, verdict)
            reviewed.append(ReviewedAnnotation(annotation=annotation, verdict=verdict))

        return AnnotationBundle(items=reviewed)


class ReviewerFactory:
    """Constructs the appropriate reviewer based on available API keys.

    Dispatch logic:
        Two keys (Anthropic + OpenAI) → ``ConsensusReviewer``
        One key (either)              → ``SingleProviderReviewer``
        Zero keys                     → ``ValueError``

    Args:
        config: ``Config`` instance with API key and model fields.
        providers: Optional tuple ``(provider_a, provider_b)`` for test
            injection.  When ``None``, real providers are constructed lazily.
    """

    @staticmethod
    def build(
        config: "Config",
        providers: tuple[ProviderInterface, ...] | None = None,
    ) -> "SingleProviderReviewer | ConsensusReviewer":
        """Build and return the appropriate reviewer.

        Args:
            config: Runtime configuration (keys, model).
            providers: Test injection — ordered tuple of provider instances.
                When given, skips real provider construction entirely.

        Returns:
            ``ConsensusReviewer`` when both API keys are present, otherwise
            ``SingleProviderReviewer``.

        Raises:
            ValueError: If no API key is configured.
        """
        has_anthropic = bool(config.anthropic_api_key)
        has_openai = bool(config.openai_api_key)

        if not has_anthropic and not has_openai:
            raise ValueError("no provider API key configured")

        if has_anthropic and has_openai:
            # Two-model fix (MUST-FIX 1): each provider gets its own model.
            # The configured provider's side gets config.model; the OTHER side
            # gets that provider's own default model.
            if config.provider == "claude":
                model_a = config.model  # configured side: Anthropic/Claude
                model_b = _DEFAULT_OPENAI_MODEL  # other side: OpenAI default
            elif config.provider == "openai":
                model_a = _DEFAULT_ANTHROPIC_MODEL  # other side: Anthropic default
                model_b = config.model  # configured side: OpenAI
            else:
                # Unknown provider — use defaults for both sides
                model_a = _DEFAULT_ANTHROPIC_MODEL
                model_b = _DEFAULT_OPENAI_MODEL

            if providers is not None:
                provider_a, provider_b = providers[0], providers[1]
            else:
                # Lazy imports to avoid forcing SDK deps on test runs that
                # inject providers via the `providers` kwarg.
                from paper2html.pipeline.providers.anthropic import AnthropicProvider  # type: ignore[import]
                from paper2html.pipeline.providers.openai import OpenAIProvider  # type: ignore[import]

                provider_a = AnthropicProvider(api_key=config.anthropic_api_key)
                provider_b = OpenAIProvider(api_key=config.openai_api_key)

            return ConsensusReviewer(
                provider_a=provider_a,
                provider_b=provider_b,
                model_a=model_a,
                model_b=model_b,
            )
        else:
            # Single provider
            if has_anthropic:
                model = (
                    config.model
                    if config.provider == "claude"
                    else _DEFAULT_ANTHROPIC_MODEL
                )
                if providers is not None:
                    provider = providers[0]
                else:
                    from paper2html.pipeline.providers.anthropic import (
                        AnthropicProvider,
                    )  # type: ignore[import]

                    provider = AnthropicProvider(api_key=config.anthropic_api_key)
            else:
                # has_openai
                model = (
                    config.model
                    if config.provider == "openai"
                    else _DEFAULT_OPENAI_MODEL
                )
                if providers is not None:
                    provider = providers[0]
                else:
                    from paper2html.pipeline.providers.openai import OpenAIProvider  # type: ignore[import]

                    provider = OpenAIProvider(api_key=config.openai_api_key)

            return SingleProviderReviewer(provider=provider, model=model)
