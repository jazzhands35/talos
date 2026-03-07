"""Position computation — pure functions for arb pair P&L tracking."""

from __future__ import annotations

from typing import NamedTuple

from talos.fees import fee_adjusted_profit_matched
from talos.models.order import ACTIVE_STATUSES, Order
from talos.models.position import EventPositionSummary, LegSummary
from talos.models.strategy import ArbPair


class _LegAccum(NamedTuple):
    filled: int = 0
    resting: int = 0
    total_fill_cost: int = 0
    max_no_price: int = 0


def _add_order(acc: _LegAccum, order: Order) -> _LegAccum:
    return _LegAccum(
        filled=acc.filled + order.fill_count,
        resting=acc.resting + order.remaining_count,
        total_fill_cost=acc.total_fill_cost + order.no_price * order.fill_count,
        max_no_price=max(acc.max_no_price, order.no_price),
    )


def _proportional_cost(total_cost: int, count: int, total_filled: int) -> int:
    """Allocate cost proportionally via integer division."""
    return total_cost * count // total_filled if total_filled > 0 else 0


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

    accum: dict[str, dict[str, _LegAccum]] = {}
    best_queue: dict[str, dict[str, int | None]] = {}

    for order in orders:
        if order.side != "no" or order.action != "buy":
            continue
        if order.status not in ACTIVE_STATUSES:
            continue
        pair = ticker_to_pair.get(order.ticker)
        if pair is None:
            continue
        evt = pair.event_ticker
        legs = accum.setdefault(evt, {})
        legs[order.ticker] = _add_order(legs.get(order.ticker, _LegAccum()), order)
        # Track best queue position among resting orders
        if order.remaining_count > 0 and order.queue_position and order.queue_position > 0:
            bq = best_queue.setdefault(evt, {})
            prev = bq.get(order.ticker)
            if prev is None or order.queue_position < prev:
                bq[order.ticker] = order.queue_position

    # Build summaries
    _empty = _LegAccum()
    summaries: list[EventPositionSummary] = []
    for pair in pairs:
        evt = pair.event_ticker
        leg_data = accum.get(evt)
        if leg_data is None:
            continue

        a = leg_data.get(pair.ticker_a, _empty)
        b = leg_data.get(pair.ticker_b, _empty)

        if a.filled + a.resting + b.filled + b.resting == 0:
            continue

        matched = min(a.filled, b.filled)
        unmatched_a = a.filled - matched
        unmatched_b = b.filled - matched

        if matched > 0:
            cost_a_matched = _proportional_cost(a.total_fill_cost, matched, a.filled)
            cost_b_matched = _proportional_cost(b.total_fill_cost, matched, b.filled)
            locked_profit = fee_adjusted_profit_matched(
                matched, cost_a_matched, cost_b_matched
            )
        else:
            locked_profit = 0.0

        exposure = _proportional_cost(
            a.total_fill_cost, unmatched_a, a.filled
        ) + _proportional_cost(b.total_fill_cost, unmatched_b, b.filled)

        avg_a = a.total_fill_cost // a.filled if a.filled > 0 else a.max_no_price
        avg_b = b.total_fill_cost // b.filled if b.filled > 0 else b.max_no_price

        evt_queue = best_queue.get(evt, {})
        summaries.append(
            EventPositionSummary(
                event_ticker=evt,
                leg_a=LegSummary(
                    ticker=pair.ticker_a,
                    no_price=avg_a,
                    filled_count=a.filled,
                    resting_count=a.resting,
                    total_fill_cost=a.total_fill_cost,
                    queue_position=evt_queue.get(pair.ticker_a),
                ),
                leg_b=LegSummary(
                    ticker=pair.ticker_b,
                    no_price=avg_b,
                    filled_count=b.filled,
                    resting_count=b.resting,
                    total_fill_cost=b.total_fill_cost,
                    queue_position=evt_queue.get(pair.ticker_b),
                ),
                matched_pairs=matched,
                locked_profit_cents=locked_profit,
                unmatched_a=unmatched_a,
                unmatched_b=unmatched_b,
                exposure_cents=exposure,
            )
        )

    return summaries
