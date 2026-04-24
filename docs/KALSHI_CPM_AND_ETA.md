# Kalshi CPM and Fill ETA — How It Works

This document explains how to calculate **CPM** (Contracts Per Minute) and **fill ETA** ("up to bat" / "sold out" timers) for resting orders on Kalshi's orderbook. These tell you how fast a price level is trading and how long until your order gets filled.

## Concepts

- **CPM (Contracts Per Minute)**: The rate at which contracts are being traded at or near a price level. Higher CPM = more liquid, faster fills.
- **Up to Bat ETA**: Estimated minutes until your order reaches the front of the queue (starts getting filled).
- **Sold Out ETA**: Estimated minutes until your entire order is completely filled.

## What Data You Need

### 1. Trade History (for CPM)

Fetch recent trades for the market from Kalshi's trades endpoint:

```
GET /trade-api/v2/markets/{market_ticker}/trades?limit=120
```

**Alternative endpoints** (try in order if one doesn't work):
- `GET /trade-api/v2/markets/trades?market_ticker={ticker}&limit=120`
- `GET /trade-api/v2/markets/trades?ticker={ticker}&limit=120`
- `GET /trade-api/v2/trades?market_ticker={ticker}&limit=120`

**Response** contains an array of trade objects. The response key may be `trades`, `fills`, `data`, or `results` — check all four.

Each trade object has (post March 12, 2026 — legacy integer fields removed):

| Field | API Field Name | Format | Description |
|-------|---------------|--------|-------------|
| Timestamp | `created_time` | ISO 8601 string | When the trade happened |
| Side | `taker_side` | string | Which outcome was taken (YES or NO) |
| Quantity | `count_fp` | string (e.g., `"10"`) | Number of contracts traded |
| YES price | `yes_price_dollars` | string (e.g., `"0.65"`) | Price in dollars for the YES side |
| NO price | `no_price_dollars` | string (e.g., `"0.35"`) | Price in dollars for the NO side |
| Trade ID | `trade_id` | string | Unique trade identifier (for deduplication) |

> **Note:** Legacy integer fields (`count`, `yes_price`, `no_price`, `price`) were removed March 12, 2026. Talos's `Trade` model converts `_dollars`/`_fp` strings to int cents/int counts via `_migrate_fp` validator.

**Timestamp normalization**: If the timestamp is > 10^12, it's in milliseconds — divide by 1000 to get seconds. If it's a string (ISO 8601), parse it. If nothing is available, use current time as fallback.

**Price normalization**: Post March 12, prices come as dollar strings (e.g., `"0.65"`) via `_dollars` fields. Multiply by 100 to get cents. The Pydantic model handles this conversion automatically.

### 2. Queue Position (for ETA)

See the separate [KALSHI_QUEUE_POSITION.md](KALSHI_QUEUE_POSITION.md) document. You need to know how many contracts are ahead of your order in the queue.

### 3. Your Order Info (for ETA)

- **Your order quantity** at this price level
- **Queue position** from the queue positions endpoint

## Step 1: Parse Trades Into Flow Events

Each trade generates **flow events** — records of contracts moving at a specific price on a specific side of the book.

For each trade, determine:

1. **Outcome**: YES or NO (from the `taker_side` / `side` field)
2. **Book side**: Was this trade hitting the BID or the ASK?
   - If `taker_side` is available: taker buying = hitting the ASK, taker selling = hitting the BID
   - Specifically: if `taker_side == outcome`, it's an **ASK** hit. If `taker_side != outcome`, it's a **BID** hit.
3. **Price in cents**: The YES-side price and NO-side price (they sum to 100). If only one is given, derive the other: `no_price = 100 - yes_price`.
4. **Quantity**: Number of contracts in this trade.

**A single trade can produce TWO flow events** — one for YES and one for NO — because every trade has both a YES price and a NO price (they're complementary). For each outcome where you can determine a valid price (1–99 cents) and book side (BID or ASK), create a flow event.

**Deduplicate** by trade ID to avoid counting the same trade twice (important if you fetch overlapping data from multiple polls).

### Flow Event Structure

```
flow_event = {
    outcome: "yes" or "no",
    book_side: "BID" or "ASK",
    price_cents: 1-99,
    quantity: float,
    timestamp: float (unix seconds)
}
```

### Storage

Store flow events in a dict keyed by `"{TICKER}|{outcome}|{book_side}|{price_cents}"`:

```python
# Key: "TICKER-YES-BID-55"
# Value: list of (timestamp, quantity) tuples
flow = {
    "KXYZ-24|yes|BID|55": [(1709720000.0, 10.0), (1709720030.0, 5.0), ...],
    "KXYZ-24|no|ASK|45":  [(1709720015.0, 10.0), ...],
}
```

**Retention**: Keep events for ~1 hour (3600–3700 seconds). Prune on every update. Cap at ~320 events per key to bound memory.

## Step 2: Calculate CPM

CPM is calculated over three time windows:

| Window | Duration | Use Case |
|--------|----------|----------|
| CPM5 | 5 minutes (300s) | Recent activity, used for ETA calculations |
| CPM30 | 30 minutes (1800s) | Medium-term trend |
| CPM60 | 60 minutes (3600s) | Long-term baseline |

### Basic Formula

```
CPM(window) = total_quantity_in_window / (window_seconds / 60)
```

For a 5-minute window with 30 contracts traded:
```
CPM5 = 30 / (300 / 60) = 30 / 5 = 6.0 contracts per minute
```

### Per-Price-Level CPM

Filter events to only those matching your specific price level:

```python
def cpm_for_price(flow_events, window_sec):
    now = time.time()
    cutoff = now - window_sec
    qty_sum = sum(qty for ts, qty in flow_events if ts >= cutoff)
    return qty_sum / (window_sec / 60.0)
```

### Aggregate CPM (All Price Levels)

Sum events across ALL price levels for a given (ticker, outcome, book_side):

```python
def cpm_aggregate(all_flow, ticker, outcome, book_side, window_sec):
    now = time.time()
    cutoff = now - window_sec
    total = 0.0
    for key, events in all_flow.items():
        if key.startswith(f"{ticker}|{outcome}|{book_side}|"):
            total += sum(qty for ts, qty in events if ts >= cutoff)
    return total / (window_sec / 60.0)
```

Use aggregate CPM as a **fallback** when per-price-level CPM has no data (the specific price level hasn't traded recently, but the overall market has).

### Short-Window Adjustment

When you haven't been observing long enough to fill a window, use the actual observed time instead:

```python
first_event_ts = min(ts for ts, qty in all_events)
observed_sec = now - first_event_ts

if observed_sec < window_sec:
    use_sec = max(1.0, observed_sec)  # Avoid division by zero
else:
    use_sec = window_sec

cpm = qty_sum / (use_sec / 60.0)
```

### Partial Flag

Mark the CPM as "partial" (display with an asterisk `*`) when you have less than 5 minutes of observation:

```python
partial = observed_sec < 300.0  # Less than 5 minutes of data
```

This tells the user "this rate is extrapolated from limited data — take it with a grain of salt."

### Alternative CPM Source: Top-of-Book Quantity Changes

In addition to trade events, you can compute CPM by watching the **top-of-book quantity decrease** on each orderbook update. When the best bid/ask quantity drops, that means contracts were filled:

```python
# On each orderbook update for a price level:
if current_qty < previous_qty:
    delta = previous_qty - current_qty
    events.append((now, delta))
```

This is useful as a supplemental signal when trade history is sparse or delayed. It detects fills in real-time from orderbook snapshots/deltas without waiting for the trades endpoint to update.

## Step 3: Calculate Fill ETA

Fill ETA combines **queue position** (how many contracts are ahead of you) with **CPM** (how fast contracts are trading).

### Up to Bat (Time to Front of Queue)

```
up_to_bat_minutes = queue_position / CPM5
```

This estimates how long until your order reaches the front of the queue and starts getting filled.

**Example**: Queue position = 30, CPM5 = 6.0 → `30 / 6 = 5.0 minutes`

### Sold Out (Time to Complete Fill)

```
sold_out_minutes = (queue_position + your_order_size) / CPM5
```

This estimates how long until your entire order is filled. It accounts for both the contracts ahead of you AND the time to work through your own order.

```
sold_out_minutes = max(sold_out_minutes, up_to_bat_minutes)  # Never less than up-to-bat
```

**Example**: Queue position = 30, your order = 10 contracts, CPM5 = 6.0 → `(30 + 10) / 6 = 6.7 minutes`

### When CPM Is Zero or Missing

If CPM5 is zero or unavailable (no recent trades), display **infinity** (`∞`). You can't estimate a fill time with no trading activity.

### When to Show ETAs

Only show ETAs when ALL of these are true:
- You have an active order at this price level (`mine_qty > 0`)
- You have a valid queue position (`queue_position is not None`)
- The price level is the best price (top of book) — CPM for non-top levels is less meaningful since trades primarily happen at the best price

## Display Formatting

### CPM Values

```python
def format_cpm(value, partial=False):
    if value is None:
        return "--"
    v = abs(float(value))
    if v >= 1000:
        text = f"{value:,.0f}"     # "1,500"
    elif v >= 100:
        text = f"{value:.0f}"      # "150"
    elif v >= 10:
        text = f"{value:.1f}"      # "15.7"
    else:
        text = f"{value:.2f}"      # "5.12"
    if partial:
        text += "*"                # Asterisk = extrapolated from <5min data
    return text
```

### ETA Values

```python
def format_eta(minutes, partial=False, round_hours_after=None):
    m = max(0.0, float(minutes))
    if not math.isfinite(m) or m > 525600:  # >1 year
        text = "∞"
    elif m >= 60:
        hours = m / 60.0
        if round_hours_after and hours > round_hours_after:
            text = f"{int(round(hours))}h"    # "8h"
        else:
            text = f"{hours:.1f}h"             # "2.5h"
    else:
        text = f"{max(1, int(round(m)))}m"     # "5m" (minimum 1m)
    if partial:
        text += "*"
    return text
```

For "sold out" ETA, use `round_hours_after=5.0` — beyond 5 hours, round to the nearest whole hour since precision is meaningless at that scale.

## Putting It All Together

```
1. Fetch trades periodically (every few seconds)
   GET /trade-api/v2/markets/{ticker}/trades?limit=120

2. Parse each trade into flow events:
   For each trade → extract (outcome, book_side, price, qty, timestamp)
   Store in flow dict keyed by "ticker|outcome|side|price"
   Deduplicate by trade_id

3. Calculate CPM for each active price level:
   CPM5  = sum(qty in last 300s)  / 5.0
   CPM30 = sum(qty in last 1800s) / 30.0
   CPM60 = sum(qty in last 3600s) / 60.0
   If per-level CPM is empty, fall back to aggregate (all levels for that side)

4. For price levels where you have resting orders:
   Fetch queue position (see KALSHI_QUEUE_POSITION.md)
   up_to_bat  = queue_position / CPM5
   sold_out   = (queue_position + your_qty) / CPM5

5. Display:
   CPM columns: "6.00", "4.3", "150", "--"
   ETA columns: "5m", "2.5h", "∞", "12m*"
```

## Edge Cases Summary

| Situation | Behavior |
|-----------|----------|
| No trades in window | CPM = 0.0, ETA = ∞ |
| Less than 5 min of data | Use actual observed time, mark with `*` |
| No queue position data | Don't show ETA |
| Order partially filled | Assume queue position = 1 (front) |
| Price not at top of book | Show CPM but typically skip ETA |
| CPM5 = 0 but CPM30 > 0 | ETA = ∞ (use CPM5 for ETA, show CPM30 separately for context) |
| Multiple price levels active | Calculate CPM per-level; aggregate is fallback only |

## Data Housekeeping

| Item | Limit | Purpose |
|------|-------|---------|
| Event retention | ~3700 seconds (~1 hour) | Drop stale events |
| Events per price level | ~320 max | Bound memory |
| Trade dedup cache | ~20,000 entries, 1-hour TTL | Prevent double-counting |
| Flow keys (total) | ~1200 max | Evict least-recent when exceeded |
| CPM cache TTL | 1–2 seconds | Avoid recalculating on every render |
