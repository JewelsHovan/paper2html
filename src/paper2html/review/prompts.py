"""Prompt constants and builders for the accuracy-review stage.

Design notes
------------
* ``REVIEW_SYSTEM_PROMPT`` instructs the provider to return ONLY a JSON object
  with a fixed schema and to base its verdict exclusively on the provided source
  section text.
* ``build_review_user_prompt`` includes only the annotation's OWN section text
  — never content from other sections (per Zone-1 constraint).
* ``_RETRY_PREFIX`` mirrors the retry pattern from worker.py.
"""

from __future__ import annotations

REVIEW_SYSTEM_PROMPT: str = (
    "You are an academic fact-checking assistant.\n"
    "You receive an annotation (a claim extracted from a research paper) and the "
    "verbatim source text of the paper section the annotation comes from.\n"
    "Your task: determine whether the annotation's claim is textually supported by "
    "the provided source section text.\n"
    "\n"
    "You MUST respond with a single valid JSON object and NOTHING else. "
    "Do not include any text, markdown, or code fences outside the JSON.\n"
    "\n"
    "The JSON object must have exactly these keys:\n"
    '  "verdict": one of "supported", "unsupported", or "partial"\n'
    '  "confidence": a float in [0.0, 1.0] expressing your certainty\n'
    '  "anchor_quote": a verbatim substring (≤120 chars) from the source text that '
    "supports the claim, or null if no supporting span exists\n"
    '  "reviewer_notes": a short explanation string, or "" if none\n'
    "\n"
    "Rules:\n"
    "- Use ONLY the provided source section text as evidence.\n"
    '- If the claim is not supported by the source text, set verdict to "unsupported" '
    "and anchor_quote to null.\n"
    "- anchor_quote MUST be an exact verbatim substring of the source text — "
    "never paraphrase or fabricate.\n"
    "- Do not include any text outside the JSON object."
)

_RETRY_PREFIX: str = (
    "IMPORTANT: return valid JSON matching the schema exactly. "
    "Your previous response was not valid JSON.\n"
)


def build_review_user_prompt(
    annotation_id: str, annotation_kind: str, annotation_text: str, section_text: str
) -> str:
    """Build the user message for a single annotation review.

    Includes ONLY the annotation's own section text — never content from other
    sections.

    Args:
        annotation_id: Unique identifier for the annotation.
        annotation_kind: Annotation type tag (carry-through, for context).
        annotation_text: The claim text to verify.
        section_text: Verbatim plain-text of the annotation's own section.

    Returns:
        Formatted user prompt string.
    """
    return (
        f"ANNOTATION_ID: {annotation_id}\n"
        f"ANNOTATION_KIND: {annotation_kind}\n"
        f"ANNOTATION_TEXT: {annotation_text}\n"
        "\n"
        "SOURCE_SECTION_TEXT:\n"
        f"{section_text}"
    )
