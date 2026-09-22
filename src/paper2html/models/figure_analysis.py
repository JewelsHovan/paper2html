"""FigureAnalysis dataclass — inter-ticket schema contract (tickets 007/008/009).

Do NOT add, rename, or reorder fields without updating all downstream consumers.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FigureAnalysis:
    """Structured vision analysis for a single figure.

    Attributes:
        figure_id: The figure's canonical id (e.g. ``"fig-3"``).
        alt_text: Accessibility alt-text, ≤ 150 characters, screen-reader quality.
        gloss: 1–3 sentence narrative capturing trend direction, comparison, or key
            takeaway.  Must NOT merely restate the caption.
        source_anchors: Sentence ids from the paper body that reference this figure.
            Empty list is valid; absent field is invalid.
        provider_model: Model identifier used to produce this analysis.
        confidence: Provider-reported confidence in [0, 1], or ``None`` if unavailable.
    """

    figure_id: str
    alt_text: str
    gloss: str
    source_anchors: list[str] = field(default_factory=list)
    provider_model: str = ""
    confidence: float | None = None
