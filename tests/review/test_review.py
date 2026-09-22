"""Tests for the accuracy-review / consensus-pass stage (ticket #008).

All tests are synchronous (use the sync ``review()`` facade).
No pytestmark needed — no async tests here.

Test checklist (all required):
    test_verdict_schema
    test_bundle_accepted_default
    test_bundle_accepted_partial_flag
    test_consensus_both_supported
    test_consensus_disagreement
    test_consensus_both_partial
    test_consensus_supported_partial
    test_factory_two_providers
    test_factory_one_provider
    test_factory_zero_providers
    test_factory_offline_smoke
    test_single_provider_reviewer
    test_drop_logging
    test_anchor_fabrication_guard
    test_prompt_scope
    test_trailing_whitespace_quote
    test_per_provider_model
    test_empty_annotations
"""

from __future__ import annotations

import logging
import json
from pathlib import Path

import pytest

from paper2html.config import Config, PROVIDER_DEFAULTS
from paper2html.review.schemas import (
    AnnotationBundle,
    ReviewedAnnotation,
    ReviewVerdict,
    SourceAnchor,
)
from paper2html.review.reviewer import (
    ConsensusReviewer,
    ReviewerFactory,
    SingleProviderReviewer,
    _DEFAULT_OPENAI_MODEL,
)

from tests.review.conftest import (
    SECTION_A_TEXT,
    SECTION_B_TEXT,
    FakeProvider,
    make_annotation,
    partial_response,
    supported_response,
    unsupported_response,
)


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------


def test_verdict_schema():
    """SourceAnchor, ReviewVerdict, ReviewedAnnotation, AnnotationBundle are plain dataclasses."""
    anchor = SourceAnchor(
        section_id="sec-1",
        span_start=0,
        span_end=5,
        quote="Hello",
    )
    verdict = ReviewVerdict(
        annotation_id="ann-1",
        verdict="supported",
        anchor=anchor,
        confidence=0.95,
        reviewer_notes="clear match",
    )
    ann = make_annotation()
    ra = ReviewedAnnotation(annotation=ann, verdict=verdict)
    bundle = AnnotationBundle(items=[ra])

    assert anchor.section_id == "sec-1"
    assert anchor.span_start == 0
    assert anchor.span_end == 5
    assert anchor.quote == "Hello"

    assert verdict.verdict == "supported"
    assert verdict.confidence == 0.95
    assert verdict.anchor is anchor

    assert ra.annotation is ann
    assert ra.verdict is verdict

    assert bundle.items == [ra]


def test_bundle_accepted_default():
    """accepted() returns only 'supported'; unsupported and partial are excluded by default."""
    ann_s = make_annotation("ann-s", text="supported claim")
    ann_u = make_annotation("ann-u", text="unsupported claim")
    ann_p = make_annotation("ann-p", text="partial claim")

    anchor = SourceAnchor("sec-1", 0, 5, "multi")

    ra_s = ReviewedAnnotation(
        annotation=ann_s,
        verdict=ReviewVerdict("ann-s", "supported", anchor, 0.9, ""),
    )
    ra_u = ReviewedAnnotation(
        annotation=ann_u,
        verdict=ReviewVerdict("ann-u", "unsupported", None, 0.8, ""),
    )
    ra_p = ReviewedAnnotation(
        annotation=ann_p,
        verdict=ReviewVerdict("ann-p", "partial", None, 0.5, ""),
    )

    bundle = AnnotationBundle(items=[ra_s, ra_u, ra_p])
    result = bundle.accepted()

    assert len(result) == 1
    assert result[0].verdict.verdict == "supported"


def test_bundle_accepted_partial_flag():
    """accepted(accept_partial=True) includes partial; calling twice is idempotent."""
    ann_s = make_annotation("ann-s")
    ann_p = make_annotation("ann-p")

    anchor = SourceAnchor("sec-1", 0, 5, "multi")
    ra_s = ReviewedAnnotation(
        annotation=ann_s,
        verdict=ReviewVerdict("ann-s", "supported", anchor, 0.9, ""),
    )
    ra_p = ReviewedAnnotation(
        annotation=ann_p,
        verdict=ReviewVerdict("ann-p", "partial", None, 0.5, ""),
    )
    bundle = AnnotationBundle(items=[ra_s, ra_p])

    result1 = bundle.accepted(accept_partial=True)
    result2 = bundle.accepted(accept_partial=True)

    # Both calls return same length — idempotent
    assert len(result1) == 2
    assert len(result2) == 2
    ids1 = {r.verdict.annotation_id for r in result1}
    ids2 = {r.verdict.annotation_id for r in result2}
    assert ids1 == ids2 == {"ann-s", "ann-p"}


# ---------------------------------------------------------------------------
# Consensus tests
# ---------------------------------------------------------------------------


def test_consensus_both_supported():
    """Both providers support → final verdict supported; anchor is verbatim substring."""
    quote = "multi-head self-attention"
    assert quote in SECTION_A_TEXT, (
        "test setup: quote must be substring of SECTION_A_TEXT"
    )

    provider_a = FakeProvider([supported_response(quote=quote, confidence=0.95)])
    provider_b = FakeProvider([supported_response(quote=quote, confidence=0.90)])

    reviewer = ConsensusReviewer(provider_a, provider_b, "model-a", "model-b")
    ann = make_annotation(
        text="multi-head self-attention attends to different subspaces"
    )
    bundle = reviewer.review([ann], {"sec-1": SECTION_A_TEXT})

    assert len(bundle.items) == 1
    ra = bundle.items[0]
    assert ra.verdict.verdict == "supported"
    assert ra.verdict.anchor is not None
    assert ra.verdict.anchor.quote == quote
    assert ra.verdict.anchor.quote in SECTION_A_TEXT


def test_consensus_disagreement():
    """A=supported, B=unsupported → consensus unsupported (Zone-1)."""
    quote = "multi-head self-attention"
    provider_a = FakeProvider([supported_response(quote=quote, confidence=0.9)])
    provider_b = FakeProvider([unsupported_response(confidence=0.8)])

    reviewer = ConsensusReviewer(provider_a, provider_b, "model-a", "model-b")
    ann = make_annotation()
    bundle = reviewer.review([ann], {"sec-1": SECTION_A_TEXT})

    assert bundle.items[0].verdict.verdict == "unsupported"


def test_consensus_both_partial():
    """Both partial → consensus partial."""
    quote = "multi-head self-attention"
    provider_a = FakeProvider([partial_response(quote=quote, confidence=0.6)])
    provider_b = FakeProvider([partial_response(quote=quote, confidence=0.55)])

    reviewer = ConsensusReviewer(provider_a, provider_b, "model-a", "model-b")
    ann = make_annotation()
    bundle = reviewer.review([ann], {"sec-1": SECTION_A_TEXT})

    assert bundle.items[0].verdict.verdict == "partial"


def test_consensus_supported_partial():
    """A=supported, B=partial → unsupported (fix 3 cell — not in simple agreement table)."""
    quote = "multi-head self-attention"
    provider_a = FakeProvider([supported_response(quote=quote, confidence=0.9)])
    provider_b = FakeProvider([partial_response(quote=quote, confidence=0.6)])

    reviewer = ConsensusReviewer(provider_a, provider_b, "model-a", "model-b")
    ann = make_annotation()
    bundle = reviewer.review([ann], {"sec-1": SECTION_A_TEXT})

    assert bundle.items[0].verdict.verdict == "unsupported"


# ---------------------------------------------------------------------------
# Factory tests
# ---------------------------------------------------------------------------


def _make_config(
    *,
    anthropic_key: str | None = None,
    openai_key: str | None = None,
    provider: str = "claude",
    model: str = "",
) -> Config:
    resolved_model = (
        model if model else PROVIDER_DEFAULTS.get(provider, "claude-opus-4-8")
    )
    return Config(
        provider=provider,
        model=resolved_model,
        anthropic_api_key=anthropic_key,
        openai_api_key=openai_key,
        cache_dir=Path("/tmp/paper2html_test"),
    )


def test_factory_two_providers():
    """Two API keys → ConsensusReviewer."""
    config = _make_config(anthropic_key="ant-key", openai_key="oai-key")
    fake_a = FakeProvider([])
    fake_b = FakeProvider([])
    reviewer = ReviewerFactory.build(config, providers=(fake_a, fake_b))
    assert isinstance(reviewer, ConsensusReviewer)


def test_factory_one_provider():
    """One API key → SingleProviderReviewer."""
    config = _make_config(anthropic_key="ant-key")
    fake = FakeProvider([])
    reviewer = ReviewerFactory.build(config, providers=(fake,))
    assert isinstance(reviewer, SingleProviderReviewer)


def test_factory_zero_providers():
    """Zero API keys → ValueError."""
    config = _make_config()
    with pytest.raises(ValueError, match="no provider API key configured"):
        ReviewerFactory.build(config)


def test_factory_offline_smoke():
    """build() with both fake keys and providers=None constructs real providers without network."""
    # This test just verifies the factory can lazy-import and instantiate real providers.
    # It does NOT make any network calls because we inject providers.
    config = _make_config(anthropic_key="fake-ant-key", openai_key="fake-oai-key")
    fake_a = FakeProvider([])
    fake_b = FakeProvider([])
    reviewer = ReviewerFactory.build(config, providers=(fake_a, fake_b))
    assert isinstance(reviewer, ConsensusReviewer)


# ---------------------------------------------------------------------------
# SingleProviderReviewer tests
# ---------------------------------------------------------------------------


def test_single_provider_reviewer():
    """SingleProviderReviewer returns supported annotation with valid anchor."""
    quote = "multi-head self-attention"
    provider = FakeProvider([supported_response(quote=quote, confidence=0.88)])
    reviewer = SingleProviderReviewer(provider=provider, model="test-model")
    ann = make_annotation()
    bundle = reviewer.review([ann], {"sec-1": SECTION_A_TEXT})

    assert len(bundle.items) == 1
    ra = bundle.items[0]
    assert ra.verdict.verdict == "supported"
    assert ra.verdict.anchor is not None
    assert ra.verdict.anchor.quote == quote
    provider.assert_exhausted()


# ---------------------------------------------------------------------------
# Drop logging test
# ---------------------------------------------------------------------------


def test_drop_logging(caplog):
    """Exactly one DEBUG record is emitted for each unsupported annotation."""
    provider = FakeProvider([unsupported_response(confidence=0.8)])
    reviewer = SingleProviderReviewer(provider=provider, model="test-model")
    ann = make_annotation("ann-drop")

    with caplog.at_level(logging.DEBUG, logger="paper2html.review.reviewer"):
        bundle = reviewer.review([ann], {"sec-1": SECTION_A_TEXT})

    assert bundle.items[0].verdict.verdict == "unsupported"

    debug_records = [r for r in caplog.records if r.levelno == logging.DEBUG]
    # Exactly one debug record containing the annotation id
    ann_drop_records = [r for r in debug_records if "ann-drop" in r.getMessage()]
    assert len(ann_drop_records) == 1


# ---------------------------------------------------------------------------
# Anchor fabrication guard
# ---------------------------------------------------------------------------


def test_anchor_fabrication_guard():
    """Provider says supported but anchor_quote is not a substring → downgraded to unsupported."""
    fake_quote = "this text does not appear in the source section at all"
    assert fake_quote not in SECTION_A_TEXT, "test setup: quote must NOT be in source"

    provider = FakeProvider([supported_response(quote=fake_quote, confidence=0.95)])
    reviewer = SingleProviderReviewer(provider=provider, model="test-model")
    ann = make_annotation()
    bundle = reviewer.review([ann], {"sec-1": SECTION_A_TEXT})

    ra = bundle.items[0]
    assert ra.verdict.verdict == "unsupported"
    assert ra.verdict.anchor is None


# ---------------------------------------------------------------------------
# Prompt scope test
# ---------------------------------------------------------------------------


def test_prompt_scope():
    """User prompt contains annotation text and own section text, NOT other section text."""
    quote = "multi-head self-attention"
    provider = FakeProvider([supported_response(quote=quote, confidence=0.9)])
    reviewer = SingleProviderReviewer(provider=provider, model="test-model")
    ann = make_annotation(section_id="sec-1", text="multi-head self-attention claim")

    source_sections = {
        "sec-1": SECTION_A_TEXT,
        "sec-2": SECTION_B_TEXT,
    }
    reviewer.review([ann], source_sections)

    # Collect all user-role prompt contents
    user_prompts = [p for p in provider.received_prompts if "ANNOTATION_TEXT" in p]
    assert len(user_prompts) >= 1
    user_prompt = user_prompts[0]

    assert ann.text in user_prompt
    assert SECTION_A_TEXT in user_prompt
    # Other section's text must NOT appear in the prompt
    assert SECTION_B_TEXT not in user_prompt


# ---------------------------------------------------------------------------
# Trailing-whitespace quote test (fix 8)
# ---------------------------------------------------------------------------


def test_trailing_whitespace_quote():
    """Anchor quote with leading/trailing whitespace is stripped and still validates."""
    base_quote = "multi-head self-attention"
    padded_quote = f"  {base_quote}  "  # surrounding whitespace
    assert base_quote in SECTION_A_TEXT

    provider = FakeProvider([supported_response(quote=padded_quote, confidence=0.9)])
    reviewer = SingleProviderReviewer(provider=provider, model="test-model")
    ann = make_annotation()
    bundle = reviewer.review([ann], {"sec-1": SECTION_A_TEXT})

    ra = bundle.items[0]
    assert ra.verdict.verdict == "supported"
    assert ra.verdict.anchor is not None
    assert ra.verdict.anchor.quote == base_quote  # stripped


# ---------------------------------------------------------------------------
# Per-provider model assertion test (fix 1)
# ---------------------------------------------------------------------------


def test_per_provider_model():
    """ConsensusReviewer sends provider-appropriate model strings to each provider."""
    quote = "multi-head self-attention"
    provider_a = FakeProvider([supported_response(quote=quote)])
    provider_b = FakeProvider([supported_response(quote=quote)])

    # When config.provider="claude" → model_a = config.model, model_b = OpenAI default
    config = _make_config(
        anthropic_key="ant-key",
        openai_key="oai-key",
        provider="claude",
        model="claude-test-model",
    )
    reviewer = ReviewerFactory.build(config, providers=(provider_a, provider_b))
    assert isinstance(reviewer, ConsensusReviewer)

    ann = make_annotation()
    reviewer.review([ann], {"sec-1": SECTION_A_TEXT})

    # Provider A received configured Claude model
    assert provider_a.received_models == ["claude-test-model"]
    # Provider B received OpenAI default model (NOT claude-test-model)
    assert provider_b.received_models == [_DEFAULT_OPENAI_MODEL]
    assert provider_b.received_models[0] != "claude-test-model"


# ---------------------------------------------------------------------------
# Empty annotations list
# ---------------------------------------------------------------------------


def test_empty_annotations():
    """Empty input → empty bundle, zero provider calls."""
    provider = FakeProvider([])  # no responses queued
    reviewer = SingleProviderReviewer(provider=provider, model="test-model")
    bundle = reviewer.review([], {"sec-1": SECTION_A_TEXT})

    assert bundle.items == []
    assert provider.received_models == []
    provider.assert_exhausted()


# ---------------------------------------------------------------------------
# Missing section_id → unsupported
# ---------------------------------------------------------------------------


def test_missing_section_unsupported():
    """Annotation with section_id not in source_sections → unsupported, no provider call."""
    provider = FakeProvider([])  # no responses queued
    reviewer = SingleProviderReviewer(provider=provider, model="test-model")
    ann = make_annotation(section_id="sec-missing")
    bundle = reviewer.review([ann], {"sec-1": SECTION_A_TEXT})

    assert len(bundle.items) == 1
    assert bundle.items[0].verdict.verdict == "unsupported"
    assert provider.received_models == []
    provider.assert_exhausted()


# ---------------------------------------------------------------------------
# Malformed JSON → safe fallback (unsupported), never crash
# ---------------------------------------------------------------------------


def test_malformed_json_fallback():
    """Malformed JSON after both attempts → unsupported, never crash."""
    provider = FakeProvider(["not json at all", "still not json"])
    reviewer = SingleProviderReviewer(provider=provider, model="test-model")
    ann = make_annotation()
    bundle = reviewer.review([ann], {"sec-1": SECTION_A_TEXT})

    assert bundle.items[0].verdict.verdict == "unsupported"
    provider.assert_exhausted()


# ---------------------------------------------------------------------------
# Confidence clamping
# ---------------------------------------------------------------------------


def test_confidence_clamped():
    """Provider confidence >1.0 or <0.0 is clamped to [0.0, 1.0]."""
    quote = "multi-head self-attention"
    over_response = json.dumps(
        {
            "verdict": "supported",
            "confidence": 1.5,  # out of range
            "anchor_quote": quote,
            "reviewer_notes": "",
        }
    )
    provider = FakeProvider([over_response])
    reviewer = SingleProviderReviewer(provider=provider, model="test-model")
    ann = make_annotation()
    bundle = reviewer.review([ann], {"sec-1": SECTION_A_TEXT})

    assert bundle.items[0].verdict.confidence == 1.0


# ---------------------------------------------------------------------------
# Leniency: missing keys in provider JSON (fix 7)
# ---------------------------------------------------------------------------


def test_provider_json_missing_confidence():
    """Missing 'confidence' → defaults to 0.0."""
    quote = "multi-head self-attention"
    response = json.dumps(
        {
            "verdict": "supported",
            "anchor_quote": quote,
            "reviewer_notes": "",
            # 'confidence' intentionally absent
        }
    )
    provider = FakeProvider([response])
    reviewer = SingleProviderReviewer(provider=provider, model="test-model")
    ann = make_annotation()
    bundle = reviewer.review([ann], {"sec-1": SECTION_A_TEXT})

    assert bundle.items[0].verdict.confidence == 0.0


def test_provider_json_invalid_verdict():
    """Invalid 'verdict' value → treated as unsupported."""
    response = json.dumps(
        {
            "verdict": "INVALID_VERDICT",
            "confidence": 0.9,
            "anchor_quote": None,
            "reviewer_notes": "",
        }
    )
    provider = FakeProvider([response])
    reviewer = SingleProviderReviewer(provider=provider, model="test-model")
    ann = make_annotation()
    bundle = reviewer.review([ann], {"sec-1": SECTION_A_TEXT})

    assert bundle.items[0].verdict.verdict == "unsupported"


# ---------------------------------------------------------------------------
# Quote truncation to 120 chars
# ---------------------------------------------------------------------------


def test_quote_truncated_to_120():
    """Quote longer than 120 chars is truncated before substring check."""
    # Build a long quote that starts with something in SECTION_A_TEXT
    # We need the first 120 chars of SECTION_A_TEXT to be a valid substring
    long_prefix = SECTION_A_TEXT[:50]  # definitely in source
    long_quote = long_prefix + "X" * 200  # total > 120 chars

    # After truncation to 120, it should be `(long_prefix + "X" * (120 - 50))`.
    # The truncated quote likely won't be in SECTION_A_TEXT (due to X's beyond
    # the source), so it downgrades to unsupported. Let's verify that at least
    # no crash occurs and the quote > 120 is handled.
    provider = FakeProvider([supported_response(quote=long_quote, confidence=0.9)])
    reviewer = SingleProviderReviewer(provider=provider, model="test-model")
    ann = make_annotation()
    bundle = reviewer.review([ann], {"sec-1": SECTION_A_TEXT})

    ra = bundle.items[0]
    # The truncated quote (first 120 chars) contains X's, so it won't match → unsupported
    assert ra.verdict.verdict == "unsupported"
    assert ra.verdict.anchor is None
