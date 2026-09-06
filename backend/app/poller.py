"""
Scaling decision (brief: "how does the system scale for larger
watchlists and more users"): if 10,000 users all watch AAPL, we do NOT
want 10,000 upstream API calls. The poller fetches each DISTINCT
symbol across the whole system exactly once per cycle, then fans the
result out to every subscribed websocket connection in-memory. Cost
scales with unique symbols tracked platform-wide, not with user count.

For a real multi-instance deployment this fan-out would move from an
in-process dict to Redis Pub/Sub so all backend replicas share one
feed -- noted here rather than built, since a single demo instance
doesn't need it (brief: "where to keep things simple vs add complexity").
"""
import asyncio
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from sqlalchemy import distinct

from .database import SessionLocal
from . import models
from .data_sources import market_data
from .change_engine import evaluate

POLL_INTERVAL_SECONDS = 8

# Always tracked in the background as a market benchmark, even if no
# user explicitly adds it to their own watchlist -- this is what lets
# the /changes endpoint tell a user whether a move is stock-specific
# or just the whole market moving together.
BENCHMARK_SYMBOL = "SPY"


class ConnectionManager:
    def __init__(self):
        # symbol -> set of websocket connections subscribed to it
        self.subscribers: dict[str, set] = {}

    async def subscribe(self, symbol: str, websocket):
        self.subscribers.setdefault(symbol, set()).add(websocket)

    def unsubscribe_all(self, websocket):
        for conns in self.subscribers.values():
            conns.discard(websocket)

    async def broadcast(self, symbol: str, payload: dict):
        dead = []
        for ws in self.subscribers.get(symbol, set()):
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.subscribers.get(symbol, set()).discard(ws)


manager = ConnectionManager()


def _update_symbol_stats(db: Session, symbol: str, price: float, volume: float) -> models.SymbolStats:
    stats = db.query(models.SymbolStats).filter(models.SymbolStats.symbol == symbol).first()
    if stats is None:
        stats = models.SymbolStats(
            symbol=symbol,
            avg_true_range_pct=0.02,
            avg_volume=max(volume, 1.0),
            week52_high=price,
            week52_low=price,
            ma50=price,
        )
        db.add(stats)
    else:
        # Exponential moving updates -- cheap, no need to store full history to approximate these.
        alpha = 0.05
        stats.avg_volume = (1 - alpha) * stats.avg_volume + alpha * volume
        stats.ma50 = (1 - alpha) * (stats.ma50 or price) + alpha * price
        stats.week52_high = max(stats.week52_high or price, price)
        stats.week52_low = min(stats.week52_low or price, price)
        stats.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(stats)
    return stats


async def poll_once():
    db = SessionLocal()
    try:
        symbols = {
            row[0] for row in
            db.query(distinct(models.WatchlistItem.symbol)).all()
        }
        symbols.add(BENCHMARK_SYMBOL)
        for symbol in symbols:
            quote = await market_data.get_quote(symbol)

            last_snapshot = (
                db.query(models.PriceSnapshot)
                .filter(models.PriceSnapshot.symbol == symbol)
                .order_by(models.PriceSnapshot.captured_at.desc())
                .first()
            )
            last_price = last_snapshot.price if last_snapshot else None

            stats = _update_symbol_stats(db, symbol, quote.price, quote.volume)

            sig = evaluate(
                last_price=last_price,
                current_price=quote.price,
                current_volume=quote.volume,
                avg_true_range_pct=stats.avg_true_range_pct,
                avg_volume=stats.avg_volume,
                week52_high=stats.week52_high,
                week52_low=stats.week52_low,
                ma50=stats.ma50,
            )

            snap = models.PriceSnapshot(
                symbol=symbol, price=quote.price, volume=quote.volume,
                source=quote.source, quality=quote.quality,
            )
            db.add(snap)

            if sig.is_meaningful:
                db.add(models.ChangeEvent(
                    symbol=symbol, kind=sig.kind, score=sig.score,
                    price=quote.price, detail=sig.detail,
                ))
            db.commit()

            await manager.broadcast(symbol, {
                "symbol": symbol,
                "price": quote.price,
                "volume": quote.volume,
                "source": quote.source,
                "quality": quote.quality,
                "is_meaningful": sig.is_meaningful,
                "detail": sig.detail,
                "captured_at": datetime.now(timezone.utc).isoformat(),
            })
    finally:
        db.close()


async def poller_loop():
    while True:
        try:
            await poll_once()
        except Exception as e:
            print(f"[poller] cycle failed, continuing: {e}")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)