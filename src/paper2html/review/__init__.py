"""Accuracy review / consensus pass for the paper2html pipeline.

This package verifies LLM-generated annotations against the paper's source text
and emits only verified annotations — each carrying a source anchor — for the
HTML generator (#009).

Public surface:
    schemas      — Annotation, SourceAnchor, ReviewVerdict, ReviewedAnnotation,
                   AnnotationBundle
    reviewer     — SingleProviderReviewer, ConsensusReviewer, ReviewerFactory
    prompts      — REVIEW_SYSTEM_PROMPT, build_review_user_prompt, _RETRY_PREFIX
"""

from paper2html.review.schemas import (
    Annotation,
    AnnotationBundle,
    ReviewedAnnotation,
    ReviewVerdict,
    SourceAnchor,
)
from paper2html.review.reviewer import (
    ConsensusReviewer,
    ReviewerFactory,
    SingleProviderReviewer,
)

__all__ = [
    "Annotation",
    "AnnotationBundle",
    "ReviewedAnnotation",
    "ReviewVerdict",
    "SourceAnchor",
    "SingleProviderReviewer",
    "ConsensusReviewer",
    "ReviewerFactory",
]
