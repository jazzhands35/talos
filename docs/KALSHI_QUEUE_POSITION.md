# Kalshi Queue Position — How It Works

This document explains how to get queue position data from the Kalshi exchange API so you know where your resting limit orders sit in the order queue.

## What Is Queue Position?

When you place a limit order on Kalshi that doesn't immediately fill, it goes into a queue at your chosen price level. Queue position tells you how many contracts (or dollars) are ahead of you in that queue. A lower number means you're closer to getting filled.

- **Queue position = 1** → You're at the front of the queue. The next incoming market order at your price level fills you first.
- **Queue position = 50** → There are 50 contracts ahead of you. They all get filled before yours does.

## Data Sources

There are three ways to get queue position from Kalshi:

### 1. Dedicated Queue Position Endpoint (Best Source)

```
GET /trade-api/v2/portfolio/orders/queue_positions
```

**Parameters** (at least one is **required** — omitting both returns 400):
- `market_tickers=TICKER1,TICKER2,...` (comma-separated string)
- `market_tickers[]=TICKER1&market_tickers[]=TICKER2` (repeated params)
- `event_ticker=EVENT_TICKER` (all markets under one event)

**Response**:
```json
{
  "queue_positions": [
    {
      "order_id": "abc123-def456-...",
      "queue_position": 15,
      "queue_position_fp": "15.00"
    },
    ...
  ]
}
```

> **⚠ `queue_position_fp` is a STRING**, not a float. The API returns `"2835.00"` not `2835.0`. You must call `float(fp)` before any numeric comparison or arithmetic.

**Notes**:
- This is the most reliable source. Trust it when it returns a positive value.
- Requires authentication (same auth as all portfolio endpoints).
- The response array key might be `queue_positions`, `data`, or `results` depending on API version — check all three.
- Batch up to ~35 market tickers per request to avoid URL length issues.
- If `market_tickers` param format doesn't work, fall back to querying by `event_ticker` instead.

### 2. Portfolio Orders Endpoint (Fallback)

```
GET /trade-api/v2/portfolio/orders
```

Individual order objects in the response may include queue position as a field. The field name is inconsistent across API versions:

```python
# Check these field names in priority order:
QUEUE_FIELD_NAMES = [
    "queue_position_fp",     # dollar-denominated (preferred)
    "queue_position",        # contract count
    "queuePosition",         # camelCase variant
    "queue_pos",             # shorthand
    "priority_position",     # alternate name
    "priorityPosition",      # camelCase alternate
    "priority",              # legacy
    "position_in_queue",     # verbose variant
    "queue_ahead",           # alternate
]
```

Use the first non-empty value you find. This is a fallback — the dedicated endpoint above is more reliable and more current.

### 3. WebSocket Order Updates (Real-Time)

If you subscribe to the Kalshi order update WebSocket channel, order update messages may include queue position fields. Same field name inconsistency applies — check the list above.

**Important**: WS updates can sometimes send `0` or `null` for queue position as a placeholder, even when the order is actually in the queue. Use conservative merge logic (see below).

## Polling Strategy

- Poll the dedicated `/queue_positions` endpoint every **3 seconds** (minimum). More frequent polling wastes rate limit budget without gaining meaningful accuracy.
- Only poll for **resting orders** (orders with `status` = `resting` or `open` that have remaining quantity > 0). Don't waste requests on filled/cancelled orders.
- Cache results keyed by `order_id`. Merge new results on top of existing cache.
- The dedicated endpoint takes priority over queue values from the orders endpoint or WebSocket.

## Handling Edge Cases

### `queue_position_fp` vs `queue_position`

Kalshi has two conventions:
- `queue_position_fp` — dollar-denominated, **returned as a STRING** (e.g., `"15.00"` means $15 of contracts ahead of you). Must `float()` before use.
- `queue_position` — integer, may be contract count or dollars depending on context

Prefer `_fp` when both are present. Use `max(1, round(float(fp)))` for positive values to avoid small fractional values rounding to zero.

### Partially Filled Orders

When an order has been partially filled (some contracts executed, some remaining), the API sometimes returns `0` or omits the queue position entirely.

**Do NOT assume position 1.** A partially filled order is not necessarily at the front — partial fills can happen from large incoming orders that sweep through multiple price levels. Only display a queue position if the dedicated endpoint returns a positive value.

### Zero Queue Position

**Zero means "no data available", NOT "front of queue".** This was verified empirically — orders deep in the queue (positions 2000+) were incorrectly displayed as position 1 when zero was treated as "front".

- `0` from the dedicated endpoint → no data, display as unknown
- `0` from the orders endpoint → always no data (field is deprecated)
- Only trust and display **positive** values

### Conservative Merge for Real-Time Updates

When merging queue position from multiple sources (REST poll + WebSocket updates), use this logic:

```python
def merge_queue_position(existing, incoming):
    """Keep the smallest positive queue position. Never let 0 or null
    overwrite a known positive value."""
    if incoming is None:
        return existing
    if existing is None:
        return incoming
    if incoming <= 0 < existing:
        return existing       # Don't let 0/negative overwrite positive
    if existing <= 0 < incoming:
        return incoming        # Replace 0/negative with positive
    return min(existing, incoming)  # Both positive: keep the smaller one
```

The rationale: queue position can only stay the same or improve (get smaller) as orders ahead of you get filled. A sudden jump from 5 to 50 is a data artifact. A drop from 50 to 0 that later corrects back to 5 is also an artifact. Keeping the smallest positive value avoids both.

## Putting It All Together

Here's the recommended flow:

```
1. On startup / when orders change:
   - Collect market tickers for all resting orders
   - Call GET /trade-api/v2/portfolio/orders/queue_positions
     with batches of ~35 tickers
   - Store results: { order_id -> queue_position }

2. Every ~3 seconds:
   - Re-fetch queue positions for active tickers
   - Merge into cache using conservative merge

3. If using WebSocket order updates:
   - Extract queue_position from update messages
   - Merge into cache using conservative merge
   - WS gives you faster updates between REST polls

4. For display / decision-making:
   - Read from cache by order_id
   - If positive value exists → use it (authoritative)
   - If value is 0 or missing → display as unknown (—)
   - Do NOT assume 0 means "front of queue" — it means no data
```

## Authentication

All portfolio endpoints require Kalshi API authentication. You need:
- API key (member ID)
- RSA private key for signing requests

Auth is done via RSA-PSS signatures. Each request signs a timestamp + method + path string. See Kalshi's API docs for the full auth flow — it's the same auth used for placing orders, fetching positions, etc.

## Rate Limiting

Queue position polling shares the same global per-API-key rate limit as all other Kalshi API calls (order placement, position fetching, market data, etc.). Budget accordingly — if your app also places orders and fetches orderbooks, those all compete for the same rate limit bucket. A 429 response means you've hit the limit; back off and retry.

## Quick Reference

| What | Where |
|------|-------|
| Best data source | `GET /trade-api/v2/portfolio/orders/queue_positions` |
| Fallback source | `queue_position` fields on order objects from `/portfolio/orders` |
| Real-time source | WebSocket order update channel |
| Poll frequency | Every 3+ seconds |
| Batch size | ~35 market tickers per request |
| Auth | RSA-PSS signed requests (same as all portfolio endpoints) |
| Field names to check | `queue_position_fp`, `queue_position`, `queuePosition`, `queue_pos`, `position_in_queue` |
| Merge strategy | Conservative — keep smallest positive value |
