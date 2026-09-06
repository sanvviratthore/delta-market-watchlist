from sqlalchemy import (
    Column, Integer, String, Float, DateTime, ForeignKey, Boolean, UniqueConstraint
)
from sqlalchemy.orm import relationship
from datetime import datetime, timezone
from .database import Base


def now_utc():
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    email = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    created_at = Column(DateTime, default=now_utc)

    watchlist_items = relationship("WatchlistItem", back_populates="user", cascade="all, delete-orphan")


class WatchlistItem(Base):
    """
    A symbol a user is tracking. last_seen_price/at are the anchor
    for 'what changed since you last checked' -- stored server-side
    (not localStorage) so it's identical across devices/sessions,
    which is what the brief asks for under state persistence.
    """
    __tablename__ = "watchlist_items"
    __table_args__ = (UniqueConstraint("user_id", "symbol", name="uq_user_symbol"),)

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    symbol = Column(String, nullable=False, index=True)
    added_at = Column(DateTime, default=now_utc)

    last_seen_price = Column(Float, nullable=True)
    last_seen_at = Column(DateTime, nullable=True)

    user = relationship("User", back_populates="watchlist_items")


class SymbolStats(Base):
    """
    Rolling baseline per symbol, used to decide what 'meaningful' means
    for THAT symbol -- a volatile penny stock and a stable blue chip
    have very different definitions of a normal move. Recomputed
    periodically from PriceSnapshot history.
    """
    __tablename__ = "symbol_stats"
    symbol = Column(String, primary_key=True)
    avg_true_range_pct = Column(Float, default=0.02)   # typical daily swing, as a fraction of price
    avg_volume = Column(Float, default=1_000_000.0)
    week52_high = Column(Float, nullable=True)
    week52_low = Column(Float, nullable=True)
    ma50 = Column(Float, nullable=True)
    updated_at = Column(DateTime, default=now_utc)


class PriceSnapshot(Base):
    """
    Append-only tick log. This is what makes 'what changed since I last
    checked' possible for an arbitrary gap (5 minutes or 5 days) --
    we don't just diff the last two ticks, we diff against whatever
    snapshot was current at last_seen_at.
    """
    __tablename__ = "price_snapshots"
    id = Column(Integer, primary_key=True)
    symbol = Column(String, index=True, nullable=False)
    price = Column(Float, nullable=False)
    volume = Column(Float, nullable=False)
    source = Column(String, nullable=False)          # "live" or "simulated"
    quality = Column(String, nullable=False)          # "fresh" | "stale" | "delayed"
    captured_at = Column(DateTime, default=now_utc, index=True)


class ChangeEvent(Base):
    """
    A materialized 'this was meaningful' record, so the digest a user
    sees on return is precomputed rather than recalculated from raw
    ticks every request.
    """
    __tablename__ = "change_events"
    id = Column(Integer, primary_key=True)
    symbol = Column(String, index=True, nullable=False)
    kind = Column(String, nullable=False)     # breakout_high | breakdown_low | volume_spike | ma_cross | volatility_move
    score = Column(Float, nullable=False)     # magnitude of significance, for ranking
    price = Column(Float, nullable=False)
    detail = Column(String, nullable=False)   # human-readable reason
    created_at = Column(DateTime, default=now_utc, index=True)
