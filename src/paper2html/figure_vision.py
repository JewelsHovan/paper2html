"""Figure vision analysis: per-figure vision worker producing alt-text, semantic
gloss, and bidirectional source anchors.

Design notes
------------
* Provider calls go EXCLUSIVELY through the ticket-006 ``ProviderInterface`` —
  this module must never import ``anthropic`` or ``openai`` directly.
* Input images must be JPEG or PNG raster bytes.  SVG input raises ``ValueError``
  before any provider call.
* ``FigureAnalysisError`` is defined here (single definition); it is NOT
  re-exported from ``paper2html.models``.
* Sentence-id derivation replicates the ``text_content.split(". ")`` 1-indexed
  scheme from ``from_arxiv_html`` exactly.  Any deviation produces wrong anchors.
* Retry pattern mirrors ``worker.py``: one retry with a prefix on the user
  message; two consecutive failures raise ``FigureAnalysisError``.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from paper2html.models.document import Figure, NormalizedDocument
from paper2html.models.figure_analysis import FigureAnalysis
from paper2html.pipeline.budget import BudgetTracker
from paper2html.pipeline.errors import PipelineError, ProviderError
from paper2html.pipeline.providers.base import Message, ProviderInterface

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Error type
# ---------------------------------------------------------------------------


class FigureAnalysisError(PipelineError, RuntimeError):
    """Raised when figure vision analysis fails.

    Attributes:
        figure_id: The id of the figure that could not be analysed.
        cause: The underlying exception.
    """

    def __init__(self, figure_id: str, cause: Exception) -> None:
        super().__init__(f"Figure analysis failed for {figure_id!r}: {cause}")
        self.figure_id = figure_id
        self.cause = cause


# ---------------------------------------------------------------------------
# Prompt constants
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "You are a precise academic paper figure analyser.\n"
    "You receive one figure from a research paper and must return a structured"
    " JSON analysis of that figure only.\n"
    "Your response MUST be a single valid JSON object with EXACTLY these keys:\n"
    '  "alt_text": <string, ≤150 chars, screen-reader quality>,\n'
    '  "gloss": <string, 1–3 sentences capturing trend/comparison/takeaway —'
    " DO NOT merely restate the caption>,\n"
    '  "confidence": <float 0.0–1.0>\n'
    "Do not include any text outside the JSON object."
)

_RETRY_PREFIX = (
    "IMPORTANT: return valid JSON matching the schema exactly. "
    "Your previous response was not valid JSON or was missing required fields.\n"
)

_MAX_ALT_TEXT_CHARS = 150
_MAX_ANCHOR_SENTENCES = 5
_MAX_ANCHOR_TEXT_CHARS = 200

# Caption regex: matches "Fig 3", "Figure 3a", "fig. 3", etc.
_CAPTION_NUM_RE = re.compile(r"(?i)^\s*fig(?:ure)?\.?\s*(\d+[a-z]?)")


# ---------------------------------------------------------------------------
# Transient intermediate — used internally by the worker
# ---------------------------------------------------------------------------


@dataclass
class _PartialAnalysis:
    """Transient intermediate result from a successful provider call."""

    alt_text: str
    gloss: str
    confidence: float | None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _derive_figure_number(figure: Figure) -> str | None:
    """Derive the figure number used in textual references.

    Precedence:
    1. Caption prefix regex: ``r'(?i)^\\s*fig(?:ure)?\\.?\\s*(\\d+[a-z]?)'``
    2. Numeric tail of ``figure.id`` (e.g. ``"fig-3"`` → ``"3"``).
    3. ``figure.label`` if it matches ``^\\d+[a-z]?$``.
    4. ``None`` — debug-logged.

    Args:
        figure: The ``Figure`` to inspect.

    Returns:
        Figure number string (e.g. ``"3"`` or ``"3a"``), or ``None``.
    """
    caption = figure.caption or ""
    m = _CAPTION_NUM_RE.match(caption)
    if m:
        return m.group(1)

    # Fallback 1: numeric tail of id ("fig-3" → "3")
    id_tail = figure.id.rsplit("-", 1)[-1]
    if re.match(r"^\d+[a-z]?$", id_tail):
        return id_tail

    # Fallback 2: label
    label = figure.label or ""
    if re.match(r"^\d+[a-z]?$", label):
        return label

    logger.debug(
        "Could not derive figure number for %r (caption=%r, label=%r)",
        figure.id,
        caption,
        label,
    )
    return None


def _extract_anchors(figure: Figure, doc: NormalizedDocument) -> list[str]:
    """Derive sentence ids that reference this figure.

    If no figure number can be derived, falls back to
    ``figure.referencing_sentence_ids``.

    The sentence-id scheme replicates ``from_arxiv_html`` exactly:
    ``f"{section.id}-sent-{i}"`` for ``i`` in 1..N where sentences are
    produced by ``text_content.split(". ")``.

    Args:
        figure: The figure being analysed.
        doc: The surrounding normalised document.

    Returns:
        Ordered, deduplicated list of sentence ids — regex hits first, then
        any additional ids from ``figure.referencing_sentence_ids``.
    """
    number = _derive_figure_number(figure)
    if number is None:
        return list(figure.referencing_sentence_ids)

    # Compile pattern: "\bFig. 3\b", "\bFigure 3\b", etc.
    pattern = re.compile(r"(?i)\bfig(?:ure)?\.?\s*" + re.escape(number) + r"\b")

    seen: set[str] = set()
    ordered: list[str] = []

    for section in doc.sections:
        sentences = section.text_content.split(". ")
        for i, sent in enumerate(sentences, start=1):
            sent_id = f"{section.id}-sent-{i}"
            if pattern.search(sent) and sent_id not in seen:
                seen.add(sent_id)
                ordered.append(sent_id)

    # Union with existing referencing_sentence_ids (order-preserving dedup)
    for sent_id in figure.referencing_sentence_ids:
        if sent_id not in seen:
            seen.add(sent_id)
            ordered.append(sent_id)

    return ordered


def _get_sentence_texts(
    anchors: list[str],
    doc: NormalizedDocument,
    max_count: int = _MAX_ANCHOR_SENTENCES,
    max_chars: int = _MAX_ANCHOR_TEXT_CHARS,
) -> list[str]:
    """Retrieve the actual sentence text for the first ``max_count`` anchors.

    Args:
        anchors: Sentence ids to look up.
        doc: Document to search.
        max_count: Maximum number of sentences to return.
        max_chars: Maximum characters per sentence.

    Returns:
        List of sentence text strings, truncated to ``max_chars`` each.
    """
    sec_index: dict[str, list[str]] = {
        s.id: s.text_content.split(". ") for s in doc.sections
    }
    texts: list[str] = []
    for sent_id in anchors[:max_count]:
        # sent_id format: "<section_id>-sent-<n>"
        parts = sent_id.rsplit("-sent-", 1)
        if len(parts) != 2:
            continue
        sec_id, n_str = parts
        try:
            n = int(n_str)
        except ValueError:
            continue
        sentences = sec_index.get(sec_id, [])
        if 1 <= n <= len(sentences):
            texts.append(sentences[n - 1][:max_chars])
    return texts


def _build_prompt_messages(
    figure: Figure,
    anchor_sentences: list[str],
) -> list[Message]:
    """Build the system + user messages for the vision provider call.

    Args:
        figure: The figure being analysed.
        anchor_sentences: Up to 5 referencing sentence texts (truncated).

    Returns:
        Two-element list ``[system_message, user_message]``.
    """
    system_msg = Message(role="system", content=_SYSTEM_PROMPT)

    user_parts: list[str] = []
    caption = figure.caption or ""
    if caption:
        user_parts.append(f"Caption: {caption}")

    if anchor_sentences:
        user_parts.append("\nReferencing sentences from the paper body:")
        for text in anchor_sentences:
            user_parts.append(f"  - {text}")

    user_parts.append(
        "\nAnalyse this figure and return a JSON object as described in the"
        " system prompt. Your gloss MUST capture the trend, comparison, or"
        " key takeaway — do NOT merely restate the caption."
    )
    user_msg = Message(role="user", content="\n".join(user_parts))
    return [system_msg, user_msg]


def _coerce_confidence(raw: object) -> float | None:
    """Coerce ``raw`` to a float in [0, 1], returning ``None`` on failure.

    Args:
        raw: Provider-supplied confidence value (any type).

    Returns:
        Float in [0.0, 1.0] or ``None``.
    """
    if raw is None:
        return None
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, value))


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


class FigureVisionWorker:
    """Async worker that produces a :class:`FigureAnalysis` for one figure.

    Args:
        provider: Provider interface (ticket 006).  Must not be a vendor SDK.
        model: Model identifier string.
        budget: Shared admission tracker if supplied. Otherwise this analyzer owns
            a separate unlimited tracker; its spend is not part of a pipeline run.
    """

    def __init__(
        self,
        provider: ProviderInterface,
        *,
        model: str,
        budget: BudgetTracker | None = None,
        max_output_tokens: int = 8192,
        request_timeout_seconds: float = 60.0,
    ) -> None:
        self._provider = provider
        self._model = model
        self._budget = budget if budget is not None else BudgetTracker()
        self._max_output_tokens = max_output_tokens
        self._request_timeout_seconds = request_timeout_seconds

    async def analyze(
        self,
        figure: Figure,
        document: NormalizedDocument,
    ) -> FigureAnalysis:
        """Produce a :class:`FigureAnalysis` for *figure*.

        Args:
            figure: The figure to analyse.  Must carry non-empty JPEG or PNG bytes.
            document: The surrounding document (used for anchor extraction).

        Returns:
            Populated :class:`FigureAnalysis`.

        Raises:
            ValueError: If ``figure.image_bytes`` is empty or SVG.
            FigureAnalysisError: If the provider fails or returns invalid JSON
                after one retry.
        """
        # MUST-FIX 1 — empty bytes guard (BEFORE everything else)
        if not figure.image_bytes:
            raise ValueError(
                f"Figure {figure.id!r} has empty image_bytes"
                " (upstream rasterization produced no image)"
            )

        # MUST-FIX 3 — robust SVG detection (before provider call)
        self._reject_svg(figure)

        # Derive anchors and prompt context
        anchors = _extract_anchors(figure, document)
        anchor_texts = _get_sentence_texts(anchors, document)
        messages = _build_prompt_messages(figure, anchor_texts)

        # Call provider with up to one retry
        try:
            partial = await self._call_with_retry_inner(figure, messages)
        except ProviderError as exc:
            raise FigureAnalysisError(figure.id, exc) from exc

        return FigureAnalysis(
            figure_id=figure.id,
            alt_text=partial.alt_text,
            gloss=partial.gloss,
            source_anchors=anchors,
            provider_model=self._model,
            confidence=partial.confidence,
        )

    @staticmethod
    def _reject_svg(figure: Figure) -> None:
        """Raise ``ValueError`` if the image bytes look like SVG.

        Checks:
        a. Stripped bytes start with ``b'<svg'`` (case-insensitive prefix).
        b. Stripped bytes start with ``b'<?xml'`` AND contain ``b'<svg'``
           in the first 512 bytes.
        c. ``'svg'`` appears in the mime type (catches ``image/svg+xml``).

        Args:
            figure: The figure whose bytes/mime to inspect.

        Raises:
            ValueError: Always, with the figure id in the message.
        """
        mime = figure.image_mime_type or ""
        if "svg" in mime.lower():
            raise ValueError(
                f"Figure {figure.id!r} has SVG mime type {mime!r};"
                " upstream rasterization must convert to JPEG/PNG before"
                " figure vision analysis."
            )

        stripped = figure.image_bytes.lstrip()
        # Check first 4 bytes case-insensitively for "<svg"
        if stripped[:4].lower() == b"<svg":
            raise ValueError(
                f"Figure {figure.id!r} image_bytes appear to be SVG"
                " (starts with '<svg'); upstream rasterization failed."
            )

        if stripped.startswith(b"<?xml"):
            head = stripped[:512]
            if b"<svg" in head or b"<SVG" in head:
                raise ValueError(
                    f"Figure {figure.id!r} image_bytes appear to be SVG"
                    " (XML prolog + '<svg' in first 512 bytes);"
                    " upstream rasterization failed."
                )

    async def _call_with_retry_inner(
        self,
        figure: Figure,
        messages: list[Message],
    ) -> _PartialAnalysis:
        """Attempt up to 2 provider calls, retrying once on parse/validation failure.

        Args:
            figure: The figure being analysed (for error context).
            messages: The ``[system, user]`` message list; the user message is
                replaced on retry with the retry prefix prepended.

        Returns:
            :class:`_PartialAnalysis` with alt_text, gloss, and confidence.

        Raises:
            FigureAnalysisError: After two consecutive failures.
            ProviderError: Propagated up to ``analyze()`` which wraps it.
        """
        last_exc: Exception | None = None

        for attempt in range(2):
            response = await self._budget.complete(
                self._provider,
                messages=messages,
                images=[figure.image_bytes],
                tools=[],
                model=self._model,
                max_output_tokens=self._max_output_tokens,
                request_timeout_seconds=self._request_timeout_seconds,
            )

            # Attempt JSON parse
            parsed: dict | None = None
            try:
                parsed = json.loads(response.content)
            except json.JSONDecodeError as exc:
                logger.warning(
                    "Figure vision JSON parse failure for %r (attempt %d/2): %s",
                    figure.id,
                    attempt + 1,
                    exc,
                )
                last_exc = exc

            if parsed is not None:
                alt_text = str(parsed.get("alt_text") or "")
                gloss = str(parsed.get("gloss") or "")
                if alt_text and gloss:
                    if len(alt_text) > _MAX_ALT_TEXT_CHARS:
                        logger.debug(
                            "alt_text for %r exceeds 150 chars (%d); truncating.",
                            figure.id,
                            len(alt_text),
                        )
                        alt_text = alt_text[:_MAX_ALT_TEXT_CHARS]

                    return _PartialAnalysis(
                        alt_text=alt_text,
                        gloss=gloss,
                        confidence=_coerce_confidence(parsed.get("confidence")),
                    )

                # Missing or empty required fields
                logger.warning(
                    "Figure vision response for %r missing alt_text/gloss"
                    " (attempt %d/2): %r",
                    figure.id,
                    attempt + 1,
                    parsed,
                )
                last_exc = ValueError(
                    f"Response missing required fields alt_text/gloss: {parsed!r}"
                )

            if attempt == 0:
                # Prepend retry prefix to the user message for the second call
                retry_content = _RETRY_PREFIX + messages[-1].content
                messages[-1] = Message(role="user", content=retry_content)
            else:
                raise FigureAnalysisError(
                    figure.id,
                    last_exc or ValueError("Unknown validation failure"),
                )

        # Should never reach here
        raise FigureAnalysisError(  # pragma: no cover
            figure.id,
            last_exc or ValueError("Unknown validation failure"),
        )


# ---------------------------------------------------------------------------
# Back-fill helper (SHOULD-FIX 10)
# ---------------------------------------------------------------------------


def apply_analysis(figure: Figure, analysis: FigureAnalysis) -> Figure:
    """Return a new :class:`Figure` with alt-text and gloss back-filled.

    The original ``figure`` is not mutated (Pydantic ``model_copy`` is used).

    Args:
        figure: Source figure (unchanged).
        analysis: Analysis result to apply.

    Returns:
        New ``Figure`` instance with ``alt_text``, ``gloss``, and
        ``referencing_sentence_ids`` updated from *analysis*.
    """
    return figure.model_copy(
        update={
            "alt_text": analysis.alt_text,
            "gloss": analysis.gloss,
            "referencing_sentence_ids": list(analysis.source_anchors),
        }
    )
