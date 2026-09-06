from pydantic import BaseModel, EmailStr
from datetime import datetime
from typing import Optional


class UserCreate(BaseModel):
    email: EmailStr
    password: str


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class WatchlistAdd(BaseModel):
    symbol: str


class WatchlistOut(BaseModel):
    symbol: str
    added_at: datetime
    last_seen_price: Optional[float] = None
    last_seen_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class ChangeSince(BaseModel):
    symbol: str
    current_price: float
    last_seen_price: Optional[float]
    last_seen_at: Optional[datetime]
    pct_change_since_last_seen: Optional[float]
    is_meaningful: bool
    events: list[dict]
    data_quality: str
    market_context: Optional[str] = None