"""Glue layer: populate AnnotationBundle reading-aid fields from PipelineResult.

Ticket #013 — terminology glue.
Ticket #014 — section_summaries and margin_notes population.

Merge precedence (documented here, enforced in attach_reading_aids):
    1. Worker term_definitions are collected first.  Among workers, the last
       worker in ``result.section_analyses`` iteration order wins on key
       conflict (last-write-wins within the worker layer).
    2. ``result.terminology`` (synthesis output) is merged on top — synthesis
       wins on any key conflict with the worker layer.

section_summaries merge rules (#014):
    - Only sections with all three levels (skim, read, deep) non-empty and
      non-whitespace-only are included — never-partial.
    - Pipeline worker values replace any pre-existing bundle entry for the same
      section_id (pipeline wins per section_id).
    - Pre-existing entries for section_ids not in the pipeline result are
      preserved verbatim.

margin_notes merge rules (#014):
    - Bundle notes come first, worker notes appended in section_analyses order.
    - Non-string, empty-string, and whitespace-only notes are skipped.

This module lives on the review side because ``AnnotationBundle`` is defined
in ``review.schemas``.  The import direction is already established:
``review.reviewer`` imports ``pipeline`` types; ``pipeline`` never imports
``review`` — adding ``glue.py`` here preserves that invariant.
"""

from __future__ import annotations

from paper2html.pipeline.schemas import PipelineResult
from paper2html.review.schemas import AnnotationBundle


def attach_reading_aids(
    bundle: AnnotationBundle,
    result: PipelineResult,
) -> AnnotationBundle:
    """Return a new AnnotationBundle with reading-aid fields populated from *result*.

    The returned bundle is a NEW object; neither *bundle* nor *result* are
    mutated.  The function never raises — malformed or missing data degrades
    gracefully.

    Merge precedence for term_definitions:
        Worker terms are collected first (last-worker-wins on conflict), then
        ``result.terminology`` is merged on top so synthesis wins on conflict.

    Merge rules for section_summaries:
        Only complete entries (skim + read + deep all non-empty, non-whitespace)
        are written — never-partial.  Pipeline worker values replace any
        pre-existing bundle entry for the same section_id.  Pre-existing entries
        for sections absent from the pipeline result are preserved.

    Merge rules for margin_notes:
        Bundle notes come first (order preserved), then worker notes appended in
        ``result.section_analyses`` iteration order.  Non-string, empty-string,
        and whitespace-only notes are skipped.

    Args:
        bundle: Existing bundle whose ``items`` is preserved verbatim.  Its
            ``term_definitions``, ``section_summaries``, and ``margin_notes``
            serve as the base layer that pipeline values are merged on top of.
        result: PipelineResult produced by OrchestratorRunner.

    Returns:
        A new AnnotationBundle with all reading-aid fields populated (or empty
        when no data is available).
    """
    # --- term_definitions ---
    merged_terms: dict[str, str] = {}

    # Worker layer (last-write-wins among workers)
    for worker_output in result.section_analyses:
        for entry in worker_output.term_definitions:
            term = entry.get("term")
            definition = entry.get("definition")
            if term and definition:
                merged_terms[term] = definition

    # Synthesis layer wins on conflict
    merged_terms.update(result.terminology)

    # --- section_summaries ---
    merged_summaries: dict[str, dict[str, str]] = dict(bundle.section_summaries)

    for worker_output in result.section_analyses:
        skim = worker_output.summary_skim.strip()
        read = worker_output.summary_read.strip()
        deep = worker_output.summary_deep.strip()
        if skim and read and deep:
            merged_summaries[worker_output.section_id] = {
                "skim": skim,
                "read": read,
                "deep": deep,
            }

    # --- margin_notes ---
    # Build the full combined list first, then apply the guard on return.
    merged_notes: list[str] = list(bundle.margin_notes)

    for worker_output in result.section_analyses:
        for note in worker_output.margin_notes:
            if isinstance(note, str) and note.strip():
                merged_notes.append(note)

    return AnnotationBundle(
        items=list(bundle.items),
        term_definitions=merged_terms if merged_terms else None,
        margin_notes=merged_notes if merged_notes else None,
        section_summaries=merged_summaries if merged_summaries else None,
    )
