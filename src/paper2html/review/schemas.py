"""Data schemas for the accuracy-review stage.

All types are plain ``@dataclass`` — intentionally NOT Pydantic, matching the
provider interface design convention from ticket #006.

Contract with downstream HTML generator (#009):
    ``AnnotationBundle.accepted()`` returns only verified annotations.
    An annotation with verdict ``unsupported`` NEVER appears in the accepted list.
    A ``partial`` annotation appears only when ``accept_partial=True`` is passed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass
class Annotation:
    """Input annotation produced by section or figure-vision workers.

    Attributes:
        annotation_id: Unique identifier for this annotation.
        section_id: Section this annotation belongs to (e.g. ``"sec-1"``).
        kind: Annotation type tag (carry-through only, not validated here).
        text: The claim text to be verified.
    """

    annotation_id: str
    section_id: str
    kind: str
    text: str


@dataclass
class SourceAnchor:
    """Verbatim evidence span locating an annotation's claim in the source.

    Attributes:
        section_id: Matches the section ID from the normalized document model.
        span_start: Character offset (inclusive) into the section plain-text.
        span_end: Character offset (exclusive) into the section plain-text.
        quote: Verbatim substring from the source, at most 120 characters.
    """

    section_id: str
    span_start: int
    span_end: int
    quote: str


@dataclass
class ReviewVerdict:
    """Verdict produced by the review stage for a single annotation.

    Attributes:
        annotation_id: Mirrors the source annotation's id.
        verdict: One of ``"supported"``, ``"unsupported"``, or ``"partial"``.
        anchor: ``None`` for ``"unsupported"``; for ``"supported"`` always
            present (or annotation is downgraded); for ``"partial"`` present
            when a valid anchor was found, else ``None``.
        confidence: Reviewer confidence in [0.0, 1.0].
        reviewer_notes: Human-readable notes; empty string if none.
    """

    annotation_id: str
    verdict: Literal["supported", "unsupported", "partial"]
    anchor: SourceAnchor | None
    confidence: float
    reviewer_notes: str


@dataclass
class ReviewedAnnotation:
    """An annotation paired with its review verdict.

    Attributes:
        annotation: The original annotation input.
        verdict: The review verdict for this annotation.
    """

    annotation: Annotation
    verdict: ReviewVerdict


class AnnotationBundle:
    """Collection of reviewed annotations with filtering helpers.

    Attributes:
        items: All reviewed annotations (supported, partial, and unsupported).
        term_definitions: Mapping of term text → definition string for inline
            glossary popover spans in the rendered HTML (ticket #009).
        margin_notes: Free-text margin annotations emitted as ``<aside>``
            elements alongside the main body (ticket #009).
        section_summaries: Per-section reading-aid summaries keyed by
            ``section_id``.  Each value must contain ``"skim"``, ``"read"``,
            and ``"deep"`` sub-keys; sections without all three levels are
            silently excluded from the summary toggle UI (ticket #009).
    """

    def __init__(
        self,
        items: list[ReviewedAnnotation],
        term_definitions: dict[str, str] | None = None,
        margin_notes: list[str] | None = None,
        section_summaries: dict[str, dict[str, str]] | None = None,
    ) -> None:
        self.items = items
        self.term_definitions: dict[str, str] = (
            dict(term_definitions) if term_definitions else {}
        )
        self.margin_notes: list[str] = list(margin_notes) if margin_notes else []
        self.section_summaries: dict[str, dict[str, str]] = (
            dict(section_summaries) if section_summaries else {}
        )

    def accepted(self, accept_partial: bool = False) -> list[ReviewedAnnotation]:
        """Return annotations that passed the review filter.

        This method is PURE — no side effects, no logging.

        Args:
            accept_partial: When ``True``, include ``"partial"`` verdicts in
                addition to ``"supported"`` ones.  ``"unsupported"`` annotations
                are NEVER returned regardless of this flag.

        Returns:
            A new list containing only the accepted ``ReviewedAnnotation``
            instances.  Calling this method multiple times is idempotent.
        """
        result: list[ReviewedAnnotation] = []
        for ra in self.items:
            v = ra.verdict.verdict
            if v == "supported":
                result.append(ra)
            elif v == "partial" and accept_partial:
                result.append(ra)
            # "unsupported" is never accepted
        return result
