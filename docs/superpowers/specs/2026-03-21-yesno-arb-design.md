# YES/NO Arbitrage Support for Non-Sports Markets

**Date:** 2026-03-21
**Status:** Approved design, pending implementation plan

## Problem

Kalshi's sports markets are temporarily unavailable. Talos currently only supports cross-market NO+NO arbitrage on sports events (buy NO on both sides of a 2-market event). Non-sports markets (politics, entertainment, crypto, weather) require a different arb strategy: YES/NO arbitrage within a single market.

## Core Insight

YES/NO arb on a single market is mathematically identical to cross-market NO+NO arb. Both guarantee a $1 payout per contract pair:

- **Sports (cross-NO):** Buy NO-A + NO-B on two markets. One NO settles at $1, the other at $0. Edge = `100 - no_a - no_b`.
- **Non-sports (YES/NO):** Buy YES + NO on the same market. One settles at $1, the other at $0. Edge = `100 - yes_ask - no_ask`.

The fee model, position tracking, rebalance logic, and all safety gates work identically.

## Design

### 1. Data Model — Side-Aware Legs

Add `side_a`, `side_b`, and `kalshi_event_ticker` fields to `ArbPair`:

```python
class ArbPair(BaseModel):
    event_ticker: str              # Unique pair key (market ticker for YES/NO pairs)
    ticker_a: str                  # For YES/NO: same as ticker_b
    ticker_b: str
    side_a: str = "no"             # "yes" or "no"
    side_b: str = "no"             # "yes" or "no"
    kalshi_event_ticker: str = ""  # Real Kalshi event ticker for API calls
    fee_type: str = "quadratic_with_maker_fees"
    fee_rate: float = 0.0175
    close_time: str | None = None
    expected_expiration_time: str | None = None
```

- Sports: `side_a="no", side_b="no"`, different tickers. `kalshi_event_ticker == event_ticker`.
- YES/NO: `side_a="yes", side_b="no"`, same ticker. `event_ticker` = market ticker (unique key), `kalshi_event_ticker` = real event ticker.

**`kalshi_event_ticker`** solves the API collision: `event_ticker` stays as the unique pair key used in ledgers, proposals, and UI. `kalshi_event_ticker` is passed to Kalshi REST calls like `get_all_orders(event_ticker=...)`. For sports pairs, they're identical. For YES/NO pairs, `event_ticker` is the market ticker and `kalshi_event_ticker` is the real Kalshi event ticker.

**Helper property:**
```python
@property
def api_event_ticker(self) -> str:
    """Event ticker for Kalshi API calls."""
    return self.kalshi_event_ticker or self.event_ticker
```

**Same-ticker detection property:**
```python
@property
def is_same_ticker(self) -> bool:
    """True when both legs trade the same market (YES/NO arb)."""
    return self.ticker_a == self.ticker_b
```

### 2. Series Lists & Sports Block

Split `SCAN_SERIES` into two lists:

```python
SPORTS_SERIES = ["KXNHLGAME", "KXNBAGAME", ...]   # existing 55+ tickers
NON_SPORTS_SERIES = [...]                            # curated, starts small
```

Add a toggle (config or constructor parameter):

```python
sports_enabled: bool = False  # hard block — rejects sports tickers everywhere
```

When `sports_enabled=False`:
- `scan_events()` iterates only `NON_SPORTS_SERIES`
- `add_game()` rejects events whose `series_ticker` is in `SPORTS_SERIES`
- `restore_game()` skips sports pairs from old cache files

### 3. Non-Sports Event Expansion

Series membership determines the path, not market count:

- **Sports series:** Existing 2-market validation, cross-NO pairing.
- **Non-sports series, 1 active market:** Auto-add as YES/NO pair.
- **Non-sports series, 2+ active markets:** Show market picker modal. User space-toggles markets, Enter to add selected. Each selected market becomes its own `ArbPair`.

For YES/NO pairs, `event_ticker` on `ArbPair` holds the **market ticker** as a unique pair key. The real Kalshi event ticker is stored in `kalshi_event_ticker` for API calls.

New method on `GameManager`:

```python
async def add_market_as_pair(self, event: Event, market: Market) -> ArbPair:
    """Create a YES/NO arb pair from a single market."""
    # event_ticker = market.ticker (unique key)
    # kalshi_event_ticker = event.event_ticker (for API calls)
    # ticker_a = ticker_b = market.ticker
    # side_a = "yes", side_b = "no"
```

### 4. Orderbook — Read Both Sides

Parameterize `OrderBookManager.best_ask()`:

```python
def best_ask(self, ticker: str, side: str = "no") -> OrderBookLevel | None:
    book = self._books.get(ticker)
    if not book:
        return None
    levels = book.no if side == "no" else book.yes
    return levels[0] if levels else None
```

The WS subscription already delivers both YES and NO sides in every snapshot (`yes_dollars_fp` / `no_dollars_fp`) and delta (`side: "yes" | "no"`). `LocalOrderBook` already stores both. No subscription or WS changes needed.

For YES/NO pairs where `ticker_a == ticker_b`, only one subscription is needed. `MarketFeed.subscribe()` is already idempotent.

### 5. Scanner — Side-Aware Evaluation

`ArbitrageScanner._evaluate_pair()` reads the correct book side per leg:

```python
no_a = self._books.best_ask(pair.ticker_a, side=pair.side_a)
no_b = self._books.best_ask(pair.ticker_b, side=pair.side_b)
```

The field names `no_a`/`no_b` on `Opportunity` are semantically "price of leg A/B" — the math is identical regardless of which side they represent. No rename needed.

`add_pair()` must accept and forward `side_a`/`side_b` parameters to the `ArbPair` constructor and store them on the pair.

### 6. Engine — Side-Aware Order Placement & Sync

**`place_bids()`** reads side from the pair:

```python
pair = self._find_pair(bid.event_ticker)

for side_enum, ticker, price, pair_side in [
    (Side.A, bid.ticker_a, bid.no_a, pair.side_a),
    (Side.B, bid.ticker_b, bid.no_b, pair.side_b),
]:
    order = await self._rest.create_order(
        ticker=ticker,
        action="buy",
        side=pair_side,
        yes_price=price if pair_side == "yes" else None,
        no_price=price if pair_side == "no" else None,
        count=bid.qty,
    )
```

`create_order()` already accepts both `yes_price` and `no_price` — no REST client changes.

**Order sync filtering** — every location that checks `order.side != "no"` must become side-aware:

```python
# Engine reconciliation (_reconcile_orders_vs_positions, line 1287):
pair = self._find_pair(event_ticker)
expected_sides = {pair.side_a, pair.side_b}
if order.action != "buy" or order.side not in expected_sides:
    continue
```

**Same-ticker order-to-side mapping:** For YES/NO pairs where `ticker_a == ticker_b`, the order's `side` field ("yes"/"no") determines Side.A vs Side.B, not the ticker.

**WS handler `_on_order_update`** (engine.py:940) — currently drops all non-NO orders:
```python
# Before:
if msg.side == "no" and msg.status in ("resting", "executed"):

# After — check against pair's expected sides:
pair = self._find_pair_by_ticker(msg.ticker)
if pair and msg.side in {pair.side_a, pair.side_b} and msg.status in ("resting", "executed"):
```

**Top-up placement** (engine.py:1573) — hardcodes `side="no"`. Must use pair side:
```python
pair_side = pair.side_a if side == Side.A else pair.side_b
await self._rest.create_order(
    ticker=ticker, action="buy", side=pair_side,
    yes_price=price if pair_side == "yes" else None,
    no_price=price if pair_side == "no" else None,
    count=qty,
)
```

**API calls using `event_ticker`** — all `get_all_orders(event_ticker=...)` and similar calls must use `pair.api_event_ticker` instead of `pair.event_ticker`:
- `_verify_after_action`
- `_reconcile_orders_vs_positions`
- `_discover_active_events` startup merge

### 7. Rebalance — Side-Aware Orders

All `create_order` calls in `rebalance.py` use the pair's side info:

**Catch-up order** (rebalance.py:547) — `ProposedRebalance` must carry `catchup_side: str` so the execution knows which Kalshi side to use:
```python
await rest_client.create_order(
    ticker=rebalance.catchup_ticker,
    action="buy",
    side=rebalance.catchup_side,  # from pair.side_a or pair.side_b
    yes_price=price if rebalance.catchup_side == "yes" else None,
    no_price=price if rebalance.catchup_side == "no" else None,
    count=qty,
)
```

**`_cancel_all_resting`** (rebalance.py:612) — currently filters `order.side != "no"`. Must accept the target side as a parameter:
```python
# Before:
if order.side != "no" or order.action != "buy":
    continue

# After:
if order.side != target_side or order.action != "buy":
    continue
```

**API calls** — `get_all_orders(event_ticker=...)` in the catch-up fresh sync must use the real Kalshi event ticker.

### 8. PositionLedger — Side-Aware Sync

**Moved from "untouched" to "changed."**

**8a. `sync_from_orders()` ticker-to-side collision**

`sync_from_orders()` (position_ledger.py:314) uses `ticker_to_side` dict to map orders to Side.A/Side.B:

```python
# Before:
ticker_to_side = {ticker_a: Side.A, ticker_b: Side.B}

# Problem: when ticker_a == ticker_b, dict has one entry, Side.A lost.
```

**Fix:** When `ticker_a == ticker_b` (same-ticker pair), use the order's `side` field ("yes"/"no") for mapping instead of the ticker. The ledger needs to know the pair's sides:

```python
# New parameter or stored state:
if ticker_a == ticker_b:
    # Map by order.side field
    side_map = {side_a_str: Side.A, side_b_str: Side.B}  # e.g., {"yes": A, "no": B}
    for order in orders:
        if order.action != "buy" or order.side not in side_map:
            continue
        side = side_map[order.side]
        ...
else:
    # Existing ticker-based mapping
    ticker_to_side = {ticker_a: Side.A, ticker_b: Side.B}
    for order in orders:
        if order.side != "no" or order.action != "buy":
            continue
        side = ticker_to_side.get(order.ticker)
        ...
```

Store `side_a_str` / `side_b_str` on `PositionLedger` at construction (set by engine when creating the ledger). The ledger knows its side mapping once and uses it consistently.

**8b. `sync_from_positions()` — skip for same-ticker pairs**

Kalshi's `GET /portfolio/positions` returns **net** position per market ticker. If you hold 10 YES + 10 NO on the same market, Kalshi reports position = 0 (they cancel out). This makes `sync_from_positions` useless for same-ticker YES/NO pairs.

**Fix:** Skip `sync_from_positions` for same-ticker pairs. Rely solely on `sync_from_orders`. This is safe because:
- `sync_from_positions` is the secondary augmentative source — it only patches gaps from order archival
- YES/NO arb orders are recent and won't be archived during the session
- `sync_from_orders` is the primary authoritative source

The same netting issue affects:
- **`_verify_after_action`** (engine.py:2020-2028) — position verification for same-ticker pairs should skip the positions check (order-only verification is sufficient)
- **`_discover_active_events`** (engine.py:560) — net-zero positions from same-ticker YES/NO won't appear in the discovery. Same-ticker pairs must be restored from cache, not discovered from positions

Add a guard in `sync_from_positions`:
```python
if self._is_same_ticker:
    return  # Positions API reports net, useless for YES/NO pairs
```

### 9. BidAdjuster — Same-Ticker Collision

**`_ticker_map` collision** (bid_adjuster.py:41-43): When `ticker_a == ticker_b`, the second entry overwrites the first. Side.A is permanently lost.

**Fix:** Store a list of `(pair, Side)` per ticker. When looking up, disambiguate by the order's `side` field:

```python
# Before:
self._ticker_map[pair.ticker_a] = (pair, Side.A)
self._ticker_map[pair.ticker_b] = (pair, Side.B)

# After:
self._ticker_map: dict[str, list[tuple[ArbPair, Side]]]
# For same-ticker pairs, both entries exist in the list
# Disambiguation: match order.side against pair.side_a/side_b
```

**`evaluate_jump()`** — when resolving ticker to (pair, side), check the resting order's side to disambiguate.

**`execute()`** (bid_adjuster.py:447) — hardcodes `side="no"` in `amend_order`. Must use the pair's side for the leg being amended:
```python
pair_side = pair.side_a if side == Side.A else pair.side_b
old_order, amended_order = await rest_client.amend_order(
    proposal.cancel_order_id,
    ticker=ticker,
    side=pair_side,
    action="buy",
    yes_price=proposal.new_price if pair_side == "yes" else None,
    no_price=proposal.new_price if pair_side == "no" else None,
    count=total_count,
)
```

**`_side_ticker()`** — returns the ticker for a given side. For same-ticker pairs, both sides return the same ticker. This is fine — callers that need the side use the `_ticker_map` which now disambiguates.

### 10. TopOfMarketTracker — Side-Aware Jump Detection

**Not addressed in original spec — critical gap.**

`TopOfMarketTracker.update_orders` (top_of_market.py:42) filters `order.side != "no"` and stores `max(order.no_price)` per ticker. For YES/NO pairs:

- YES-side orders are filtered out — jumps on the YES leg are invisible
- For same-ticker pairs, YES and NO resting orders would collide in the per-ticker price map

**Fix:** Track resting prices per `(ticker, side)` tuple instead of per ticker:

```python
# Before:
self._resting: dict[str, int]  # ticker -> resting price

# After:
self._resting: dict[tuple[str, str], int]  # (ticker, side) -> resting price
```

`detect_jump()` checks the correct `(ticker, side)` key. The BidAdjuster's `evaluate_jump()` passes the side through.

### 11. REST Client — `amend_order` Needs `yes_price`

`rest_client.py:amend_order()` only accepts `no_price`, not `yes_price`. Add `yes_price: int | None = None` parameter mirroring `create_order`'s signature. Build `yes_price_dollars` in the request body when provided.

### 12. Engine Reconciliation — `ticker_to_side` Collision

`_reconcile_orders_vs_positions` (engine.py:1278) has the same `ticker_to_side` dict collision as `sync_from_orders`. When `ticker_a == ticker_b`, Side.A is overwritten. Same fix: use order.side-based mapping for same-ticker pairs.

### 13. Settlement & Revenue

**Settlement revenue calculation** (engine.py:1154-1158) assumes both legs are NO positions:

```python
# Before:
if result_a == "no":
    revenue += filled_a * 100

# After — check against the pair's sides:
# For YES/NO pair: Side.A is YES, so it pays out when result == "yes"
if (pair.side_a == "no" and result_a == "no") or (pair.side_a == "yes" and result_a == "yes"):
    revenue += filled_a * 100
```

Generalized: a leg pays out when the market result matches the leg's side.

### 14. UI Labels

Reuse existing table columns, no layout changes:

- **Team A / Team B columns:** `"Cardi B - YES"` / `"Cardi B - NO"`
- **Lg column:** Truncated market title or event category (e.g., `"Perform Superbowl"`)
- **All price/position/edge columns:** Identical math, same display

Label generation in `GameManager`:
```python
def _build_yesno_labels(self, market_title: str) -> tuple[str, str]:
    short = market_title.removeprefix("Will ").removesuffix("?").strip()
    if len(short) > 30:
        short = short[:27] + "..."
    return (f"{short} - YES", f"{short} - NO")
```

**Sport/league lookups** (`_SPORT_LEAGUE` in engine.py, screens.py) — add non-sports series to the mapping with appropriate category labels (e.g., "Entertainment", "Politics").

Game status column returns no data for unmapped non-sports series — existing fallback handles this (shows expiration time or blank).

### 15. Persistence

Add `side_a`, `side_b`, and `kalshi_event_ticker` to saved game data in `__main__.py`. `restore_game()` reads with defaults (`"no"` for sides, `""` for kalshi_event_ticker) for backward compatibility. Sports pairs in old cache files are skipped when `sports_enabled=False`.

### 16. Market Picker Modal

For non-sports events with 2+ active markets, a market picker sub-modal shows all active markets:

- Reuses `ScanScreen` pattern (DataTable with Space-to-toggle, Enter to add selected)
- Columns: Market title, 24h volume, YES ask, NO ask, spread
- Triggered from `add_game()` when event is non-sports with multiple markets
- Returns list of selected `Market` objects to `add_market_as_pair()`

### 17. Side Threading Through Callers

All callers of `best_ask()` must pass the correct side. Key sites beyond the scanner:

- **`bid_adjuster.py`** — `evaluate_jump()`, `_is_jumped()`: look up side from `_ticker_map` (now list-based, returns `(pair, Side)` which gives access to `pair.side_a`/`pair.side_b`)
- **`top_of_market.py`** — `detect_jump()`: uses `(ticker, side)` key from Section 10
- **`rebalance.py`** — `compute_rebalance_proposal()` calls `best_ask(pair.ticker_a)` / `best_ask(pair.ticker_b)` to check market availability. Must pass `pair.side_a` / `pair.side_b`

### 18. Data Collector Logging

WS handler (engine.py:929) logs `price=msg.no_price` regardless of order side. For YES-side orders, use `msg.yes_price`. Similarly auto-accept logging (engine.py:1756) uses `order.no_price`. Fix: use `order.no_price if order.side == "no" else order.yes_price`.

### 19. `_cancel_all_resting` API Call

`_cancel_all_resting` (rebalance.py:603) calls `get_all_orders(event_ticker=event_ticker)`. This receives the pair's `event_ticker` (which is the market ticker for YES/NO pairs). Must pass `pair.api_event_ticker` instead. Thread through from the caller.

### 20. `BidConfirmation` Side Fallback

`place_bids()` looks up the pair to get sides. If `_find_pair` returns `None` (shouldn't happen but defensive), fall back to `side="no"` with a warning log rather than silently placing the wrong side order.

## What Stays Untouched

- **`OpportunityProposer`** — generic edge/stability/position gates
- **`ProposalQueue`** — pure state machine
- **`fees.py`** — symmetric math, handles all fee types including fee-free
- **`fee_adjusted_profit_matched`** — settlement math identical (100 - costs)
- **`MarketFeed` / `ws_client`** — already delivers both sides
- **UI table layout / CSS** — no structural changes

## Modules Changed

| Module | Change |
|--------|--------|
| `models/strategy.py` | Add `side_a`, `side_b`, `kalshi_event_ticker`, `is_same_ticker` to `ArbPair`. Add `catchup_side` to `ProposedRebalance` |
| `game_manager.py` | Split series lists, sports toggle, non-sports event expansion, label generation, `add_market_as_pair()` |
| `scanner.py` | Pass `side` to `best_ask()`, update `add_pair()` to accept sides |
| `orderbook.py` | Parameterize `best_ask(side=)` |
| `engine.py` | Side-aware order placement, order sync filtering, WS handler, top-up, reconciliation (`ticker_to_side` collision), settlement revenue, API event_ticker, data collector logging, `_verify_after_action` skip positions for same-ticker |
| `rebalance.py` | Side-aware `create_order`, `_cancel_all_resting` (target side + api_event_ticker), catch-up, `best_ask` side threading |
| `rest_client.py` | Add `yes_price` parameter to `amend_order()` |
| `position_ledger.py` | Side-aware `sync_from_orders`, skip `sync_from_positions` for same-ticker, store side mapping at construction |
| `bid_adjuster.py` | `_ticker_map` list-based for same-ticker. Side-aware `evaluate_jump`, `execute()` amend, `best_ask` side threading |
| `top_of_market.py` | Track resting prices per `(ticker, side)` tuple, side-aware jump detection |
| `__main__.py` | Persist/restore `side_a`/`side_b`/`kalshi_event_ticker`, sports toggle config |
| `ui/screens.py` | Market picker sub-modal for non-sports events |

## Future Extensions

- Volume-based automatic market selection (replace manual picker)
- `--mode sports|nonsports|all` CLI arg
- Re-enable sports when Kalshi restores them (flip toggle)
