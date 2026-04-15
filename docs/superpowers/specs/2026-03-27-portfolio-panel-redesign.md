# Portfolio Panel Redesign

## Problem

The current portfolio panel has several issues:
- P&L numbers (Today/Yesterday/7d) have never been accurate — settlement aggregation is unreliable
- Terms overlap and confuse: "Locked In", "Exposure", and "Invested" blur together
- Missing useful operational info: no visibility into how many events have bids vs positions vs nothing
- "Tracked: 316/1283" doesn't tell you what state those events are in

## Design

Replace the current panel with two clear sections: **Account** (money) and **Coverage** (events).

### Account Section

```
Cash:       $4,584.35
Matched:    47 units
Partial:    12 events
Locked In:  $15.44
Exposure:   $131.48
```

| Field | Definition | Source |
|-------|-----------|--------|
| **Cash** | Kalshi account balance | `GET /portfolio/balance` → `balance` field. Updated by `update_balance()` on its own 10s poll (kept as-is). |
| **Matched** | Count of completed arb units across all events. A "matched unit" = `min(filled_a, filled_b) // unit_size`. E.g., 20 YES + 20 NO at unit_size=20 = 1 matched unit. | Summed from `EventPositionSummary.matched_pairs // unit_size` per event. Requires `unit_size` — add to `EventPositionSummary` from the ledger. |
| **Partial** | Count of **events** with incomplete arb work — at least one side has fills but the event is not fully matched at a unit boundary. Includes "one side filled" and "both sides partially filled" cases. | Per event: `1` if `filled_a + filled_b > 0` and `not (matched == filled_a == filled_b and matched % unit_size == 0)`. Sum across events. |
| **Locked In** | Guaranteed profit from matched pairs regardless of outcome. | Sum of existing `EventPositionSummary.locked_profit_cents` across events (already computed). |
| **Exposure** | Total cost of **unmatched contracts only** — money at risk if the unmatched side loses. NOT pair-based (that would overestimate). | Sum of existing `EventPositionSummary.exposure_cents` across events (already computed via integer `_prorate`). No new computation needed. |

### Coverage Section

```
Events:       316
w/ Positions:  43
Bidding:       28
Unentered:    245
```

| Field | Definition | Source |
|-------|-----------|--------|
| **Events** | Total events in the table (each NO+NO or YES+NO pair = 1 event) | `len(scanner.pairs)` (canonical source for monitored events) |
| **w/ Positions** | Events with any filled contracts (matched or unmatched) | Count `EventPositionSummary` entries where `filled_a + filled_b > 0` |
| **Bidding** | Events with resting orders but no fills yet | Count `EventPositionSummary` entries where `resting > 0` and `filled == 0` on both legs |
| **Unentered** | Events with no position and no bids | `Events - w/ Positions - Bidding` |

**Note:** Categories are mutually exclusive — every event is in exactly one of {w/ Positions, Bidding, Unentered}. An event with BOTH fills and resting orders counts as "w/ Positions".

### Removed

- **P&L section** (Today/Yesterday/7d) — settlement aggregation was never reliable. P&L tracking lives in the History screen.
- **"Invested"** — confusing overlap with Exposure. The existing `exposure_cents` (unmatched contract cost) is clearer and more actionable.
- **"Tracked: X/Y"** — replaced by the 4-way coverage breakdown.

## Implementation

### Files Changed

1. **`src/talos/ui/widgets.py`** — `PortfolioPanel` class:
   - Replace `render()` with new layout (two sections: Account + Coverage)
   - Add `update_account(matched, partial, locked, exposure)` replacing `update_portfolio_summary()`
   - Add `update_coverage(events, with_positions, bidding, unentered)` replacing `update_pnl()` and `update_tracked_counts()`
   - Keep `update_balance()` as-is (separate 10s poll)
   - Remove `_pnl_*`, `_invested_*`, `_tracked`, `_with_positions` fields

2. **`src/talos/ui/app.py`** — `refresh_opportunities()`:
   - Aggregate Matched/Partial/Exposure from `position_summaries` (data already exists in `EventPositionSummary`)
   - Compute coverage counts (w/ Positions, Bidding, Unentered) from `position_summaries` + `scanner.pairs`
   - Call `panel.update_account()` and `panel.update_coverage()`
   - In `_poll_settlements()`: remove only the `aggregate_settlements()` + `panel.update_pnl()` block (lines ~457-468). Keep settlement cache population for the History screen.
   - Remove `_poll_settlements()` immediate call on mount (line ~137) — no longer needed for panel display

3. **`src/talos/models/position.py`** — `EventPositionSummary`:
   - Add `unit_size: int` field so `app.py` can compute `matched_pairs // unit_size`

4. **Tests**:
   - `tests/test_ui.py` — update panel assertions for new layout
   - `tests/test_proposal_panel.py` — update any panel method calls
   - `tests/test_portfolio_render.py` — rewrite: current tests check `update_portfolio_summary`, "Today:" text, etc. All will break.

### Computation Details (in `app.py:refresh_opportunities`)

```python
# Already in the existing summation loop over position_summaries:
total_matched_units = 0
total_partial_events = 0
total_locked = 0
total_exposure = 0
with_positions = 0
bidding = 0

for s in summaries:
    filled = s.leg_a.filled_count + s.leg_b.filled_count
    resting = s.leg_a.resting_count + s.leg_b.resting_count
    matched = s.matched_pairs

    total_matched_units += matched // s.unit_size
    total_locked += s.locked_profit_cents
    total_exposure += s.exposure_cents

    if filled > 0:
        with_positions += 1
        # Partial = has fills but not cleanly matched at unit boundary
        if not (matched > 0 and matched % s.unit_size == 0
                and s.leg_a.filled_count == s.leg_b.filled_count):
            total_partial_events += 1
    elif resting > 0:
        bidding += 1

total_events = len(self._scanner.pairs) if self._scanner else 0
unentered = total_events - with_positions - bidding
```

### Notes

- `engine.py` requires no changes — all needed data already exists in `EventPositionSummary` and `PositionLedger`.
- `update_balance()` is unchanged — Cash continues to update on its own 10s poll independently.
- If `unit_size` changes mid-session, existing ledgers keep the old value. Matched/Partial counts reflect each event's own `unit_size` via the ledger, which is correct.
- The ArgoApp subclass has its own `ArgoPortfolioPanel` and does not call the parent panel methods — this redesign does not affect Argo.
