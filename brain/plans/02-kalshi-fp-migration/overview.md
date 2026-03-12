# Kalshi Fixed-Point Migration

Back to [[plans/index]]

## Context

On March 12, 2026, Kalshi removed all legacy integer fields from REST and WebSocket response payloads. Integer cents price fields (e.g., `yes_bid`, `no_price`) and integer count fields (e.g., `fill_count`, `remaining_count`) no longer exist. They are replaced by:

- **`_dollars` fields** — string, dollar-denominated (e.g., `"0.52"` = 52 cents)
- **`_fp` fields** — string, fixed-point contracts (e.g., `"10.00"` = 10 contracts)

This breaks every Pydantic model in Talos that parses API responses. The system cannot connect to Kalshi until this is fixed.

## Scope

**In scope:**
- All Pydantic models that parse Kalshi REST or WS responses
- REST client request payloads (`create_order`, `amend_order`)
- REST client response key changes (`orderbook` → `orderbook_fp`)
- Test fixtures that mock API responses

**Out of scope:**
- Internal representation changes — Talos stays on int cents / int contracts
- Downstream logic (engine, scanner, ledger, proposer, UI) — unchanged
- Fractional trading support (Talos uses whole contracts only)
- Subpenny pricing support (not relevant to current strategy)

## Strategy

**Parse at the boundary (P14).** Add `model_validator(mode="before")` to each affected model that reads the new `_dollars`/`_fp` fields and populates the existing int fields. All downstream code continues working with integer cents and integer contract counts.

This is the established Talos pattern — see `Trade._normalize` (handles `taker_side` → `side`, float price → cents) and `OrderBook._coerce_levels` (handles raw arrays → OrderBookLevel).

Conversion logic is trivial and inlined per model (P4 — no shared utility module):
- `_dollars` → `round(float(val) * 100)` → int cents
- `_fp` → `int(float(val))` → int contracts

## Alternatives Considered

1. **Change internal representation to dollars/fp strings** — Would touch every module in the codebase. Violates P4 (subtract before you add) and P6 (boring and proven). Integer arithmetic is safer for financial calculations.

2. **Shared conversion utility module** — The conversions are one-liners. A shared module adds import complexity for no benefit. Each model is self-contained per existing patterns.

3. **Dual-field models (keep old + add new)** — Over-engineered. The old fields are gone from the API. The model validator approach handles both old (tests) and new (production) formats transparently.

**Chosen: Option 1 (model validators)** — minimal blast radius, follows existing patterns, all downstream code unchanged.

## Applicable Skills

- `safety-audit` — after Phase 4 (request payloads touch order placement)
- `test-runner` — after every phase

## Field Mapping Reference

### Dollars → Cents (prices, fees, costs)
| Old Field | New Field | Appears On |
|-----------|-----------|------------|
| `yes_bid` | `yes_bid_dollars` | Market, TickerMessage |
| `yes_ask` | `yes_ask_dollars` | Market, TickerMessage |
| `no_bid` | `no_bid_dollars` | Market, TickerMessage |
| `no_ask` | `no_ask_dollars` | Market, TickerMessage |
| `last_price` | `last_price_dollars` | Market, TickerMessage |
| `yes_price` | `yes_price_dollars` | Order, Fill, Trade |
| `no_price` | `no_price_dollars` | Order, Fill, Trade |
| `taker_fees` | `taker_fees_dollars` | Order |
| `maker_fees` | `maker_fees_dollars` | Order |
| `total_traded` | `total_traded_dollars` | Position |
| `market_exposure` | `market_exposure_dollars` | Position |

### FP → Int (counts, quantities, positions)
| Old Field | New Field | Appears On |
|-----------|-----------|------------|
| `fill_count` | `fill_count_fp` | Order |
| `remaining_count` | `remaining_count_fp` | Order |
| `initial_count` | `initial_count_fp` | Order |
| `volume` | `volume_fp` | Market, TickerMessage |
| `open_interest` | `open_interest_fp` | Market |
| `count` | `count_fp` | Fill, Trade, TradeMessage |
| `position` | `position_fp` | Position |

### Structural Changes
| Component | Old Format | New Format |
|-----------|-----------|------------|
| REST orderbook response key | `data["orderbook"]` | `data["orderbook"]` or `data["orderbook_fp"]` |
| REST orderbook levels | `[[cents_int, qty_int], ...]` | `[["dollars_str", "fp_str"], ...]` |
| WS snapshot fields | `yes`, `no` | `yes_dollars_fp`, `no_dollars_fp` |
| WS snapshot levels | `[[cents_int, qty_int], ...]` | `[["dollars_str", "fp_str"], ...]` |
| WS delta fields | `price` (int), `delta` (int) | `price_dollars` (str), `delta_fp` (str) |
| REST order requests | `count` (int), `no_price` (int) | `count_fp` (str), `no_price_dollars` (str) |

## Phases

1. [[plans/02-kalshi-fp-migration/phase-1-market-models]] — Market, Trade, OrderBook validators
2. [[plans/02-kalshi-fp-migration/phase-2-portfolio-models]] — Order, Fill, Position validators
3. [[plans/02-kalshi-fp-migration/phase-3-ws-models]] — OrderBookSnapshot, OrderBookDelta, TickerMessage, TradeMessage
4. [[plans/02-kalshi-fp-migration/phase-4-rest-requests]] — Request payloads + response key handling
5. [[plans/02-kalshi-fp-migration/phase-5-test-migration]] — Update test fixtures, verify full suite

## Verification

```bash
.venv/Scripts/python -m pytest tests/ -x     # all tests pass
.venv/Scripts/python -m ruff check src/       # no lint errors
.venv/Scripts/python -m pyright               # type check (ignore known false positives)
```

Runtime: launch Talos against Kalshi demo/prod and verify:
- Prices display correctly in the opportunities table
- Orders display with correct fill counts and prices
- Positions show accurate contract counts
- Orderbook updates flow via WebSocket
- Order placement succeeds with new field format
