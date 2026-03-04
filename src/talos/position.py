"""Position computation — pure functions for arb pair P&L tracking."""

from __future__ import annotations

from talos.models.order import Order
from talos.models.position import EventPositionSummary, LegSummary
from talos.models.strategy import ArbPair


def compute_event_positions(
    orders: list[Order],
    pairs: list[ArbPair],
) -> list[EventPositionSummary]:
    """Derive per-event position summaries from orders and arb pairs.

    Pure function — no I/O.  Only considers ``buy no`` orders whose
    ticker matches a registered pair leg.
    """
    # Build ticker → pair lookup
    ticker_to_pair: dict[str, ArbPair] = {}
    for pair in pairs:
        ticker_to_pair[pair.ticker_a] = pair
        ticker_to_pair[pair.ticker_b] = pair

    # Accumulate per-pair, per-leg stats
    # key = event_ticker, value = {ticker: (filled, resting, max_no_price)}
    accum: dict[str, dict[str, list[int]]] = {}

    for order in orders:
        if order.side != "no" or order.action != "buy":
            continue
        pair = ticker_to_pair.get(order.ticker)
        if pair is None:
            continue
        evt = pair.event_ticker
        accum.setdefault(evt, {})
        entry = accum[evt].setdefault(order.ticker, [0, 0, 0])
        entry[0] += order.fill_count
        entry[1] += order.remaining_count
        # Track worst-case (highest) NO price for exposure calc
        if order.no_price > entry[2]:
            entry[2] = order.no_price

    # Build summaries
    summaries: list[EventPositionSummary] = []
    for pair in pairs:
        evt = pair.event_ticker
        leg_data = accum.get(evt)
        if leg_data is None:
            continue

        data_a = leg_data.get(pair.ticker_a, [0, 0, 0])
        data_b = leg_data.get(pair.ticker_b, [0, 0, 0])

        filled_a, resting_a, price_a = data_a
        filled_b, resting_b, price_b = data_b

        # Skip pairs with zero activity
        if filled_a + resting_a + filled_b + resting_b == 0:
            continue

        matched = min(filled_a, filled_b)
        locked_profit = matched * (100 - price_a - price_b)
        unmatched_a = filled_a - matched
        unmatched_b = filled_b - matched
        exposure = unmatched_a * price_a + unmatched_b * price_b

        summaries.append(
            EventPositionSummary(
                event_ticker=evt,
                leg_a=LegSummary(
                    ticker=pair.ticker_a,
                    no_price=price_a,
                    filled_count=filled_a,
                    resting_count=resting_a,
                ),
                leg_b=LegSummary(
                    ticker=pair.ticker_b,
                    no_price=price_b,
                    filled_count=filled_b,
                    resting_count=resting_b,
                ),
                matched_pairs=matched,
                locked_profit_cents=locked_profit,
                unmatched_a=unmatched_a,
                unmatched_b=unmatched_b,
                exposure_cents=exposure,
            )
        )

    return summaries
