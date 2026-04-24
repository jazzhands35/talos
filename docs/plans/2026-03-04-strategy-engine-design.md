# Strategy Engine (Layer 3) Design

**Goal:** Detect NO+NO arbitrage opportunities within Kalshi game events by monitoring orderbooks for both teams and calculating real-time edges.

**Architecture:** Pure state scanner + async game manager. The scanner evaluates pairs on every orderbook update. The game manager handles setup from pasted URLs. Downstream layers (UI, execution) consume opportunity data for manual bidding.

---

## Strategy: Same-Event NO+NO Arbitrage

Each Kalshi game event (e.g., Stanford vs Miami) has two contracts — one per team. Buying NO on both guarantees a $1 payout (exactly one team loses). Profit exists when the combined NO cost is less than $1.

**Pricing:**
- NO ask price = 100 - best YES bid (cents)
- Raw edge = best_yes_bid_A + best_yes_bid_B - 100
- Arbitrage exists when edge > 0

**Example — Stanford vs Miami:**
- Stanford best YES bid: 62¢ (100 qty) → NO ask: 38¢
- Miami best YES bid: 45¢ (200 qty) → NO ask: 55¢
- Total NO cost: 93¢ → raw edge: 7¢
- Tradeable qty: min(100, 200) = 100 contracts
- Max profit: 7¢ × 100 = $7.00 (before fees)

**Fee handling:** The scanner reports raw edges. Fee filtering is a separate downstream concern.

---

## Data Models

### ArbPair

Defines which two markets form a pair within an event:

```python
class ArbPair(BaseModel):
    event_ticker: str
    ticker_a: str
    ticker_b: str
```

### Opportunity

A detected arbitrage opportunity at a point in time:

```python
class Opportunity(BaseModel):
    event_ticker: str
    ticker_a: str
    ticker_b: str
    no_a: int          # NO ask price for leg A (cents)
    no_b: int          # NO ask price for leg B (cents)
    qty_a: int         # quantity available at no_a
    qty_b: int         # quantity available at no_b
    raw_edge: int      # best_bid_a + best_bid_b - 100 (cents)
    tradeable_qty: int # min(qty_a, qty_b)
    timestamp: str     # when computed
```

---

## ArbitrageScanner

Pure state machine — no I/O, no async. Reads orderbook state, evaluates pairs, maintains opportunity list.

```python
class ArbitrageScanner:
    def __init__(self, book_manager: OrderBookManager) -> None: ...

    # Pair management
    def add_pair(self, event_ticker: str, ticker_a: str, ticker_b: str) -> None: ...
    def remove_pair(self, event_ticker: str) -> None: ...

    # Scanning (called after each book update)
    def scan(self, ticker: str) -> None: ...

    # Queries
    @property
    def opportunities(self) -> list[Opportunity]: ...  # sorted by raw_edge desc
    @property
    def pairs(self) -> list[ArbPair]: ...
```

### scan() logic

1. Find all pairs involving `ticker`
2. For each pair, get `best_bid(ticker_a)` and `best_bid(ticker_b)` from OrderBookManager
3. Skip if either is None, zero quantity, or stale
4. Calculate `raw_edge = bid_a.price + bid_b.price - 100`
5. If edge > 0: upsert opportunity. If ≤ 0: remove.

---

## GameManager

Async orchestrator — handles setup from Kalshi URLs, ties REST + feed + scanner together.

```python
class GameManager:
    def __init__(self, rest: KalshiRESTClient, feed: MarketFeed,
                 scanner: ArbitrageScanner) -> None: ...

    async def add_game(self, url: str) -> ArbPair: ...
    async def add_games(self, urls: list[str]) -> list[ArbPair]: ...
    async def remove_game(self, event_ticker: str) -> None: ...

    @property
    def active_games(self) -> list[ArbPair]: ...
```

### add_game() flow

1. Parse event ticker from Kalshi URL
2. Fetch event via REST (`get_event(ticker, with_nested_markets=True)`)
3. Extract the 2 market tickers
4. Register pair with scanner (`scanner.add_pair(...)`)
5. Subscribe to both orderbooks via MarketFeed

---

## Integration: MarketFeed Callback

MarketFeed needs to trigger scanner after book updates. Uses a generic callback to stay decoupled:

```python
# MarketFeed gets an optional callback:
self._on_book_update: Callable[[str], None] | None = None

# After apply_snapshot or apply_delta in _on_message:
if self._on_book_update:
    self._on_book_update(ticker)

# Wiring at startup:
feed.on_book_update = scanner.scan
```

### Full data flow

```
Operator pastes game URLs
        │
        ▼
   GameManager.add_games(urls)
        │
        ├── parse_kalshi_url(url) → event ticker
        ├── rest.get_event(ticker) → 2 markets
        ├── scanner.add_pair(event, ticker_a, ticker_b)
        └── feed.subscribe(ticker_a), feed.subscribe(ticker_b)
                │
                ▼
         WS orderbook data flows in
                │
                ▼
         MarketFeed._on_message()
                │
                ├── books.apply_snapshot/delta()
                └── scanner.scan(ticker)  ← via on_book_update callback
                        │
                        ▼
                 scanner.opportunities updated
                        │
                        ▼
                 UI displays / operator acts
```

---

## Error Handling

- **Missing book data:** `best_bid()` returns None → remove opportunity for that pair
- **Stale books:** Check `LocalOrderBook.stale` → skip, log warning
- **No pairs for ticker:** `scan()` is a no-op (fast path)
- **Zero quantity:** Treat as no bid available
- **URL parse failure:** `add_game()` raises ValueError with descriptive message
- **Event has != 2 markets:** `add_game()` raises ValueError (not a standard game event)

---

## Testing Plan

### ArbitrageScanner (pure state — no mocks)

| Test | Verifies |
|------|----------|
| add/remove pair | Pair management basics |
| scan finds opportunity | Edge > 0 detected, correct prices |
| scan no opportunity | Edge ≤ 0, no opportunity emitted |
| scan removes vanished opp | Edge goes negative → removed |
| scan updates existing opp | Price change → opportunity updated |
| scan missing book data | One leg None → no opportunity |
| scan stale book | Stale flag → skipped |
| opportunities sorted | Highest edge first |
| tradeable_qty is min | min(qty_a, qty_b) |
| scan ignores unrelated ticker | No-op for untracked tickers |

### GameManager (async — mocks REST + feed)

| Test | Verifies |
|------|----------|
| add_game parses URL | Correct ticker extracted |
| add_game fetches and registers | REST called, pair added, subscribed |
| add_game rejects bad URL | ValueError on unparseable URL |
| add_game rejects non-game event | ValueError when != 2 markets |
| remove_game cleans up | Unsubscribes, removes pair |

### MarketFeed callback integration

| Test | Verifies |
|------|----------|
| on_book_update fires after snapshot | Callback called with ticker |
| on_book_update fires after delta | Callback called with ticker |
| no callback registered | No error when callback is None |

---

## Downstream Consumers (not in Layer 3 scope)

- **UI (Layer 5):** Single dashboard showing all active games, their opportunities, and positions. Operator manually places NO bids.
- **Execution (Layer 4):** Takes manual bid instructions from UI, places orders via REST client.
