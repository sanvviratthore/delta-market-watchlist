"""
Tests for the one thing this product actually claims to do well:
deciding what counts as a "meaningful change". Each test encodes a
specific claim from the README/pitch, so a judge (or future me) can
verify the behaviour rather than take it on faith.

Run with:  pytest tests/ -v
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.change_engine import evaluate, compare_to_market


def test_small_move_on_volatile_stock_is_not_meaningful():
    """A 1% move on a stock that normally swings 3% a day is noise."""
    result = evaluate(
        last_price=100, current_price=101, current_volume=1_000_000,
        avg_true_range_pct=0.03, avg_volume=1_000_000,
        week52_high=150, week52_low=80, ma50=99,
    )
    assert result.is_meaningful is False


def test_same_pct_move_on_stable_stock_is_meaningful():
    """
    This is the core thesis of the product: the SAME 1% move is
    meaningful on a stock that normally swings only 0.5% a day.
    A flat percentage threshold would treat these identically —
    the volatility-adjusted score must not.
    """
    result = evaluate(
        last_price=100, current_price=101, current_volume=1_000_000,
        avg_true_range_pct=0.005, avg_volume=1_000_000,
        week52_high=150, week52_low=80, ma50=99,
    )
    assert result.is_meaningful is True
    assert result.kind == "volatility_move"


def test_volume_spike_alone_is_meaningful_even_with_flat_price():
    """Unusual participation should flag on its own, independent of price direction."""
    result = evaluate(
        last_price=100, current_price=100.2, current_volume=5_000_000,
        avg_true_range_pct=0.02, avg_volume=1_000_000,
        week52_high=150, week52_low=80, ma50=99,
    )
    assert result.is_meaningful is True
    assert result.kind == "volume_spike"


def test_new_52_week_high_is_always_meaningful():
    """Structural levels the whole market watches should flag regardless of daily volatility norms."""
    result = evaluate(
        last_price=145, current_price=151, current_volume=1_000_000,
        avg_true_range_pct=0.05, avg_volume=1_000_000,  # even a very volatile stock
        week52_high=150, week52_low=80, ma50=99,
    )
    assert result.is_meaningful is True
    assert result.kind == "breakout_high"


def test_new_52_week_low_is_always_meaningful():
    result = evaluate(
        last_price=85, current_price=79, current_volume=1_000_000,
        avg_true_range_pct=0.05, avg_volume=1_000_000,
        week52_high=150, week52_low=80, ma50=99,
    )
    assert result.is_meaningful is True
    assert result.kind == "breakdown_low"


def test_ma50_cross_upward_is_meaningful():
    result = evaluate(
        last_price=98, current_price=101, current_volume=1_000_000,
        avg_true_range_pct=0.03, avg_volume=1_000_000,
        week52_high=None, week52_low=None, ma50=100,
    )
    assert result.is_meaningful is True
    assert result.kind == "ma_cross"


def test_no_last_price_never_crashes_and_can_still_flag_structural_events():
    """First observation for a symbol has no prior price to diff against —
    must not crash, and structural checks (52w high/low) should still work."""
    result = evaluate(
        last_price=None, current_price=155, current_volume=1_000_000,
        avg_true_range_pct=0.02, avg_volume=1_000_000,
        week52_high=150, week52_low=80, ma50=99,
    )
    assert result.is_meaningful is True
    assert result.kind == "breakout_high"


def test_quiet_stock_with_no_thresholds_set_is_not_meaningful():
    """No prior price, no known 52w range, no moving average yet —
    should not spuriously flag anything."""
    result = evaluate(
        last_price=None, current_price=100, current_volume=1_000_000,
        avg_true_range_pct=0.02, avg_volume=1_000_000,
        week52_high=None, week52_low=None, ma50=None,
    )
    assert result.is_meaningful is False


def test_when_multiple_signals_fire_the_strongest_is_reported():
    """Volume spike (ratio 5x -> score 5.0) should win over a modest
    volatility move (score ~2.0) for the headline reason shown to the user."""
    result = evaluate(
        last_price=100, current_price=101, current_volume=5_000_000,
        avg_true_range_pct=0.005, avg_volume=1_000_000,
        week52_high=150, week52_low=80, ma50=99,
    )
    assert result.is_meaningful is True
    assert result.kind == "volume_spike"


def test_market_wide_move_is_flagged_as_tracking_the_market():
    """A stock down 2% while the S&P 500 is also down ~2% is a market
    story, not a stock story -- the exact case this feature exists for."""
    context = compare_to_market(stock_pct_change=-2.1, market_pct_change=-1.9)
    assert context is not None
    assert "Tracks the broader market" in context


def test_stock_specific_move_is_flagged_as_diverging():
    """A stock down 4% while the market is flat is stock-specific news."""
    context = compare_to_market(stock_pct_change=-4.0, market_pct_change=0.1)
    assert context is not None
    assert "Diverges from the broader market" in context


def test_tiny_move_gets_no_market_context_claim():
    """A 0.2% move is too small to confidently attribute either way --
    should not force a claim."""
    context = compare_to_market(stock_pct_change=0.2, market_pct_change=0.1)
    assert context is None


def test_missing_benchmark_data_gets_no_market_context_claim():
    """No SPY reading yet (e.g. right after startup) -- must not crash
    or fabricate a comparison."""
    context = compare_to_market(stock_pct_change=3.0, market_pct_change=None)
    assert context is None