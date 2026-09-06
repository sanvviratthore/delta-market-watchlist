"""
The core idea of the product lives here: deciding what counts as a
"meaningful change" per the brief, instead of just showing raw % move.

A move is scored on three independent signals, any one of which can
make it meaningful on its own:

  1. Volatility-adjusted price move -- a move is compared against the
     symbol's OWN typical daily swing (avg_true_range_pct), not a flat
     threshold. A 3% move on a stock that normally swings 1% is a big
     deal; the same 3% on a stock that normally swings 5% is noise.
  2. Volume spike -- unusual participation, independent of price
     direction, often precedes a real move.
  3. Structural threshold crossings -- new 52-week high/low, or
     crossing the 50-day moving average -- because these are levels
     other market participants watch, not just this user.

Each symbol also gets a category label so the digest reads like a
person wrote it ("broke above 30-day high on 2.4x average volume")
rather than a bare percentage.

compare_to_market() adds a second, independent layer on top: whether a
move is actually about the stock, or just the whole market moving
together (see its docstring below).
"""
from dataclasses import dataclass


@dataclass
class Significance:
    is_meaningful: bool
    score: float
    kind: str | None
    detail: str | None


def evaluate(
    *,
    last_price: float | None,
    current_price: float,
    current_volume: float,
    avg_true_range_pct: float,
    avg_volume: float,
    week52_high: float | None,
    week52_low: float | None,
    ma50: float | None,
) -> Significance:
    events: list[tuple[float, str, str]] = []  # (score, kind, detail)

    if last_price and last_price > 0:
        pct_move = (current_price - last_price) / last_price
        # Normalize the raw move by this symbol's typical swing.
        # z >= 1.5 means "bigger than this stock's normal daily noise".
        z = abs(pct_move) / max(avg_true_range_pct, 0.001)
        if z >= 1.5:
            direction = "up" if pct_move > 0 else "down"
            events.append((
                min(z, 6.0),
                "volatility_move",
                f"Moved {pct_move*100:+.1f}% ({direction}), "
                f"{z:.1f}x this stock's typical daily swing",
            ))

    if avg_volume > 0:
        vol_ratio = current_volume / avg_volume
        if vol_ratio >= 2.0:
            events.append((
                min(vol_ratio, 6.0),
                "volume_spike",
                f"Trading at {vol_ratio:.1f}x average volume",
            ))

    if week52_high and current_price >= week52_high:
        events.append((5.0, "breakout_high", "Hit a new 52-week high"))
    if week52_low and current_price <= week52_low:
        events.append((5.0, "breakdown_low", "Hit a new 52-week low"))

    if ma50 and last_price:
        crossed_up = last_price < ma50 <= current_price
        crossed_down = last_price > ma50 >= current_price
        if crossed_up:
            events.append((3.0, "ma_cross", "Crossed above its 50-day moving average"))
        elif crossed_down:
            events.append((3.0, "ma_cross", "Crossed below its 50-day moving average"))

    if not events:
        return Significance(False, 0.0, None, None)

    events.sort(key=lambda e: e[0], reverse=True)
    top_score, top_kind, top_detail = events[0]
    return Significance(True, top_score, top_kind, top_detail)


def compare_to_market(stock_pct_change: float | None, market_pct_change: float | None) -> str | None:
    """
    Answers a question a raw % change can't: is this move about the
    STOCK, or is the whole market doing the same thing? A user should
    read "AAPL -2.1%" very differently if the S&P 500 is also down 2%
    that day (broad selloff, probably not AAPL-specific news) versus
    flat (something happened to AAPL specifically).

    Deliberately returns None rather than a weak/ambiguous claim when
    the move is too small to attribute confidently either way, or when
    we don't have a benchmark reading yet (e.g. right after startup).
    """
    if stock_pct_change is None or market_pct_change is None:
        return None
    if abs(stock_pct_change) < 0.5:
        return None  # too small to say anything meaningful about attribution

    diff = stock_pct_change - market_pct_change
    if abs(diff) < 1.0:
        return (
            f"Tracks the broader market (S&P 500 {market_pct_change:+.1f}%) — "
            f"likely a market-wide move, not news specific to this stock."
        )
    return (
        f"Diverges from the broader market (S&P 500 {market_pct_change:+.1f}%) — "
        f"likely driven by something specific to this stock."
    )