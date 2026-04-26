# Kalshi Net Position and P&L — How It Works

This document explains how to track positions on Kalshi, compute net exposure, calculate P&L under each outcome scenario, and derive effective American odds for a combined position. It covers single-side positions, two-sided (hedged) positions, and the maker fee's effect on profit.

## Kalshi's Contract Model (Quick Recap)

- Every Kalshi market is a binary contract: YES or NO.
- Prices are in **cents** (1–99). YES price + NO price = 100.
- Buying YES at 60¢ costs $0.60 per contract. If YES wins, you get $1.00 (profit = $0.40). If NO wins, you lose your $0.60.
- Buying NO at 40¢ on the same market is the mirror: costs $0.40, profit $0.60 if NO wins.
- You can hold **both YES and NO** contracts on the same market simultaneously (common in spread/total markets where you're building a position from both sides).

---

## Part 1: Raw Position Data from Kalshi

### Positions Endpoint

```
GET /trade-api/v2/portfolio/positions
```

Returns one entry per market you have exposure in. Key fields (post March 12, 2026 — legacy integer fields removed):

| Field | API Field Name | Format | Description |
|-------|---------------|--------|-------------|
| Ticker | `ticker` | string | Market contract identifier |
| Position | `position_fp` | string (e.g., `"10"`) | **Signed**: positive = net YES, negative = net NO |
| Total Traded | `total_traded_dollars` | string (e.g., `"0.25"`) | Total dollars traded |
| Market Exposure | `market_exposure_dollars` | string (e.g., `"6.50"`) | Total dollars at risk |
| Resting Count | `resting_orders_count` | integer (e.g., `3`) | Number of resting orders (count of orders, not contracts) |

> **Note:** Legacy integer fields (`position`, `total_traded`, `market_exposure`) were removed March 12, 2026 in favor of `_fp` / `_dollars` string variants. `resting_orders_count` was NOT removed — it is still returned as an integer (count of orders, which is inherently whole). Verified against production `/portfolio/positions` 2026-04-25: response carries exactly `resting_orders_count` (int), with no `_fp` variant. Talos's `Position` model converts `_fp`/`_dollars` strings to int cents/int counts via `_migrate_fp` validator.

**Position sign convention**: `+100` means you hold 100 YES contracts. `-50` means you hold 50 NO contracts.

**Inferring side when not explicit**: If `position > 0` → YES. If `position < 0` → NO.

### Orders Endpoint (For Cost Basis)

```
GET /trade-api/v2/portfolio/orders
```

Returns all orders (open + filled). Filled orders are used to reconstruct **price buckets** — the specific prices at which you accumulated your position. This gives you accurate cost basis when you've bought at multiple price levels.

Post March 12, 2026: order fields use `_dollars`/`_fp` strings — e.g., `yes_price_dollars` (string, `"0.65"`), `no_price_dollars`, `fill_count_fp` (string, `"10"`), `remaining_count_fp`, `maker_fees_dollars`. Legacy integer fields (`yes_price`, `no_price`, `fill_count`, etc.) have been removed.

### Price Reconstruction

If the positions endpoint doesn't provide `avg_price` directly, you can derive it from exposure:

```python
avg_price_cents = round((market_exposure_dollars * 100) / abs(position))
```

YES and NO prices are complementary: `no_price = 100 - yes_price`.

---

## Part 2: Building Position Buckets

A single position number (+100 YES) doesn't tell you what you paid. If you bought 60 contracts at 55¢ and 40 contracts at 58¢, your cost basis is different from buying all 100 at 56¢.

### From Filled Orders

Group filled orders by (market_ticker, side) and sum quantities at each price:

```python
# Example bucket structure
buckets_by_side = {
    "yes": [
        {"price_cents": 55, "qty": 60},
        {"price_cents": 58, "qty": 40},
    ],
    "no": [
        {"price_cents": 42, "qty": 20},
    ],
}
```

### Reconciling with Net Position

The positions endpoint is **authoritative** for net quantity. Order history may be incomplete (old orders drop off). After building buckets from orders, reconcile:

```python
yes_qty = sum(b["qty"] for b in yes_buckets)   # e.g., 100
no_qty  = sum(b["qty"] for b in no_buckets)     # e.g., 20
net_from_buckets = yes_qty - no_qty              # e.g., 80

target_net = position_from_api                   # e.g., 85 (authoritative)
delta = target_net - net_from_buckets            # e.g., +5

if delta > 0:
    # Missing some YES contracts — add at best-known price
    yes_buckets.append({"price_cents": avg_price, "qty": delta})
elif delta < 0:
    # Missing some NO contracts
    no_buckets.append({"price_cents": 100 - avg_price, "qty": abs(delta)})
```

This ensures bucket math always agrees with the authoritative position count.

---

## Part 3: Single-Side Position Math

For a position on one side only (e.g., you only hold YES contracts):

### Stake (Cost / Risk)

```python
stake = sum(qty * (price_cents / 100.0) for each bucket)
```

**Example**: 60 contracts at 55¢ + 40 at 58¢:
```
stake = 60 × 0.55 + 40 × 0.58 = 33.00 + 23.20 = $56.20
```

If you have `market_exposure_dollars` from the API, use that directly — it's more accurate than reconstructing from buckets.

### Win (Profit If Correct)

```python
total_qty = sum(qty for each bucket)
win = total_qty - stake
# Equivalently:
win = sum(qty * (1.0 - price_cents / 100.0) for each bucket)
```

**Example**: 100 contracts, $56.20 cost:
```
win = 100 × $1.00 - $56.20 = $43.80
```

If your side wins, you receive `total_qty` dollars and your profit is `win`. If your side loses, you lose `stake`.

### Average Price

```python
avg_price_cents = (stake / total_qty) * 100
```

**Example**: `(56.20 / 100) × 100 = 56.2¢`

### American Odds for Position

```python
def american_from_win_risk(win, risk):
    if win >= risk:
        return (win / risk) * 100.0     # Positive odds
    else:
        return -(risk / win) * 100.0    # Negative odds
```

**Example**: Win=$43.80, Risk=$56.20 → `-(56.20 / 43.80) × 100 = -128.3`

---

## Part 4: Two-Sided Positions (Spread/Total Markets)

In spread and total markets, you often hold **both YES and NO** contracts on the same market. This happens because:
- You're hedging (arbing) both sides
- You accumulated positions on different sides at different times
- The market structure means YES and NO represent opposite outcomes (e.g., Team A covers -7.5 vs Team A doesn't cover -7.5)

### Why You Can't Just Net Them

If you hold 100 YES at 60¢ and 50 NO at 45¢:
- These are **not** offsetting in the simple sense. You paid $60 for YES and $22.50 for NO ($82.50 total).
- If YES wins: you get $100 from YES, lose $22.50 on NO → profit = $100 - $82.50 = $17.50
- If NO wins: you lose $60 on YES, get $50 from NO → profit = $50 - $82.50 = -$32.50

The P&L depends on **which outcome happens**, so you must compute both scenarios.

### Cross-Portfolio P&L Formula

```python
# Aggregate YES and NO separately
yes_qty   = total YES contracts held
yes_stake = total cost of YES contracts (dollars)
no_qty    = total NO contracts held
no_stake  = total cost of NO contracts (dollars)

# Scenario: YES wins
# - You collect yes_qty dollars from YES contracts
# - You lose no_stake dollars (NO contracts worthless)
# - Fee applies to the PROFIT portion of the winning side
pnl_if_yes = fee_adj * (yes_qty - yes_stake) - no_stake

# Scenario: NO wins
# - You collect no_qty dollars from NO contracts
# - You lose yes_stake dollars (YES contracts worthless)
pnl_if_no = fee_adj * (no_qty - no_stake) - yes_stake
```

Where `fee_adj` is the maker fee factor (see Part 5 below).

**The key insight**: `(yes_qty - yes_stake)` is the **profit** on the YES side if YES wins. The fee applies only to this profit, not to the return of your stake. Then you subtract the **total loss** on the other side (`no_stake` is gone).

### Net Direction

```python
net_qty = abs(yes_qty - no_qty)

if yes_qty > no_qty:
    net_direction = "YES"    # You're net long YES
elif no_qty > yes_qty:
    net_direction = "NO"     # You're net long NO
else:
    net_direction = "FLAT"   # Balanced hedge
```

### Effective Odds for the Combined Position

```python
net_profit = max(pnl_if_yes, pnl_if_no)                    # Best case
net_loss   = abs(min(pnl_if_yes, pnl_if_no))               # Worst case
effective_american = american_from_win_risk(net_profit, net_loss)
```

### Worked Example

**Position**: 100 YES at 60¢, 50 NO at 45¢

```
yes_qty   = 100
yes_stake = 100 × 0.60 = $60.00
no_qty    = 50
no_stake  = 50 × 0.45  = $22.50

fee_adj = 0.99125  (half-maker 0.875%)

pnl_if_yes = 0.99125 × (100 - 60) - 22.50
           = 0.99125 × 40 - 22.50
           = 39.65 - 22.50
           = +$17.15

pnl_if_no  = 0.99125 × (50 - 22.50) - 60
           = 0.99125 × 27.50 - 60
           = 27.26 - 60
           = -$32.74

net_qty       = |100 - 50| = 50
net_direction = "YES" (more YES than NO)
net_profit    = max(17.15, -32.74) = $17.15
net_loss      = |-32.74| = $32.74

effective_am = -(32.74 / 17.15) × 100 = -190.8
```

**Interpretation**: You're net 50 YES at effectively -191 American odds. If YES wins, you make $17.15. If NO wins, you lose $32.74.

### Balanced Arb Example

**Position**: 100 YES at 60¢, 100 NO at 40¢ (perfectly balanced)

```
yes_qty=100, yes_stake=$60, no_qty=100, no_stake=$40

pnl_if_yes = 0.99125 × (100 - 60) - 40 = 39.65 - 40 = -$0.35
pnl_if_no  = 0.99125 × (100 - 40) - 60 = 59.475 - 60 = -$0.525
```

Both scenarios lose money — this is expected. When YES + NO prices sum to exactly 100¢, there's no arbitrage edge, and the maker fee creates a small guaranteed loss.

Arb profit only exists when you buy YES + NO for **less than 100¢ combined** (e.g., YES at 58¢ + NO at 39¢ = 97¢ total → 3¢ gross edge minus fees).

---

## Part 5: The Maker Fee

Kalshi charges a maker fee on **profit only** (not on return of your stake). The fee depends on context:

| Fee Level | Rate | `fee_adj` Factor | When It Applies |
|-----------|------|-----------------|-----------------|
| Full maker | 1.75% | `1 - 0.0175 = 0.9825` | Resting limit orders (you provide liquidity) |
| Half maker | 0.875% | `1 - 0.00875 = 0.99125` | Crossing orders, or midpoint approximation |
| No fee | 0% | `1.0` | `fee_type` ∈ {`fee_free`, `no_fee`} |

**Verified 2026-04-26** against live `/portfolio/fills` data: per-contract trade fee tracks the quadratic formula at the 1.75% rate within rounding error. Both `fee_type=quadratic` AND `fee_type=quadratic_with_maker_fees` series charge maker fees at this rate (an earlier brain note claimed plain `quadratic` series paid 0 maker — that is incorrect; the running code in `src/talos/fees.py` correctly applies the rate to both, matching the data).

### Per-fill rounding (not currently modeled by Talos)

Per [docs.kalshi.com/getting_started/fee_rounding.md](https://docs.kalshi.com/getting_started/fee_rounding.md), every fill goes through a rounding pipeline AFTER the trade-fee formula:

1. The trade fee from the formula above is **rounded UP to the nearest $0.0001 (centicent)**.
2. The resulting balance change is **floored toward negative infinity to the nearest $0.01**, generating a "rounding fee" of $0.0000–$0.0099 per fill.
3. A per-order accumulator tracks the rounding overpayment. **Once accumulated rounding exceeds $0.01, a $0.01 rebate is issued** and the accumulator decrements.

The net charge per fill is `trade_fee + rounding_fee − rebate` (always ≥ $0.00).

**Implication for Talos:** the formula in `src/talos/fees.py` gives the trade-fee component only. For very small fills (count ≪ 1) or fills near 0¢/100¢, the rounding fee can dwarf the trade fee — empirically seen up to **80× the formula prediction** on a 0.01-contract fill. For typical multi-contract fills, the deviation is well under $0.01. Talos avoids the issue in practice by using **actual** `maker_fees` / `fee_cost` values from the API for display and ledger accounting (see [[brain/decisions#2026-03-09 — Quadratic fee model and fill-time charging]]); the formula is used only for pre-placement safety gates.

### How the Fee Enters the P&L Formula

The fee reduces only the **profit portion** of the winning side:

```python
profit_before_fee = winning_qty - winning_stake    # What you'd earn without fee
profit_after_fee  = fee_adj * profit_before_fee    # Fee eats into profit
pnl = profit_after_fee - losing_stake              # Minus total loss on other side
```

The fee does NOT reduce your returned stake. If you bought 100 contracts at 60¢ and they win, you get back $60 stake + ($40 profit × fee_adj). The fee only touches the $40.

### Per-Side American Odds with Fee

When displaying the effective odds for just one side of a position:

```python
# Side has: total quantity, total stake, total win (before fee)
am_odds = american_from_win_risk(
    win * fee_adj,    # Fee-adjusted profit
    stake             # Risk unchanged
)
```

---

## Part 6: Moneyline vs Spread/Total — Key Difference

### Moneyline (Game)

In moneyline markets, "Team A wins" and "Team B wins" are separate events. Your YES on Team A and YES on Team B are **independent bets**, not opposite sides of the same contract.

Cross-portfolio P&L for moneyline:
```python
# Side A positions: win=$40, stake=$60 (if Team A wins)
# Side B positions: win=$30, stake=$50 (if Team B wins)

PA = Awin - Bstake    # Profit if A wins: A pays out, B is worthless
PB = Bwin - Astake    # Profit if B wins: B pays out, A is worthless
```

No fee adjustment in this formula — the fee is already baked into the per-side win/stake amounts when they were computed from Kalshi cents.

### Spread / Total

In spread/total markets, YES and NO are the **same contract** in opposite directions. The cross-portfolio formula explicitly applies the maker fee to the profit portion:

```python
pnl_if_yes = fee_adj * (yes_qty - yes_stake) - no_stake
pnl_if_no  = fee_adj * (no_qty - no_stake) - yes_stake
```

### Why the Difference?

- **Moneyline**: Each side's win/stake already passed through `kalshi_cents_to_american()` which embeds the fee. No double-counting.
- **Spread/Total**: Win/stake are computed from raw `qty × (price/100)` — the fee hasn't been applied yet. It's applied at the cross-portfolio level where profit is computed.

---

## Part 7: Display Formatting

### American Odds from Win/Risk

```python
def american_from_win_risk(win, risk):
    """Convert profit/risk to American odds."""
    if risk <= 0 or win <= 0:
        return None
    if win >= risk:
        return (win / risk) * 100.0     # +200 means $200 profit per $100 risked
    else:
        return -(risk / win) * 100.0    # -200 means risk $200 to profit $100
```

### Position Summary Format

For spread/total positions, the heading shows:
```
Net [Direction] × [Net Qty] @ [Effective AM Odds]
```

Examples:
- `Net Warriors -1.5 × 50 @ -191` — net 50 YES, paying -191 juice
- `Net FLAT (100 ea)` — balanced hedge, equal YES and NO
- `Net YES × 200 @ +105` — favorable position, getting +105

---

## Putting It All Together

```
Kalshi Portfolio API
  GET /portfolio/positions  →  net position per market (signed)
  GET /portfolio/orders     →  filled orders for price buckets
          ↓
Build Price Buckets
  Group orders by (ticker, side, price)
  Reconcile bucket qty with authoritative position count
          ↓
Aggregate Per Side
  yes_qty, yes_stake = sum(qty), sum(qty × price/100)
  no_qty,  no_stake  = sum(qty), sum(qty × price/100)
          ↓
Cross-Portfolio P&L (for spread/total)
  pnl_if_yes = fee_adj × (yes_qty - yes_stake) - no_stake
  pnl_if_no  = fee_adj × (no_qty  - no_stake)  - yes_stake
          ↓
Net Summary
  net_qty       = |yes_qty - no_qty|
  net_direction = YES if yes_qty > no_qty, NO otherwise, FLAT if equal
  net_profit    = max(pnl_if_yes, pnl_if_no)
  net_loss      = |min(pnl_if_yes, pnl_if_no)|
  effective_am  = american_from_win_risk(net_profit, net_loss)
```

## Quick Reference

| Calculation | Formula | Notes |
|------------|---------|-------|
| Stake (cost) | `qty × (price_cents / 100)` | Or use `market_exposure_dollars` directly |
| Win (profit) | `qty - stake` | Equivalently `qty × (1 - price/100)` |
| Avg price | `(stake / qty) × 100` | In cents |
| AM from win/risk | `(w/r)×100` if w≥r, else `-(r/w)×100` | Positive = underdog, negative = favorite |
| P&L if YES wins | `fee_adj × (yes_qty - yes_stake) - no_stake` | Fee on profit only |
| P&L if NO wins | `fee_adj × (no_qty - no_stake) - yes_stake` | Fee on profit only |
| Fee adj (full) | `1 - 0.0175 = 0.9825` | Resting limit orders |
| Fee adj (half) | `1 - 0.00875 = 0.99125` | Crossing / midpoint |
| YES + NO = arb? | Only if combined cost < 100¢ per contract | e.g., 58¢ + 39¢ = 97¢ → 3¢ edge |
