"""
Market data is the one part of this system Claude/we don't control --
free-tier APIs rate-limit, time out, or lag. The brief explicitly asks
"how do you handle stale, delayed or conflicting data", so that's
treated as a first-class design problem, not an afterthought:

  - Primary: a real quote API (Stooq's free CSV endpoint -- no key
    needed, good enough for a demo; swap in Finnhub/Polygon with an
    API key for production).
  - Fallback: a seeded random-walk simulator, so the product still
    works end-to-end (and honestly labels itself as simulated) if the
    primary source is down, rate-limited, or the symbol isn't found.
  - A simple circuit breaker trips after repeated primary failures so
    we stop hammering a dead upstream and degrade immediately instead
    of timing out on every single poll.
"""
import asyncio
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
import httpx


@dataclass
class Quote:
    symbol: str
    price: float
    volume: float
    source: str          # "live" | "simulated"
    quality: str          # "fresh" | "stale" | "delayed"
    fetched_at: datetime


class CircuitBreaker:
    def __init__(self, fail_threshold: int = 3, cooldown_seconds: int = 60):
        self.fail_threshold = fail_threshold
        self.cooldown_seconds = cooldown_seconds
        self.failures = 0
        self.opened_at: float | None = None

    def is_open(self) -> bool:
        if self.opened_at is None:
            return False
        if time.time() - self.opened_at > self.cooldown_seconds:
            # half-open: allow one probe through
            self.opened_at = None
            self.failures = 0
            return False
        return True

    def record_success(self):
        self.failures = 0
        self.opened_at = None

    def record_failure(self):
        self.failures += 1
        if self.failures >= self.fail_threshold:
            self.opened_at = time.time()


class SimulatedFeed:
    """Deterministic-ish per-symbol random walk, seeded by symbol so
    repeated calls for the same symbol drift continuously rather than
    jumping randomly -- makes the demo look like a real feed."""

    _state: dict[str, dict] = {}

    @classmethod
    def quote(cls, symbol: str) -> Quote:
        st = cls._state.setdefault(
            symbol,
            {"price": 100 + (hash(symbol) % 400), "volume": 500_000 + (hash(symbol) % 2_000_000)},
        )
        drift = random.gauss(0, 1) * 0.004 * st["price"]
        st["price"] = max(0.5, st["price"] + drift)
        st["volume"] = max(1000, st["volume"] * random.uniform(0.7, 1.6))
        return Quote(
            symbol=symbol,
            price=round(st["price"], 2),
            volume=round(st["volume"]),
            source="simulated",
            quality="fresh",
            fetched_at=datetime.now(timezone.utc),
        )


class MarketDataClient:
    def __init__(self):
        self.breaker = CircuitBreaker()
        self._client = httpx.AsyncClient(timeout=4.0)

    async def get_quote(self, symbol: str) -> Quote:
        if self.breaker.is_open():
            return SimulatedFeed.quote(symbol)

        try:
            # Stooq free CSV quote endpoint, no API key required.
            url = f"https://stooq.com/q/l/?s={symbol.lower()}.us&f=sd2t2ohlcv&h&e=csv"
            resp = await self._client.get(url)
            resp.raise_for_status()
            lines = resp.text.strip().splitlines()
            if len(lines) < 2:
                raise ValueError("empty response")
            row = lines[1].split(",")
            # Symbol,Date,Time,Open,High,Low,Close,Volume
            close = float(row[6])
            volume = float(row[7]) if row[7] not in ("", "N/D") else 0.0
            if close <= 0:
                raise ValueError("bad quote")

            fetched_at = datetime.now(timezone.utc)
            age_seconds = 0  # Stooq doesn't give us a precise timestamp we trust
            quality = "delayed" if row[1] else "fresh"  # free quotes are ~15min delayed by nature

            self.breaker.record_success()
            return Quote(symbol, round(close, 2), volume, "live", "delayed", fetched_at)
        except Exception:
            self.breaker.record_failure()
            return SimulatedFeed.quote(symbol)

    async def close(self):
        await self._client.aclose()


market_data = MarketDataClient()
