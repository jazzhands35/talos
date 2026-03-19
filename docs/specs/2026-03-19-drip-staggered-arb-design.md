# Drip — Staggered 1-Contract Arb Manager

**Date:** 2026-03-19
**Status:** Design approved, not yet implemented

## Problem

Talos places arb bids as single large orders (20 contracts per side). Fills are uncontrollable — one side can fill 18/20 while the other sits at 3/20, creating large imbalanced exposure. The rebalance logic reacts after the fact, but by then the damage (adverse selection, one-sided fills near game start) is already done.

## Solution

A standalone Textual TUI ("Drip") that manages a single arb event by drip-feeding 1-contract bids with fill-reactive balance control. Instead of reacting to imbalance, Drip prevents it by controlling the flow rate on each side — throttling the fast side and letting the slow side catch up, contract by contract.

## Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Balance mode | Strict 1:1 (delta never exceeds 1) | Start simple, graduate to proportional later |
| Target sizing | Manual — operator sets per-event target | Matches current Talos workflow |
| Per-queue cap | 20 resting orders per side | Same worst case as current system, but staggering makes it unlikely |
| Rebalance trigger | Fill-reactive (on each fill event) | Deterministic, easy to reason about. Queue velocity prediction is a future enhancement |
| Jump handling | Cancel front bid, place at back of new price queue | Preserves stagger depth; no burst of amend API calls |
| Initial deployment | Manual kick-off, automatic stagger (alternating A/B) | Human-in-the-loop for event selection, system handles pacing |
| Relationship to Talos | Separate standalone program | Zero risk to Talos. Shares Kalshi client library code as imports |
| UI | Minimal Textual TUI | Same tech stack as Talos, purpose-built for monitoring the drip strategy |

## Architecture

### Project Structure

```
drip/
├── __main__.py          # Entry point, .env loading, app launch
├── config.py            # DripConfig: event ticker, prices, max_resting, stagger_delay
├── controller.py        # DripController: pure state machine (no I/O)
├── side_state.py        # DripSide: per-side order tracking, fill counts
├── ui/
│   ├── app.py           # DripApp: Textual shell, timers, WS wiring
│   ├── theme.py         # Reuse Catppuccin Mocha from Talos
│   └── widgets.py       # Status table, balance indicator, action log
```

### Reused from Talos (import, don't copy)

```python
from talos.auth import KalshiAuth
from talos.config import KalshiConfig
from talos.rest_client import KalshiRESTClient
from talos.models.order import Order, Fill
from talos.models.market import Market, Event
from talos.fees import fee_adjusted_cost, MAKER_FEE_RATE
```

### Key Architectural Principles

- **DripController is a pure state machine** — receives fills and book updates, returns action objects (`PlaceOrder`, `CancelOrder`, `NoOp`). No async, no I/O. The app layer executes actions via REST. Same pattern as Talos's `PositionLedger`.
- **One DripController per event** — architecture supports multiple, but v1 is single-event.
- **WS feed** — single connection, subscribe to both tickers' orderbook + fills channels. Route messages to the controller.
- **REST sync as backup** — every 30s, poll orders and reconcile against controller state. Kalshi is the single source of truth.

## State Machine

### Per-Side State

```
resting_orders: list[OrderInfo]   # ordered by queue position (front first)
filled_count: int                  # total fills received
target_price: int                  # current NO price in cents
deploying: bool                    # still in initial stagger phase
```

### Core Decision Loop (on every fill from WS)

```
ON FILL(side, order_id):
    1. Record the fill, remove order from resting list
    2. Compute delta = abs(filled_A - filled_B)

    IF delta == 0 (balanced):
        → Both sides: replenish up to max_resting (add 1 bid to back of queue)

    IF delta == 1 (this fill just created imbalance):
        → Ahead side: do NOT replenish (let it drain)
        → Behind side: replenish up to max_resting

    IF delta > 1 (imbalance growing — defensive):
        → Ahead side: cancel frontmost resting bid (slow it down)
        → Behind side: replenish up to max_resting
```

### Jump Handling (on orderbook update)

```
ON JUMP(side, new_price):
    → Update target_price for that side
    → Cancel frontmost resting bid at old price
    → Place new 1-contract bid at new price (back of queue)
    → Leave remaining bids alone — they rotate naturally via fills or future jumps
```

### Initial Deployment

```
DEPLOY:
    → Place 1 bid on side A, wait stagger_delay (5s)
    → Place 1 bid on side B, wait stagger_delay
    → Alternate A, B, A, B... until both sides reach max_resting
    → Set deploying = False, hand off to fill-reactive loop
```

Alternating A/B ensures both sides build up together — never more than 1 order ahead during deployment.

## TUI Layout

```
┌─────────────────────────────────────────────────┐
│  DRIP — KXNHLGAME-26MAR19WPGBOS                │
├────────────┬────────────┬───────────────────────┤
│  Side A    │  Side B    │  Balance              │
│  WPG       │  BOS       │  Δ = 0 ✓              │
│  Price: 47¢│  Price: 53¢│                       │
│  Filled: 12│  Filled: 12│  Matched: 12          │
│  Resting: 8│  Resting: 8│  Exposure: $0.00      │
│  Q-front: 3│  Q-front:150│                      │
├────────────┴────────────┴───────────────────────┤
│  Actions                                        │
│  02:15  Fill A #12 — balanced, replenish both   │
│  02:14  Fill B #12 — balanced, replenish both   │
│  02:13  Fill A #12 — ahead +1, hold A           │
│  02:12  Cancel A front — jump 47→48¢, rotate    │
└─────────────────────────────────────────────────┘
```

## Safety & Edge Cases

### Profitability Gate (P18)

Before every order placement: `fee_adjusted_cost(price_a) + fee_adjusted_cost(price_b) < 100`. If the arb goes unprofitable, stop deploying new bids. Existing resting bids stay (at the old profitable price). If a jump makes the new price unprofitable, cancel the front bid without replacing it.

### Mega-Fill Protection

WS fills can arrive in bursts. If 8 fills arrive on side A in one event loop tick, process them sequentially through the state machine. After all 8, delta = 8. The controller cancels front A bids one per evaluation cycle until delta ≤ 1. Hard cap: never more than `max_resting` orders per side, enforced at the placement layer.

### Game Start / Wind-Down

Operator presses a key to trigger wind-down. Wind-down = stop deploying new bids, let existing bids fill or expire. If one side fills during wind-down and creates imbalance, cancel the ahead side's resting bids. Future enhancement: wire in game status for automatic wind-down.

### Process Isolation from Talos

Drip and Talos must NOT manage the same event simultaneously. No technical enforcement initially — operator discipline. The TUI shows a clear warning. Future: shared lock file or Kalshi order group tags.

### Crash Recovery

On startup, query Kalshi for all resting orders on the configured event. Reconstruct DripSide state from what's on the book. Fill counts from positions API. Resume the loop without re-deploying. Drip is stateless on disk — Kalshi is the source of truth.

### Rate Limiting

Respect `Retry-After` headers. If rate-limited during deployment stagger, pause and resume. During fill-reactive rotation, a rate limit delays the action — the controller's desired state is preserved, execution catches up when the limit clears.

## Success Criteria

Measured against events run on the current Talos system:

1. **P&L improvement** (primary) — pilot event's actual P&L is better because fewer one-sided fills eat profit
2. **Balance ratio** — pilot event stays more balanced (lower average delta) than typical Talos events
3. **Fill completion rate** — both sides reach target more reliably; fewer events stuck at 18/3 when game starts
4. **Throughput** — queue never goes cold; always working toward the next fill on both sides

### Failure Signals

- API rate limiting kicks in frequently enough to disrupt the rotation loop
- Mega-fills consistently blow through the stagger, producing the same imbalance as the current system
- The overhead of managing 40 individual orders exceeds the balance improvement benefit

## Explicit Non-Goals (v1)

- No multi-event support — one event at a time
- No automatic event selection — operator picks event and prices
- No queue velocity prediction — fill-reactive only
- No proportional balance mode — strict 1:1 only
- No dynamic max_resting — fixed at 20
- No Talos integration — no shared state, no IPC
- No persistence — no SQLite, no JSONL logs
- No price discovery — operator provides both NO prices

## Future Enhancements (post-pilot)

- **Queue velocity monitoring** — proactive throttling based on CPM and queue depth
- **Proportional balance mode** — allow configurable delta tolerance
- **Dynamic max_resting** — adjust per-queue cap based on volume and queue size
- **Multi-event support** — manage multiple events simultaneously
- **Talos integration** — show drip events in Talos table, shared state
- **Automatic deployment** — scanner-driven event selection with auto-deploy
- **Data collection** — SQLite/JSONL logging for strategy analysis
