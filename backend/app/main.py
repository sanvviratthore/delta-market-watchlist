import asyncio
from datetime import datetime, timezone

from fastapi import FastAPI, Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from . import models, schemas, auth
from .database import engine, get_db
from .poller import poller_loop, manager, BENCHMARK_SYMBOL
from .change_engine import evaluate, compare_to_market

models.Base.metadata.create_all(bind=engine)

app = FastAPI(title="Delta — Smart Market Watchlist")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup():
    asyncio.create_task(poller_loop())


# ---------- Auth ----------

DEFAULT_DEMO_SYMBOLS = ["AAPL", "TSLA", "MSFT", "NVDA"]


@app.post("/auth/register", response_model=schemas.Token)
def register(payload: schemas.UserCreate, db: Session = Depends(get_db)):
    if db.query(models.User).filter(models.User.email == payload.email).first():
        raise HTTPException(400, "Email already registered")
    user = models.User(email=payload.email, hashed_password=auth.hash_password(payload.password))
    db.add(user)
    db.commit()
    db.refresh(user)

    # New accounts start with a small starter watchlist rather than an
    # empty screen -- there's nothing to configure, so there's no
    # reason to make someone type tickers before seeing the product work.
    for symbol in DEFAULT_DEMO_SYMBOLS:
        db.add(models.WatchlistItem(user_id=user.id, symbol=symbol))
    db.commit()

    token = auth.create_access_token({"sub": str(user.id)})
    return schemas.Token(access_token=token)


@app.post("/auth/login", response_model=schemas.Token)
def login(form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.email == form.username).first()
    if not user or not auth.verify_password(form.password, user.hashed_password):
        raise HTTPException(401, "Incorrect email or password")
    token = auth.create_access_token({"sub": str(user.id)})
    return schemas.Token(access_token=token)


# ---------- Watchlist CRUD ----------

@app.get("/watchlist", response_model=list[schemas.WatchlistOut])
def get_watchlist(db: Session = Depends(get_db), user: models.User = Depends(auth.get_current_user)):
    return db.query(models.WatchlistItem).filter(models.WatchlistItem.user_id == user.id).all()


@app.post("/watchlist", response_model=schemas.WatchlistOut)
def add_to_watchlist(
    payload: schemas.WatchlistAdd,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.get_current_user),
):
    symbol = payload.symbol.upper().strip()
    existing = (
        db.query(models.WatchlistItem)
        .filter(models.WatchlistItem.user_id == user.id, models.WatchlistItem.symbol == symbol)
        .first()
    )
    if existing:
        return existing
    item = models.WatchlistItem(user_id=user.id, symbol=symbol)
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


@app.delete("/watchlist/{symbol}")
def remove_from_watchlist(
    symbol: str, db: Session = Depends(get_db), user: models.User = Depends(auth.get_current_user)
):
    item = (
        db.query(models.WatchlistItem)
        .filter(models.WatchlistItem.user_id == user.id, models.WatchlistItem.symbol == symbol.upper())
        .first()
    )
    if not item:
        raise HTTPException(404, "Not in watchlist")
    db.delete(item)
    db.commit()
    return {"ok": True}


# ---------- The core feature: what changed since I last looked ----------

@app.get("/watchlist/{symbol}/changes", response_model=schemas.ChangeSince)
def changes_since_last_seen(
    symbol: str, db: Session = Depends(get_db), user: models.User = Depends(auth.get_current_user)
):
    symbol = symbol.upper()
    item = (
        db.query(models.WatchlistItem)
        .filter(models.WatchlistItem.user_id == user.id, models.WatchlistItem.symbol == symbol)
        .first()
    )
    if not item:
        raise HTTPException(404, "Not in watchlist")

    latest_snapshot = (
        db.query(models.PriceSnapshot)
        .filter(models.PriceSnapshot.symbol == symbol)
        .order_by(models.PriceSnapshot.captured_at.desc())
        .first()
    )
    if not latest_snapshot:
        raise HTTPException(503, "No market data yet for this symbol — try again in a few seconds")

    # Every ChangeEvent recorded since this user last looked (server-side
    # anchor, so this is correct whether it's been 5 minutes or 5 days,
    # and identical whichever device they check from).
    since = item.last_seen_at or item.added_at
    events = (
        db.query(models.ChangeEvent)
        .filter(models.ChangeEvent.symbol == symbol, models.ChangeEvent.created_at >= since)
        .order_by(models.ChangeEvent.created_at.asc())
        .all()
    )

    pct_change = None
    if item.last_seen_price:
        pct_change = (latest_snapshot.price - item.last_seen_price) / item.last_seen_price * 100

    # Market context: was this move about the stock, or the whole
    # market? Compare the stock's % change since last_seen against the
    # S&P 500's % change over the exact same window.
    market_context = None
    spy_latest = (
        db.query(models.PriceSnapshot)
        .filter(models.PriceSnapshot.symbol == BENCHMARK_SYMBOL)
        .order_by(models.PriceSnapshot.captured_at.desc())
        .first()
    )
    spy_at_anchor = (
        db.query(models.PriceSnapshot)
        .filter(models.PriceSnapshot.symbol == BENCHMARK_SYMBOL, models.PriceSnapshot.captured_at <= since)
        .order_by(models.PriceSnapshot.captured_at.desc())
        .first()
    ) or (
        db.query(models.PriceSnapshot)
        .filter(models.PriceSnapshot.symbol == BENCHMARK_SYMBOL)
        .order_by(models.PriceSnapshot.captured_at.asc())
        .first()
    )
    if spy_latest and spy_at_anchor and spy_at_anchor.price > 0 and pct_change is not None:
        spy_pct = (spy_latest.price - spy_at_anchor.price) / spy_at_anchor.price * 100
        market_context = compare_to_market(pct_change, spy_pct)

    result = schemas.ChangeSince(
        symbol=symbol,
        current_price=latest_snapshot.price,
        last_seen_price=item.last_seen_price,
        last_seen_at=item.last_seen_at,
        pct_change_since_last_seen=pct_change,
        is_meaningful=len(events) > 0,
        events=[
            {"kind": e.kind, "detail": e.detail, "score": e.score, "at": e.created_at.isoformat()}
            for e in events
        ],
        data_quality=latest_snapshot.quality,
        market_context=market_context,
    )

    # Mark as seen NOW that we've computed the diff -- next visit starts fresh from here.
    item.last_seen_price = latest_snapshot.price
    item.last_seen_at = datetime.now(timezone.utc)
    db.commit()

    return result


# ---------- Live push ----------

@app.websocket("/ws/{symbol}")
async def ws_symbol(websocket: WebSocket, symbol: str):
    symbol = symbol.upper()
    await websocket.accept()
    await manager.subscribe(symbol, websocket)
    try:
        while True:
            # keep the connection open; client doesn't need to send anything
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.unsubscribe_all(websocket)


@app.get("/health")
def health():
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}