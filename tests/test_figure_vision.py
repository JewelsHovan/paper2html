"""Unit tests for paper2html.figure_vision (ticket 007).

KG constraints honoured:
- asyncio_mode=strict: module-level pytestmark (not conftest) per learning #S41
- No-SDK check runs in subprocess per learning #S33
- Coverage uses module form per learning #S32
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from paper2html.figure_vision import (
    FigureAnalysisError,
    FigureVisionWorker,
    apply_analysis,
)
from paper2html.models import Figure, FigureAnalysis, NormalizedDocument, Section
from paper2html.models.document import DocumentMetadata
from paper2html.pipeline.budget import BudgetTracker
from paper2html.pipeline.errors import BudgetExceededError, PipelineError, ProviderError
from paper2html.pipeline.providers.base import (
    Message,
    ProviderInterface,
    ProviderResponse,
    ToolDef,
)

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------

_FIXTURES_DIR = Path(__file__).parent / "fixtures"
_JPEG_PATH = _FIXTURES_DIR / "figure_100x100.jpg"

_CANNED_ALT = "Line chart showing training loss decreasing faster for proposed method"
_CANNED_GLOSS = (
    "The proposed method converges ~2x faster than the baseline, reaching"
    " plateau loss by epoch 15 versus epoch 30."
)
_CANNED_CONFIDENCE = 0.92

_CANNED_RESPONSE_JSON = (
    f'{{"alt_text": "{_CANNED_ALT}", "gloss": "{_CANNED_GLOSS}",'
    f' "confidence": {_CANNED_CONFIDENCE}}}'
)

_MODEL = "claude-haiku-4-5"


# ---------------------------------------------------------------------------
# Stub provider (purpose-built; does NOT reuse MockProvider)
# ---------------------------------------------------------------------------


class StubProvider(ProviderInterface):
    """Minimal stub that returns a canned ``ProviderResponse``.

    Args:
        responses: Sequence of responses to return in order.  The last entry
            is repeated if the call count exceeds the list length.
        raise_on: If set, raises this exception on the call whose (1-indexed)
            position matches ``raise_after`` calls, then never raises again.
        raise_after: How many successful calls before raising (default 0 → raise
            on the very first call).
    """

    def __init__(
        self,
        responses: list[ProviderResponse] | None = None,
        raise_on: Exception | None = None,
        raise_after: int = 0,
    ) -> None:
        if responses is None:
            responses = [
                ProviderResponse(
                    content=_CANNED_RESPONSE_JSON,
                    input_tokens=50,
                    output_tokens=40,
                )
            ]
        self._responses = responses
        self._raise_on = raise_on
        self._raise_after = raise_after
        self.call_count: int = 0
        self._calls: list[tuple[list[Message], list[bytes], list[ToolDef], str]] = []

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
        self.call_count += 1
        self._calls.append((messages, images, tools, model))

        if self._raise_on is not None and self.call_count > self._raise_after:
            raise self._raise_on

        idx = min(self.call_count - 1, len(self._responses) - 1)
        return self._responses[idx]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def jpeg_bytes() -> bytes:
    """Load the committed 100×100 JPEG fixture."""
    return _JPEG_PATH.read_bytes()


@pytest.fixture()
def fig3(jpeg_bytes: bytes) -> Figure:
    """A Figure with id 'fig-3', caption 'Figure 3: ...', JPEG bytes."""
    return Figure(
        id="fig-3",
        caption="Figure 3: Training loss curves for baseline and proposed method",
        image_bytes=jpeg_bytes,
        image_mime_type="image/jpeg",
    )


@pytest.fixture()
def doc_with_fig3_refs() -> NormalizedDocument:
    """NormalizedDocument whose sections contain references to Figure 3 and Fig.3.

    Section sec-1 text_content is structured so that each figure reference
    occupies a DISTINCT '. '-delimited sentence slot.  Note: the sentence
    splitter uses ". " (period + space), so "Fig. 3" (period + space + digit)
    would be broken across two sentences.  We use "Fig.3" (no space) for the
    abbreviated-form sentence so the split is predictable:
      sent-1: "As shown in Figure 3, the proposed method outperforms"
      sent-2: "Figure 3 shows the full training dynamics"
      sent-3: "We refer again to Figure 3 for details"
      sent-4: "See also Fig.3 for comparison"   ← abbreviated, no space
      sent-5: "Figure 4 presents the ablation study"
      sent-6: "The results in Figure 4 confirm our hypothesis"
    """
    sec = Section(
        id="sec-1",
        title="Results",
        level=1,
        text_content=(
            "As shown in Figure 3, the proposed method outperforms. "
            "Figure 3 shows the full training dynamics. "
            "We refer again to Figure 3 for details. "
            "See also Fig.3 for comparison. "
            "Figure 4 presents the ablation study. "
            "The results in Figure 4 confirm our hypothesis"
        ),
    )
    return NormalizedDocument(
        metadata=DocumentMetadata(title="Test Paper"),
        sections=[sec],
    )


def _make_worker(
    provider: StubProvider | None = None,
    model: str = _MODEL,
) -> FigureVisionWorker:
    if provider is None:
        provider = StubProvider()
    return FigureVisionWorker(provider, model=model)


# ---------------------------------------------------------------------------
# 1. Happy path: valid JPEG → FigureAnalysis fields populated
# ---------------------------------------------------------------------------


async def test_happy_path_fields_populated(
    fig3: Figure,
    doc_with_fig3_refs: NormalizedDocument,
) -> None:
    """Happy path: all FigureAnalysis fields populated, provider called exactly once."""
    provider = StubProvider()
    worker = _make_worker(provider)
    result = await worker.analyze(fig3, doc_with_fig3_refs)

    assert isinstance(result, FigureAnalysis)
    assert result.figure_id == "fig-3"
    assert result.alt_text == _CANNED_ALT
    assert result.gloss == _CANNED_GLOSS
    assert result.provider_model == _MODEL
    assert len(result.alt_text) <= 150

    # Exactly one call
    assert provider.call_count == 1

    # images kwarg must be [jpeg_bytes]
    _, images, _, _ = provider._calls[0]
    assert images == [fig3.image_bytes]


# ---------------------------------------------------------------------------
# 2. Behavior-Contract scenario 1 verbatim
# ---------------------------------------------------------------------------


async def test_scenario_1_verbatim(
    fig3: Figure,
    doc_with_fig3_refs: NormalizedDocument,
) -> None:
    """Scenario 1 from Zone 2 Behavior Contract."""
    provider = StubProvider()
    worker = _make_worker(provider)
    result = await worker.analyze(fig3, doc_with_fig3_refs)

    assert result.alt_text == _CANNED_ALT
    assert result.gloss == _CANNED_GLOSS
    assert result.confidence == pytest.approx(_CANNED_CONFIDENCE)
    assert result.provider_model == _MODEL
    assert "sec-1-sent-" in (result.source_anchors[0] if result.source_anchors else "")


# ---------------------------------------------------------------------------
# 3. SVG bytes (<svg ...) → ValueError; provider NOT called
# ---------------------------------------------------------------------------


async def test_svg_bytes_rejected(
    doc_with_fig3_refs: NormalizedDocument,
) -> None:
    """SVG bytes raise ValueError before any provider call."""
    provider = StubProvider()
    worker = _make_worker(provider)
    svg_fig = Figure(
        id="fig-3",
        caption="Figure 3: something",
        image_bytes=b"<svg xmlns='http://www.w3.org/2000/svg'><rect/></svg>",
        image_mime_type="image/png",
    )
    with pytest.raises(ValueError) as exc_info:
        await worker.analyze(svg_fig, doc_with_fig3_refs)

    assert "fig-3" in str(exc_info.value)
    assert provider.call_count == 0


# ---------------------------------------------------------------------------
# 4. XML-prolog SVG → ValueError (MUST-FIX 3 test)
# ---------------------------------------------------------------------------


async def test_xml_prolog_svg_rejected(
    doc_with_fig3_refs: NormalizedDocument,
) -> None:
    """SVG with XML prolog raises ValueError before any provider call."""
    provider = StubProvider()
    worker = _make_worker(provider)
    xml_svg = b'<?xml version="1.0" encoding="UTF-8"?><svg xmlns="..."><rect/></svg>'
    svg_fig = Figure(
        id="fig-3",
        caption="Figure 3: something",
        image_bytes=xml_svg,
        image_mime_type="image/png",
    )
    with pytest.raises(ValueError) as exc_info:
        await worker.analyze(svg_fig, doc_with_fig3_refs)

    assert "fig-3" in str(exc_info.value)
    assert provider.call_count == 0


# ---------------------------------------------------------------------------
# 5. SVG mime type with JPEG-ish bytes → ValueError
# ---------------------------------------------------------------------------


async def test_svg_mime_type_rejected(
    jpeg_bytes: bytes,
    doc_with_fig3_refs: NormalizedDocument,
) -> None:
    """SVG mime type raises ValueError even with raster-like bytes."""
    provider = StubProvider()
    worker = _make_worker(provider)
    svg_mime_fig = Figure(
        id="fig-3",
        caption="Figure 3: something",
        image_bytes=jpeg_bytes,
        image_mime_type="image/svg+xml",
    )
    with pytest.raises(ValueError) as exc_info:
        await worker.analyze(svg_mime_fig, doc_with_fig3_refs)

    assert "fig-3" in str(exc_info.value)
    assert provider.call_count == 0


# ---------------------------------------------------------------------------
# 6. Empty image_bytes → ValueError mentioning empty + figure id (MUST-FIX 1)
# ---------------------------------------------------------------------------


async def test_empty_image_bytes_rejected(
    doc_with_fig3_refs: NormalizedDocument,
) -> None:
    """Empty image_bytes raises ValueError before any provider call."""
    provider = StubProvider()
    worker = _make_worker(provider)
    empty_fig = Figure(
        id="fig-3",
        caption="Figure 3: something",
        image_bytes=b"",
        image_mime_type="image/jpeg",
    )
    with pytest.raises(ValueError) as exc_info:
        await worker.analyze(empty_fig, doc_with_fig3_refs)

    msg = str(exc_info.value)
    assert "fig-3" in msg
    assert "empty" in msg.lower()
    assert provider.call_count == 0


# ---------------------------------------------------------------------------
# 7. ProviderError → FigureAnalysisError with figure id + original message
# ---------------------------------------------------------------------------


async def test_provider_error_wrapped(
    fig3: Figure,
    doc_with_fig3_refs: NormalizedDocument,
) -> None:
    """ProviderError is wrapped as FigureAnalysisError with figure id + cause message."""
    provider = StubProvider(raise_on=ProviderError("timeout", provider="mock"))
    worker = _make_worker(provider)

    with pytest.raises(FigureAnalysisError) as exc_info:
        await worker.analyze(fig3, doc_with_fig3_refs)

    msg = str(exc_info.value)
    assert "fig-3" in msg
    assert "timeout" in msg

    # FigureAnalysisError must be a subclass of PipelineError
    assert issubclass(FigureAnalysisError, PipelineError)


# ---------------------------------------------------------------------------
# 8. Anchor extraction scenario 4: exact four ids (3× Figure 3 + 1× Fig. 3)
# ---------------------------------------------------------------------------


async def test_anchor_extraction_scenario_4(
    fig3: Figure,
    doc_with_fig3_refs: NormalizedDocument,
) -> None:
    """Source anchors contain exactly the 4 sentences referencing Figure/Fig. 3."""
    provider = StubProvider()
    worker = _make_worker(provider)
    result = await worker.analyze(fig3, doc_with_fig3_refs)

    # sec-1 text splits on ". ":
    # sent-1: "As shown in Figure 3, the proposed method outperforms"
    # sent-2: "Figure 3 shows the full training dynamics"
    # sent-3: "We refer again to Figure 3 for details"
    # sent-4: "See also Fig.3 for comparison"  (abbreviated form, no space)
    # sent-5: "Figure 4 presents the ablation study"
    # sent-6: "The results in Figure 4 confirm our hypothesis"
    expected = {"sec-1-sent-1", "sec-1-sent-2", "sec-1-sent-3", "sec-1-sent-4"}
    assert set(result.source_anchors) == expected, (
        f"Expected {expected}, got {set(result.source_anchors)}"
    )

    # Figure 4 sentences must be excluded
    assert "sec-1-sent-5" not in result.source_anchors
    assert "sec-1-sent-6" not in result.source_anchors


# ---------------------------------------------------------------------------
# 9. Empty anchors: unreferenced figure → source_anchors == []
# ---------------------------------------------------------------------------


async def test_empty_anchors_for_unreferenced_figure(
    jpeg_bytes: bytes,
) -> None:
    """A figure not mentioned in the document body has source_anchors == []."""
    provider = StubProvider()
    worker = _make_worker(provider)
    fig = Figure(
        id="fig-99",
        caption="Figure 99: an unreferenced figure",
        image_bytes=jpeg_bytes,
        image_mime_type="image/jpeg",
    )
    doc = NormalizedDocument(
        metadata=DocumentMetadata(title="Test Paper"),
        sections=[
            Section(
                id="sec-1",
                title="Introduction",
                level=1,
                text_content="No figure references here at all",
            )
        ],
    )
    result = await worker.analyze(fig, doc)
    assert result.source_anchors == []


# ---------------------------------------------------------------------------
# 10. Number derivation total failure → source_anchors == list(referencing_sentence_ids)
# ---------------------------------------------------------------------------


async def test_anchor_fallback_to_referencing_sentence_ids(
    jpeg_bytes: bytes,
) -> None:
    """When figure number cannot be derived, anchors fall back to referencing_sentence_ids."""
    provider = StubProvider()
    worker = _make_worker(provider)
    # id "figure-x" has no numeric tail; caption has no "Fig"; label is non-numeric
    fig = Figure(
        id="figure-x",
        caption="An unusual plot",
        image_bytes=jpeg_bytes,
        image_mime_type="image/jpeg",
        label="abc",
        referencing_sentence_ids=["sec-1-sent-5", "sec-2-sent-3"],
    )
    doc = NormalizedDocument(
        metadata=DocumentMetadata(title="Test Paper"),
        sections=[],
    )
    result = await worker.analyze(fig, doc)
    assert result.source_anchors == ["sec-1-sent-5", "sec-2-sent-3"]


# ---------------------------------------------------------------------------
# 11. alt_text 200 chars from provider → truncated to exactly 150
# ---------------------------------------------------------------------------


async def test_alt_text_truncated_to_150(
    fig3: Figure,
    doc_with_fig3_refs: NormalizedDocument,
) -> None:
    """Provider alt_text > 150 chars is silently truncated to 150."""
    long_alt = "A" * 200
    long_response = (
        f'{{"alt_text": "{long_alt}", '
        f'"gloss": "Some valid gloss text.", '
        f'"confidence": 0.8}}'
    )
    provider = StubProvider(
        responses=[
            ProviderResponse(content=long_response, input_tokens=50, output_tokens=40)
        ]
    )
    worker = _make_worker(provider)
    result = await worker.analyze(fig3, doc_with_fig3_refs)
    assert len(result.alt_text) == 150
    assert result.alt_text == "A" * 150


# ---------------------------------------------------------------------------
# 12. JSON-parse retry: 1st response non-JSON, 2nd valid → success (2 calls)
# ---------------------------------------------------------------------------


async def test_json_parse_retry_succeeds(
    fig3: Figure,
    doc_with_fig3_refs: NormalizedDocument,
) -> None:
    """Non-JSON first response triggers retry; valid second → success, called twice."""
    responses = [
        ProviderResponse(content="not valid json", input_tokens=10, output_tokens=5),
        ProviderResponse(
            content=_CANNED_RESPONSE_JSON, input_tokens=50, output_tokens=40
        ),
    ]
    provider = StubProvider(responses=responses)
    worker = _make_worker(provider)
    result = await worker.analyze(fig3, doc_with_fig3_refs)

    assert result.alt_text == _CANNED_ALT
    assert provider.call_count == 2


# ---------------------------------------------------------------------------
# 13. Missing-gloss retry: 1st JSON without gloss, 2nd valid; both-fail → error
# ---------------------------------------------------------------------------


async def test_missing_gloss_retry_succeeds(
    fig3: Figure,
    doc_with_fig3_refs: NormalizedDocument,
) -> None:
    """JSON missing 'gloss' triggers retry; valid second response → success."""
    responses = [
        ProviderResponse(
            content='{"alt_text": "Something", "confidence": 0.5}',
            input_tokens=10,
            output_tokens=5,
        ),
        ProviderResponse(
            content=_CANNED_RESPONSE_JSON, input_tokens=50, output_tokens=40
        ),
    ]
    provider = StubProvider(responses=responses)
    worker = _make_worker(provider)
    result = await worker.analyze(fig3, doc_with_fig3_refs)
    assert result.gloss == _CANNED_GLOSS
    assert provider.call_count == 2


async def test_both_retries_fail_raises_figure_analysis_error(
    fig3: Figure,
    doc_with_fig3_refs: NormalizedDocument,
) -> None:
    """Two consecutive bad responses raise FigureAnalysisError."""
    responses = [
        ProviderResponse(content="bad json", input_tokens=10, output_tokens=5),
        ProviderResponse(content="also bad", input_tokens=10, output_tokens=5),
    ]
    provider = StubProvider(responses=responses)
    worker = _make_worker(provider)

    with pytest.raises(FigureAnalysisError) as exc_info:
        await worker.analyze(fig3, doc_with_fig3_refs)

    assert "fig-3" in str(exc_info.value)
    assert provider.call_count == 2


# ---------------------------------------------------------------------------
# 14. Confidence coercion
# ---------------------------------------------------------------------------


async def _analyze_with_confidence_value(
    fig: Figure,
    doc: NormalizedDocument,
    confidence_json_value: Any,
) -> FigureAnalysis:
    """Helper: run analyze() with a specific confidence value in the response."""
    if confidence_json_value is None:
        conf_str = "null"
    elif isinstance(confidence_json_value, str):
        conf_str = f'"{confidence_json_value}"'
    else:
        conf_str = str(confidence_json_value)

    resp_json = (
        f'{{"alt_text": "Some alt text.", '
        f'"gloss": "Some gloss text.", '
        f'"confidence": {conf_str}}}'
    )
    provider = StubProvider(
        responses=[
            ProviderResponse(content=resp_json, input_tokens=10, output_tokens=10)
        ]
    )
    worker = FigureVisionWorker(provider, model=_MODEL)
    return await worker.analyze(fig, doc)


async def test_confidence_string_float_coerced(
    fig3: Figure,
    doc_with_fig3_refs: NormalizedDocument,
) -> None:
    """String '0.5' is coerced to float 0.5."""
    result = await _analyze_with_confidence_value(fig3, doc_with_fig3_refs, "0.5")
    assert result.confidence == pytest.approx(0.5)


async def test_confidence_over_1_clamped(
    fig3: Figure,
    doc_with_fig3_refs: NormalizedDocument,
) -> None:
    """Float 1.7 is clamped to 1.0."""
    result = await _analyze_with_confidence_value(fig3, doc_with_fig3_refs, 1.7)
    assert result.confidence == pytest.approx(1.0)


async def test_confidence_non_coercible_returns_none(
    fig3: Figure,
    doc_with_fig3_refs: NormalizedDocument,
) -> None:
    """String 'high' cannot be coerced → confidence is None."""
    result = await _analyze_with_confidence_value(fig3, doc_with_fig3_refs, "high")
    assert result.confidence is None


# ---------------------------------------------------------------------------
# 15. apply_analysis returns new Figure; original unmutated
# ---------------------------------------------------------------------------


async def test_apply_analysis_returns_new_figure(jpeg_bytes: bytes) -> None:
    """apply_analysis creates a new Figure with updated fields; original unchanged."""
    original = Figure(
        id="fig-3",
        caption="Figure 3: something",
        image_bytes=jpeg_bytes,
        image_mime_type="image/jpeg",
        referencing_sentence_ids=["sec-1-sent-1"],
    )
    analysis = FigureAnalysis(
        figure_id="fig-3",
        alt_text="Updated alt text",
        gloss="Updated gloss.",
        source_anchors=["sec-2-sent-3", "sec-3-sent-1"],
        provider_model=_MODEL,
        confidence=0.9,
    )
    updated = apply_analysis(original, analysis)

    # Updated fields
    assert updated.alt_text == "Updated alt text"
    assert updated.gloss == "Updated gloss."
    assert updated.referencing_sentence_ids == ["sec-2-sent-3", "sec-3-sent-1"]

    # Original is NOT mutated
    assert original.alt_text is None
    assert original.gloss is None
    assert original.referencing_sentence_ids == ["sec-1-sent-1"]

    # It's a different object
    assert updated is not original


# ---------------------------------------------------------------------------
# 16. No-SDK subprocess check (KG constraint — MUST run in subprocess)
# ---------------------------------------------------------------------------


async def test_no_sdk_imports_in_figure_vision() -> None:
    """Importing paper2html.figure_vision must not load anthropic or openai."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import paper2html.figure_vision, sys; "
                "bad = [m for m in sys.modules if m == 'anthropic'"
                " or m.startswith('anthropic.')"
                " or m == 'openai'"
                " or m.startswith('openai.')]; "
                "assert not bad, f'SDK modules loaded: {bad}'"
            ),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"SDK isolation check failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )


# ---------------------------------------------------------------------------
# 17. Budget path: check_before_call → provider → record ordering
# ---------------------------------------------------------------------------


class _SpyBudget(BudgetTracker):
    """BudgetTracker subclass that records call ordering into a shared list."""

    def __init__(self, events: list[str]) -> None:
        super().__init__(max_cost_usd=None)
        self._events = events

    async def complete(self, provider, **kwargs):
        self._events.append("check")
        response = await super().complete(provider, **kwargs)
        self._events.append("record")
        return response


class _SpyStubProvider(StubProvider):
    """StubProvider that appends 'provider' to the events list before delegating."""

    def __init__(self, events: list[str], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._events = events

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
        self._events.append("provider")
        return await super().complete(messages, images, tools, model=model)


async def test_budget_check_and_record_ordering(
    fig3: Figure,
    doc_with_fig3_refs: NormalizedDocument,
) -> None:
    """Budget check fires before provider, record fires after: check→provider→record."""
    events: list[str] = []
    budget = _SpyBudget(events)
    provider = _SpyStubProvider(events)
    worker = FigureVisionWorker(provider, model=_MODEL, budget=budget)
    await worker.analyze(fig3, doc_with_fig3_refs)

    assert events == ["check", "provider", "record"]
    assert provider.call_count == 1


# ---------------------------------------------------------------------------
# 18. Budget path: retry produces two check/provider/record triples
# ---------------------------------------------------------------------------


async def test_budget_retry_path_produces_two_check_record_pairs(
    fig3: Figure,
    doc_with_fig3_refs: NormalizedDocument,
) -> None:
    """On JSON retry: check→provider→record twice (record fires after every response)."""
    events: list[str] = []
    budget = _SpyBudget(events)
    responses = [
        ProviderResponse(content="not valid json", input_tokens=10, output_tokens=5),
        ProviderResponse(
            content=_CANNED_RESPONSE_JSON, input_tokens=50, output_tokens=40
        ),
    ]
    provider = _SpyStubProvider(events, responses=responses)
    worker = FigureVisionWorker(provider, model=_MODEL, budget=budget)
    await worker.analyze(fig3, doc_with_fig3_refs)

    assert events == ["check", "provider", "record", "check", "provider", "record"]
    assert provider.call_count == 2


# ---------------------------------------------------------------------------
# 19. Budget path: BudgetExceededError propagates (not wrapped as FigureAnalysisError)
# ---------------------------------------------------------------------------


async def test_budget_exceeded_error_propagates(
    fig3: Figure,
    doc_with_fig3_refs: NormalizedDocument,
) -> None:
    """BudgetExceededError from check_before_call propagates out of analyze()."""
    budget = BudgetTracker(max_cost_usd=None)

    async def _raise_budget_exceeded(provider, **kwargs):
        raise BudgetExceededError(
            current_usd=1.0,
            projected_usd=0.5,
            limit_usd=1.0,
        )

    budget.complete = _raise_budget_exceeded  # type: ignore[method-assign]

    provider = StubProvider()
    worker = FigureVisionWorker(provider, model=_MODEL, budget=budget)

    with pytest.raises(BudgetExceededError):
        await worker.analyze(fig3, doc_with_fig3_refs)

    # Provider must NOT have been called
    assert provider.call_count == 0


# ---------------------------------------------------------------------------
# 20. Confidence null JSON value → FigureAnalysis.confidence is None
# ---------------------------------------------------------------------------


async def test_confidence_null_json_value_returns_none(
    fig3: Figure,
    doc_with_fig3_refs: NormalizedDocument,
) -> None:
    """Provider JSON with 'confidence': null maps to FigureAnalysis.confidence is None."""
    result = await _analyze_with_confidence_value(fig3, doc_with_fig3_refs, None)
    assert result.confidence is None


# ---------------------------------------------------------------------------
# 21. Anchor union: order-preserving dedup of regex hits + pre-existing ids
# ---------------------------------------------------------------------------


async def test_anchor_union_dedup_order_preserving(
    jpeg_bytes: bytes,
    doc_with_fig3_refs: NormalizedDocument,
) -> None:
    """Regex anchors first, novel pre-existing ids appended; overlapping id appears once."""
    # sec-1-sent-2 overlaps with regex matches; sec-1-sent-99 is novel
    fig = Figure(
        id="fig-3",
        caption="Figure 3: Training loss curves for baseline and proposed method",
        image_bytes=jpeg_bytes,
        image_mime_type="image/jpeg",
        referencing_sentence_ids=["sec-1-sent-2", "sec-1-sent-99"],
    )
    provider = StubProvider()
    worker = FigureVisionWorker(provider, model=_MODEL)
    result = await worker.analyze(fig, doc_with_fig3_refs)

    # Regex finds sent-1..sent-4 in order; sent-2 is already in seen so not
    # duplicated; sent-99 is novel and appended at the end.
    assert result.source_anchors == [
        "sec-1-sent-1",
        "sec-1-sent-2",
        "sec-1-sent-3",
        "sec-1-sent-4",
        "sec-1-sent-99",
    ]
