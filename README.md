# Delta — a watchlist that tells you *why* something changed

> Most watchlists show you every tick. Delta suppresses noise and only
> surfaces moves that are actually significant — for *that specific stock*.

## The problem with a normal watchlist

A flat "+2.3%" next to a ticker tells you almost nothing on its own. A 2%
move on a stable blue chip is a real event; the same 2% on a stock that
routinely swings 5% a day is Tuesday. Most watchlists don't distinguish
between the two, so users either get alert fatigue or miss the thing that
actually mattered.

Delta's whole premise: **define "meaningful" relative to each stock's own
behavior**, remember exactly what the user last saw, and only spend
attention re-surfacing what's changed since then.

## What counts as a meaningful change (the core design decision)

A price update is flagged only if at least one of these fires:

| Signal | Why it's not just "% change" |
|---|---|
| **Volatility-adjusted move** | The move is compared to the stock's own typical daily swing (avg true range %). A z-score ≥ 1.5 means "bigger than this stock's normal noise" — not a fixed % for every ticker. |
| **Volume spike** | ≥2x average volume, independent of price direction — unusual participation is often the leading indicator. |
| **Structural crossings** | New 52-week high/low, or crossing the 50-day moving average — levels the *whole market* watches, not just this user. |

Each event carries a plain-English reason ("broke above 30-day high on 2.4x
average volume") rather than a bare number, because the point is
understanding, not just detection. See `backend/app/change_engine.py`.

## Market context — is this move about the stock, or the whole market?

This is the feature I'd lead with in a pitch. Every other signal above
answers "is this move big for THIS stock" — but not whether the move is
actually about the stock at all. A user reading "AAPL -2.1%" needs to know
whether that's AAPL-specific news, or the entire market having a bad day.

Delta silently tracks SPY (an S&P 500 ETF) in the background as a
benchmark — via the exact same poller and snapshot pipeline as any other
symbol, even though no user explicitly added it — and compares a stock's
% move since you last checked against the market's move over that same
window:

- Move is within ~1 point of the market's move → *"Tracks the broader
  market — likely a market-wide move, not news specific to this stock."*
- Move diverges meaningfully from the market → *"Diverges from the
  broader market — likely driven by something specific to this stock."*
- Move is too small (<0.5%) to attribute confidently either way → no
  claim is shown at all, rather than a forced, low-confidence guess.

This is what actually separates "AAPL moved" from "something happened
to AAPL" — most consumer watchlists never make this distinction. See
`compare_to_market()` in `change_engine.py`, tested in
`tests/test_change_engine.py`.

## State persistence across sessions/devices

`WatchlistItem.last_seen_price` / `last_seen_at` are stored **server-side**,
keyed by user, not in browser storage. Log in from any device and the
"what changed since you checked" diff is identical, because it's anchored
to a server timestamp, not a local one. Every tick is also kept in
`PriceSnapshot`, so the diff is correct whether the gap was 5 minutes or
5 days — it's not just comparing the last two ticks.

## Stale, delayed, or conflicting data

Free market-data APIs rate-limit, time out, or lag — that's treated as a
first-class problem, not an edge case:

- **Circuit breaker** (`data_sources.py`): after 3 consecutive failures
  from the live source, it stops hammering a dead upstream and falls back
  immediately instead of timing out on every poll.
- **Honest fallback**: if the live feed is unavailable, a seeded
  random-walk simulator keeps the product fully functional end-to-end for
  demo/dev purposes — and the UI is told the truth about it (`source:
  "simulated"` badge), rather than silently showing fake data as real.
- **Quality labeling**: every snapshot is tagged `fresh` / `delayed` /
  `stale`, shown in the UI, so a user is never misled about how current a
  number is.

## Scaling

If 10,000 users all watch AAPL, the system should not make 10,000 upstream
API calls. The poller (`poller.py`) fetches each **distinct symbol across
the whole platform once per cycle** and fans the result out in-memory to
every subscribed WebSocket connection. Cost scales with unique symbols
tracked platform-wide, not with user count. Noted (not built, deliberately)
for real multi-instance scale: swap the in-process fan-out dict for Redis
Pub/Sub so multiple backend replicas share one feed.

## Where I kept it simple on purpose

- **SQLite, not Postgres** — zero setup for a 72-hour build; the code is
  plain SQLAlchemy, so it's a one-line connection-string change to move to
  Postgres for real deployment.
- **Vanilla JS frontend, no build step** — the interesting engineering
  problem here is backend (change detection, data resilience, fan-out),
  not UI plumbing. A framework would have cost setup time without adding
  understanding. This is a deliberate trade-off, not a shortcut.
- **EMA-based rolling stats instead of storing full price history for
  averages** — cheap, good-enough approximations of avg volume / ATR /
  moving average without needing a historical data warehouse for a demo.

## Known limitations (said out loud, not hidden)

- 52-week high/low is approximated from data collected since the app
  started (a cold-start artifact of a 72-hour build) rather than real
  historical data — with a real data vendor this would seed from actual
  trailing-year data.
- The free live-quote source (Stooq) has no real-time timestamp, so
  "delayed" is a nominal label until swapped for a paid feed with real
  latency metadata.
- On a free-tier deploy (e.g. Render's free plan), the SQLite file lives
  on ephemeral disk — a redeploy or a long idle period wipes it, so
  registered demo accounts won't persist indefinitely. Swapping the one
  connection string in `database.py` for a managed Postgres URL (Render
  and Railway both offer a free Postgres instance) fixes this with no
  code changes elsewhere, since the app is already plain SQLAlchemy.

## Running it and trying it out

New accounts start with a small starter watchlist (AAPL, TSLA, MSFT,
NVDA) already added, so the product is visible immediately rather than
requiring you to type tickers before seeing anything work.

**Backend**
```bash
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload
```
Runs on `http://localhost:8000`. Interactive API docs at `/docs`.

**Tests**
```bash
cd backend
pip install -r requirements-dev.txt
pytest tests/ -v
```
9 tests covering the change-detection engine — the core claim of the
product (e.g. "the same 1% move is noise on a volatile stock but
meaningful on a stable one" is asserted directly, not just described).

**Frontend**
```bash
cd frontend
python3 -m http.server 5500
```
Open `http://localhost:5500`. Register a user, add a few symbols
(e.g. `AAPL`, `TSLA`, `MSFT`), and watch prices update live over
WebSocket — the poller ticks every 8 seconds.

## Project structure
```
backend/
  app/
    main.py            REST + WebSocket routes
    change_engine.py    "what counts as meaningful" — the core logic
    data_sources.py     live quote client + circuit breaker + fallback
    poller.py            deduped background polling + fan-out
    models.py, schemas.py, database.py, auth.py
frontend/
  index.html, app.js, styles.css   no build step, plain fetch + WebSocket
```