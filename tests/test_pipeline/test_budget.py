"""Budget tracker unit tests.

AC-6, Scenario 2: accumulation, pre-call check, error message, unknown model.
"""

from __future__ import annotations

import pytest

from paper2html.pipeline.budget import COST_TABLE, BudgetTracker
from paper2html.pipeline.errors import BudgetExceededError


# ---------------------------------------------------------------------------
# Accumulation
# ---------------------------------------------------------------------------


def test_budget_accumulates_costs_correctly() -> None:
    """BudgetTracker accumulates costs from the cost table."""
    tracker = BudgetTracker()
    model = "claude-haiku-4-5"
    in_rate, out_rate = COST_TABLE[model]

    tracker.record(model, input_tokens=1_000_000, output_tokens=1_000_000)
    expected = (in_rate + out_rate) / 1  # exactly $in_rate + $out_rate for 1M each
    assert abs(tracker.total_cost_usd - expected) < 1e-9


def test_budget_accumulates_multiple_calls() -> None:
    """Costs accumulate correctly across multiple calls."""
    tracker = BudgetTracker()
    tracker.record("claude-haiku-4-5", 100_000, 10_000)
    tracker.record("claude-opus-4-8", 50_000, 5_000)
    assert tracker.total_cost_usd > 0
    assert len(tracker._records) == 2


def test_budget_total_tokens() -> None:
    """total_input_tokens and total_output_tokens sum correctly."""
    tracker = BudgetTracker()
    tracker.record("claude-haiku-4-5", 100, 50)
    tracker.record("claude-haiku-4-5", 200, 100)
    assert tracker.total_input_tokens == 300
    assert tracker.total_output_tokens == 150


# ---------------------------------------------------------------------------
# Pre-call check
# ---------------------------------------------------------------------------


def test_budget_check_raises_before_cap_breaching_call() -> None:
    """check_before_call raises BudgetExceededError before cap breach."""
    tracker = BudgetTracker(max_cost_usd=0.000_001)
    with pytest.raises(BudgetExceededError) as exc_info:
        # 1000 chars → ~250 tokens → will exceed $0.000001 cap for any model
        tracker.check_before_call("claude-haiku-4-5", estimated_input_chars=1_000_000)

    err = exc_info.value
    assert err.limit_usd == 0.000_001
    assert err.current_usd >= 0.0
    assert err.projected_usd > 0.0


def test_budget_check_includes_current_and_overage_in_message() -> None:
    """BudgetExceededError message contains current cost and projected overage."""
    tracker = BudgetTracker(max_cost_usd=0.001)
    # Record some usage first
    tracker.record("claude-haiku-4-5", 100_000, 10_000)

    with pytest.raises(BudgetExceededError) as exc_info:
        tracker.check_before_call("claude-haiku-4-5", estimated_input_chars=10_000_000)

    msg = str(exc_info.value)
    assert "current=" in msg
    assert "projected" in msg
    assert "limit=" in msg


def test_budget_check_no_raise_when_no_cap() -> None:
    """check_before_call does not raise when max_cost_usd is None."""
    tracker = BudgetTracker(max_cost_usd=None)
    # Should not raise regardless of chars
    tracker.check_before_call("claude-opus-4-8", estimated_input_chars=100_000_000)


def test_budget_check_no_raise_when_under_cap() -> None:
    """check_before_call does not raise when projected cost is under cap."""
    tracker = BudgetTracker(max_cost_usd=100.0)
    tracker.check_before_call("claude-haiku-4-5", estimated_input_chars=100)


# ---------------------------------------------------------------------------
# Unknown model
# ---------------------------------------------------------------------------


def test_budget_unknown_model_returns_zero() -> None:
    """cost_for_tokens returns 0.0 for unknown models without raising."""
    tracker = BudgetTracker()
    cost = tracker.cost_for_tokens("unknown-model-xyz", 1_000_000, 1_000_000)
    assert cost == 0.0


def test_budget_record_unknown_model_does_not_raise() -> None:
    """record() does not raise for unknown models."""
    tracker = BudgetTracker()
    tracker.record("unknown-model", 100, 50)
    assert tracker.total_cost_usd == 0.0


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def test_budget_summary_shape() -> None:
    """summary() returns expected keys."""
    tracker = BudgetTracker()
    tracker.record("claude-haiku-4-5", 100, 50)
    s = tracker.summary()
    assert "total_input_tokens" in s
    assert "total_output_tokens" in s
    assert "total_cost_usd" in s
    assert "call_count" in s
    assert s["call_count"] == 1
