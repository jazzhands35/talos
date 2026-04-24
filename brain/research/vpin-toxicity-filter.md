# VPIN Toxicity Filter

Research note on using Volume-Synchronized Probability of Informed Trading (VPIN) as an adverse selection filter for Talos arb entry. Source: Easley, Lopez de Prado & O'Hara (2012); verax article (March 2026).

## Core Idea

VPIN measures order flow toxicity — the probability that the counterparty to your trade has private information. In sports prediction markets, "informed" means someone who knows or strongly suspects the game outcome. High VPIN on a market we're about to bid into signals we'd be buying a contract from someone who knows it's mispriced.

**Use case for Talos:** Before entering an arb, check VPIN on both legs. If either market has VPIN > threshold, skip — the "edge" may be a trap where one side is being dumped by an informed trader.

## The VPIN Algorithm

### Step 1: Time Bars
- Aggregate individual trades into fixed-interval bars (e.g., 1-minute bars)
- Each bar has: open price, close price, total volume

### Step 2: Bulk Volume Classification (BVC)
- Classify each bar's volume as buy-initiated or sell-initiated
- Uses the CDF of price change normalized by volatility:
  ```
  V_buy(bar) = volume * CDF(delta_price / sigma)
  V_sell(bar) = volume * (1 - CDF(delta_price / sigma))
  ```
- Where `CDF` = standard normal cumulative distribution function
- `delta_price` = close - open of the bar
- `sigma` = rolling std dev of price changes
- If price unchanged: 50/50 split (uninformative bar)

### Step 3: Volume Buckets
- Divide average daily volume by bucket count (typically n=50)
- `V_bucket = avg_daily_volume / n`
- Fill buckets sequentially from bars — a single bar may span multiple buckets
- Each bucket represents one "information arrival" unit

### Step 4: Order Imbalance per Bucket
```
OI(tau) = |V_buy(tau) - V_sell(tau)|
```
Where tau indexes volume buckets, not time.

### Step 5: VPIN Metric
```
VPIN = (1/n) * sum(OI(tau)) / V_bucket
     = sum(|V_buy - V_sell|) / (n * V_bucket)
```
Rolling over the last n buckets. Range: [0, 1].

### Interpretation
- VPIN near 0: balanced flow, no informed trading signal
- VPIN near 1: extreme imbalance, strong informed trading signal
- **Threshold:** Traditional literature uses CDF > 0.9 (i.e., VPIN higher than 90% of historical values)
- verax article suggests VPIN > 0.7 as danger zone for market makers

## Adaptation for Kalshi Sports Markets

### Key Differences from Equity Markets
| Factor | Equities | Kalshi Sports |
|--------|----------|---------------|
| Volume | Millions/day | Hundreds-thousands/day per market |
| Price range | Continuous | 1-99 cents |
| Information events | Earnings, news | Game start, score changes, injuries |
| Resolution | Continuous | Binary (YES=100, NO=0) |
| Bucket count | 50 typical | Need far fewer (5-10?) given low volume |

### Practical Concerns
- **Low volume problem:** Many Kalshi sports markets trade <500 contracts/day. Standard VPIN with 50 buckets = 10 contracts/bucket — too noisy. Need smaller bucket count or longer lookback.
- **Binary payoff distortion:** As resolution approaches, informed traders have near-certain information. VPIN will spike naturally near game end — not useful as a filter for pre-game arbs.
- **Pre-game vs. in-game:** VPIN is most useful in the pre-game period (1-4h before start). During the game, price moves reflect public score information, not private.

### What VPIN Would Catch
- Insider knowledge of injuries/lineup changes not yet public
- Sharp money from sophisticated bettors who've already priced an edge
- One side being dumped (e.g., someone selling YES at below-market because they know the underdog wins)

### What VPIN Would NOT Catch
- Structural arb mispricings (our actual edge — these aren't information-driven)
- Slow adverse selection over hours (VPIN is designed for minutes-to-hours timescale)
- Cross-event correlation risk (that's [[inventory-skewed-edge]]'s domain)

## Current Infrastructure

### What Exists
- `TradeMessage` model in `models/ws.py:126-155` — fully defined, handles both old and new Kalshi field naming
- `TradeMessage` registered in `ws_client.py:36` `_MESSAGE_MODELS` for dispatch
- `CPMTracker` in `cpm.py:60-96` — deduplicates by `trade_id`, computes contracts-per-minute. Could be extended for VPIN.
- `taker_side` field exists on WS trade messages (per AsyncAPI spec)

### What's Missing
- **No `trades` channel subscription** — WS client subscribes to `orderbook_delta`, `ticker`, `user_orders`, `fill`, `market_positions`, `market_lifecycle_v2` but NOT `trades`
- **No TradeFeed class** — would need to follow the pattern of `TickerFeed`, `PortfolioFeed`, etc.
- **No VPIN computation module** — would be a new state machine similar to `CPMTracker`
- **No integration with proposer** — VPIN would need to flow into `opportunity_proposer.evaluate()` as a gate

### Implementation Sketch
```
1. New TradeFeed class (follows existing feed pattern)
   - Subscribes to "trades" channel per market
   - Routes TradeMessage to VPINTracker

2. VPINTracker (new module, similar to CPMTracker)
   - Accumulates trades per market
   - Classifies buy/sell via BVC (price change vs. mid)
   - Fills volume buckets
   - Computes rolling VPIN per market
   - Exposes: vpin(ticker) -> float | None

3. OpportunityProposer gate (new Gate ~8)
   - Before accepting arb: check vpin(ticker_a) and vpin(ticker_b)
   - If either > threshold: skip with reason "high VPIN"
   - Threshold configurable in AutomationConfig
```

### Data Requirements
- Need trades channel subscribed for every market we're scanning
- At current scale (~50-200 markets), this adds significant WS message volume
- Could limit to only markets with active arb pairs to reduce load

## Open Questions

1. **Is Kalshi trade volume sufficient for meaningful VPIN?** Many markets have <100 trades before game start. Need empirical data.
2. **What bucket parameters work for binary sports markets?** Equity defaults (50 buckets) won't work. Need calibration.
3. **False positive rate vs. edge loss tradeoff?** Skipping arbs due to high VPIN means missed edge. How often would VPIN have correctly predicted a losing leg?
4. **Correlation with our actual losses?** Need to backtest: did our historical losing legs have higher pre-entry trade imbalance than winning legs?

## Priority Assessment

**Lower priority than [[inventory-skewed-edge]]** because:
- Inventory skew uses data we already have (position ledger, no new WS subscriptions)
- VPIN requires new infrastructure (TradeFeed, VPINTracker, trades channel)
- Uncertain whether Kalshi sports volume is sufficient for meaningful VPIN
- Inventory skew addresses the more common risk (concentration) vs. the rarer risk (informed counterparty)

**Worth prototyping after inventory-skewed-edge** if we can:
1. Subscribe to trades channel for a few active markets
2. Log trade flow for a week
3. Compute offline VPIN and correlate with actual arb outcomes

## References

### Academic
- Easley, D., Lopez de Prado, M. & O'Hara, M. (2012). "Flow Toxicity and Liquidity in a High-frequency World." Review of Financial Studies
- Easley, D., Lopez de Prado, M. & O'Hara, M. (2011). "The Exchange of Flow Toxicity." Journal of Trading
- Abad, D. & Yague, J. (2012). ["From PIN to VPIN: An Introduction to Order Flow Toxicity."](https://www.quantresearch.org/From%20PIN%20to%20VPIN.pdf) Spanish Review of Financial Economics
- [Parameter Analysis of VPIN](https://escholarship.org/content/qt2sr9m6gk/qt2sr9m6gk_noSplash_31c899ac57bd2a510b3277cbbacb36b5.pdf) — UC eScholarship

### Implementations
- [VPIN Python/R implementation](https://github.com/yt-feng/VPIN) — GitHub reference implementation
- [VPIN BVC calculation gist](https://gist.github.com/ProbablePattern/c46e4fb12bf758b99b03) — R implementation with BVC

### Context
- [Prediction Markets Are Turning Into a Bot Playground](https://www.financemagnates.com/trending/prediction-markets-are-turning-into-a-bot-playground/) — Finance Magnates, March 2026
- verax article: [x.com/journoverax/status/2034630639664652465](https://x.com/journoverax/status/2034630639664652465)
- See also: [[inventory-skewed-edge]] for the complementary portfolio-level risk approach
