# Implementation Notes

Back to [[plans/03-api-integration/overview]]

## Fee Rounding Accumulator

Kalshi uses a fee accumulator per order that tracks sub-cent rounding overpayment across fills. When accumulated rounding exceeds $0.01, a whole-cent rebate is issued. This explains why computed `quadratic_fee()` may differ from actual `maker_fees` by a penny — it's not a bug.

After Phase 2 (using `maker_fill_cost_dollars` from orders), we use Kalshi's actual numbers instead of computing fees. The rounding detail explains *why* computed and actual fees sometimes disagree.

## ESPN API Integration (Future)

The biggest source of loss with Talos is trading events that are already active — live games where prices move far too quickly, causing one-sided exposure. Two complementary data sources could mitigate this:

1. **`min_close_ts` filter** (Phase 7) — avoids events closing soon at the API level
2. **ESPN API** (future plan) — provides proposed game start times and whether a game is currently live

Combining both would allow Talos to filter out both "closing soon" events AND "game already in progress" events. This is a separate plan to be created when ready.

## WS Channel vs Message Type Names

Kalshi uses different names for channels vs the message types within them:

| Channel name (subscribe) | Message type field | Notes |
|--------------------------|-------------------|-------|
| `orderbook_delta` | `orderbook_snapshot`, `orderbook_delta` | Two types on one channel |
| `ticker` | `ticker` | Same name |
| `trade` | `trade` | Same name |
| `fill` | `fill` | Same name |
| `user_orders` | `user_order` | **Plural channel, singular message!** |
| `market_positions` | `market_position` | **Plural channel, singular message!** |
| `market_lifecycle_v2` | `market_lifecycle_v2`, `event_lifecycle` | Two types on one channel |
| `order_group_updates` | `order_group_updates` | Same name |
| `communications` | `rfq_created`, `rfq_deleted`, `quote_created`, `quote_accepted`, `quote_executed` | Five types on one channel |

This is critical for the `_MESSAGE_MODELS` registry in `ws_client.py` — the key must be the message **type** string, not the channel name.

## Future UI Surfacing Candidates

Data that is fetched and cached but not yet displayed in the TUI. Wire these up when they become useful:

- **Volume/OI per market** — available via `engine.get_ticker_data(ticker)` (TickerFeed WS cache). Could add Vol-A/Vol-B columns to OpportunitiesTable or show on BidScreen. Useful as a liquidity signal complementing CPM.
