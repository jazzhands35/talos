# Inventory-Skewed Edge

Research note on adapting market-making inventory management concepts to cross-event NO+NO arbitrage. Source: verax (@journoverax) article "How to Read the Order Book Like a Quant" (March 19, 2026).

## Core Idea

Avellaneda-Stoikov market makers adjust their reservation price based on inventory — when long, they lower bids (less eager to accumulate). The **principle** generalizes beyond market-making: **when you're already exposed, demand more edge to add more exposure.**

Talos currently uses a flat `edge_threshold_cents = 1.0` in `automation_config.py`. The position ledger is threaded through to the proposer but only used for binary gates (block/allow), never for scaling the edge requirement.

## Inventory Dimensions for Arb

| Dimension | Risk | Signal Source |
|-----------|------|---------------|
| Event concentration | Multiple legs on same game — one upset kills all | `position_ledger.filled_count()` per ticker |
| League concentration | Correlated outcomes same sport/night | Count open positions grouped by series ticker |
| Capital utilization | Opportunity cost of marginal capital | Total deployed / bankroll |
| Position delta | Imbalanced fills between sides | `position_ledger.current_delta()` |
| Time concentration | Many positions resolving same window | `expected_expiration_time` clustering |

## Proposed Formula

```
required_edge = base_edge * (1 + inventory_penalty)

inventory_penalty =
    alpha * event_concentration(game_a, game_b)
  + beta  * league_concentration(sport)
  + gamma * capital_utilization
  + delta * position_delta
```

### Example

8 NHL arbs open, new NHL pair at 1.5c fee-adjusted edge:

```
base_edge             = 1.0c
event_concentration   = 0.0   (new game)
league_concentration  = 0.4   (0.05 * 8 NHL arbs)
capital_utilization   = 0.2   (0.29 * 70% deployed)
position_delta        = 0.0   (balanced)

required_edge = 1.0 * (1 + 0.6) = 1.6c
1.5c < 1.6c -> SKIP
```

## Where to Inject

Architecture already supports this — ledger flows through proposer:

```
scanner._evaluate_pair()         -> Opportunity(fee_edge=...)
opportunity_proposer.evaluate()  -> Gate 1: fee_edge < threshold -> skip
                                    (currently flat, needs dynamic threshold)
position_ledger                  -> provides all inventory signals
```

Key files:
- `automation_config.py` — add penalty coefficients (alpha, beta, gamma, delta)
- `opportunity_proposer.py:70` — Gate 1 becomes dynamic: `threshold * (1 + penalty)`
- `position_ledger.py` — already has `filled_count()`, `current_delta()`, `total_committed()`
- Need to add: league-level aggregation, capital utilization tracking

## Calibration Requirements

- Historical loss correlation data across sports/nights (from `kalshi_history.db`)
- Per-league upset frequency and clustering analysis
- Backtest: would inventory skew have prevented actual loss clusters?
- See [[../plans/04-ml-integration/overview]] for ML history data pipeline

## Related Concepts from the Article

### Potentially Useful

- **VPIN (Volume-synchronized Probability of Informed Trading)** — real-time toxicity metric. VPIN > 0.7 = informed money is active. Could filter markets where someone knows the game outcome. Requires trade-level data from WS trade channel.
  - Source: Easley et al. (2012) "Flow Toxicity and Liquidity in a High-frequency World" Review of Financial Studies

- **Hawkes process branching ratio** — when alpha/beta jumps from 0.6 to 0.85, order flow is "hot" (trades causing more trades, not new information). Could signal volatile markets to avoid.
  - Source: Hawkes (1971) "Spectra of some self-exciting and mutually exciting point processes" Biometrika

### Not Applicable at Current Scale

- **Kyle's Lambda** — price impact per dollar. At $20 unit size, our impact is zero. Only relevant if unit size scales significantly.
  - Source: Kyle (1985) "Continuous Auctions and Insider Trading" Econometrica

- **Almgren-Chriss optimal execution** — splitting large orders to minimize slippage. Irrelevant at $20 units.
  - Source: Almgren & Chriss (2001) "Optimal execution of portfolio transactions" Journal of Risk

- **Avellaneda-Stoikov quoting** — dynamic bid/ask around inventory-adjusted fair price. We take liquidity, don't provide it.
  - Source: Avellaneda & Stoikov (2008) "High-frequency trading in a limit order book" Quantitative Finance

## Industry Context (March 2026)

- 14/20 most profitable Polymarket wallets are bots (Finance Magnates)
- 30%+ of Polymarket wallets use AI agents (LayerHub)
- $40M in arb profits extracted from Polymarket Apr 2024-Apr 2025 (IMDEA research)
- Only 7-13% of human traders achieve positive returns
- Edge decay accelerating: "More bots chase the same edge. Spreads tighten. Latency becomes decisive."
- Kalshi raised $1B at $22B valuation (March 19, 2026) — sports = 90% of volume
- Arizona filed first-ever criminal charges against Kalshi (March 17, 2026) — 20-count misdemeanor

## Reference Links

- [Prediction Markets Are Turning Into a Bot Playground — Finance Magnates](https://www.financemagnates.com/trending/prediction-markets-are-turning-into-a-bot-playground/)
- [AI Agents Are Quietly Rewriting Prediction Market Trading — CoinDesk](https://www.coindesk.com/tech/2026/03/15/ai-agents-are-quietly-rewriting-prediction-market-trading/)
- [How AI Is Helping Retail Traders Exploit Prediction Market Glitches — CoinDesk](https://www.coindesk.com/markets/2026/02/21/how-ai-is-helping-retail-traders-exploit-prediction-market-glitches-to-make-easy-money/)
- [Arizona AG Files Criminal Charges Against Kalshi — NPR](https://www.npr.org/2026/03/17/nx-s1-5751165/kalshi-criminal-charges-arizona)
- [Kalshi Raises $1B at $22B Valuation — Bloomberg](https://www.bloomberg.com/news/articles/2026-03-19/kalshi-gets-1-billion-in-new-funding-at-22-billion-valuation)
- verax article: [x.com/journoverax/status/2034630639664652465](https://x.com/journoverax/status/2034630639664652465)
