"""PositionLedger — pure state machine for per-event position tracking.

Single source of truth for filled counts, resting orders, avg prices,
and safety gates. One instance per active event. No I/O, no async.

See brain/principles.md Principles 15-19 for safety invariants.
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

import structlog

from talos.automation_config import DEFAULT_UNIT_SIZE
from talos.fees import (
    MAKER_FEE_RATE,
    fee_adjusted_cost,
    fee_adjusted_profit_matched,
    quadratic_fee,
)
from talos.models.position import EventPositionSummary, LegSummary

if TYPE_CHECKING:
    from talos.cpm import CPMTracker
    from talos.models.strategy import ArbPair

logger = structlog.get_logger()


class Side(Enum):
    A = "A"
    B = "B"

    @property
    def other(self) -> Side:
        return Side.B if self is Side.A else Side.A


class _SideState:
    """Mutable per-side position state."""

    __slots__ = (
        "filled_count",
        "filled_total_cost",
        "filled_fees",
        "closed_count",
        "closed_total_cost",
        "closed_fees",
        "_fees_from_api",
        "resting_order_id",
        "resting_count",
        "resting_price",
        "_placed_at_gen",
    )

    def __init__(self) -> None:
        self.filled_count: int = 0
        self.filled_total_cost: int = 0
        self.filled_fees: int = 0
        self.closed_count: int = 0
        self.closed_total_cost: int = 0
        self.closed_fees: int = 0
        self._fees_from_api: bool = False
        self.resting_order_id: str | None = None
        self.resting_count: int = 0
        self.resting_price: int = 0
        self._placed_at_gen: int | None = None

    def reset(self) -> None:
        self.filled_count = 0
        self.filled_total_cost = 0
        self.filled_fees = 0
        self.closed_count = 0
        self.closed_total_cost = 0
        self.closed_fees = 0
        self._fees_from_api = False
        self.resting_order_id = None
        self.resting_count = 0
        self.resting_price = 0
        self._placed_at_gen = None


class PositionLedger:
    """Per-event position ledger — the single source of truth.

    Tracks filled and resting state per side, enforces safety gates,
    and provides position projections. Replaces compute_event_positions()
    for both UI display and bid adjustment safety.
    """

    def __init__(
        self,
        event_ticker: str,
        unit_size: int = DEFAULT_UNIT_SIZE,
        side_a_str: str = "no",
        side_b_str: str = "no",
        is_same_ticker: bool = False,
        ticker_a: str = "",
        ticker_b: str = "",
    ) -> None:
        if unit_size <= 0:
            raise ValueError(f"unit_size must be positive, got {unit_size}")
        self.event_ticker = event_ticker
        self.unit_size = unit_size
        self._side_a_str = side_a_str
        self._side_b_str = side_b_str
        self._is_same_ticker = is_same_ticker
        self._ticker_a = ticker_a
        self._ticker_b = ticker_b
        self._sync_gen: int = 0
        self._sides: dict[Side, _SideState] = {
            Side.A: _SideState(),
            Side.B: _SideState(),
        }
        # Order IDs confirmed cancelled by the API but potentially still
        # returned as "resting" by GET due to Kalshi's eventual consistency.
        # sync_from_orders filters these out until they disappear from the GET.
        self._recently_cancelled: set[str] = set()

    # ── Sync generation (stale-sync protection) ────────────────────

    def bump_sync_gen(self) -> None:
        """Increment sync generation. Call at start of each polling cycle."""
        self._sync_gen += 1

    def owns_tickers(self, ticker_a: str, ticker_b: str) -> bool:
        """Check if this ledger was created for the given tickers."""
        if not self._ticker_a:
            return True  # Legacy ledger without tickers — allow all
        return self._ticker_a == ticker_a and self._ticker_b == ticker_b

    # ── Per-side accessors ──────────────────────────────────────────

    def filled_count(self, side: Side) -> int:
        return self._sides[side].filled_count

    def filled_total_cost(self, side: Side) -> int:
        return self._sides[side].filled_total_cost

    def resting_order_id(self, side: Side) -> str | None:
        return self._sides[side].resting_order_id

    def resting_count(self, side: Side) -> int:
        return self._sides[side].resting_count

    def resting_price(self, side: Side) -> int:
        return self._sides[side].resting_price

    def filled_fees(self, side: Side) -> int:
        return self._sides[side].filled_fees

    # ── Derived queries ─────────────────────────────────────────────

    def avg_filled_price(self, side: Side) -> float:
        s = self._sides[side]
        if s.filled_count == 0:
            return 0.0
        return s.filled_total_cost / s.filled_count

    def total_committed(self, side: Side) -> int:
        s = self._sides[side]
        return s.filled_count + s.resting_count

    def current_delta(self) -> int:
        return abs(self.total_committed(Side.A) - self.total_committed(Side.B))

    def unit_remaining(self, side: Side) -> int:
        s = self._sides[side]
        filled_in_unit = s.filled_count % self.unit_size
        if filled_in_unit == 0 and s.filled_count > 0:
            return 0  # unit is complete
        return self.unit_size - filled_in_unit

    def is_unit_complete(self, side: Side) -> bool:
        s = self._sides[side]
        return s.filled_count > 0 and s.filled_count % self.unit_size == 0

    def both_sides_complete(self) -> bool:
        return self.is_unit_complete(Side.A) and self.is_unit_complete(Side.B)

    def has_pending_change(self) -> bool:
        """True if either side has an unconfirmed placement or cancel.

        When set, the ledger's resting state may not match Kalshi's actual
        state. Callers should wait for sync confirmation before acting.
        """
        return (
            self._sides[Side.A]._placed_at_gen is not None
            or self._sides[Side.B]._placed_at_gen is not None
        )

    # ── Safety gate ─────────────────────────────────────────────────

    def is_placement_safe(
        self,
        side: Side,
        count: int,
        price: int,
        *,
        rate: float = MAKER_FEE_RATE,
        catchup: bool = False,
    ) -> tuple[bool, str]:
        """Check if placing an order is safe. Returns (ok, reason).

        Enforces Principles 16 (unit gating), 18 (profitability gate).
        Pass the pair-specific ``rate`` for non-standard fee series.

        When ``catchup=True`` the P16 unit-boundary check is skipped because
        catch-up orders close an existing imbalance (risk-reducing, not
        speculative). P18 profitability is always enforced.
        """
        s = self._sides[side]

        # P16: resting + filled-in-unit + new must not exceed unit.
        # Modular arithmetic allows re-entry after a complete unit (10/10 → next pair).
        # Skipped for catch-up orders — closing a gap, not speculative exposure.
        if not catchup:
            filled_in_unit = s.filled_count % self.unit_size
            if filled_in_unit + s.resting_count + count > self.unit_size:
                return (
                    False,
                    f"would exceed unit: filled_in_unit={filled_in_unit} + "
                    f"resting={s.resting_count} + new={count} > {self.unit_size}",
                )

        # P18: fee-adjusted profitability
        other = self._sides[side.other]
        if other.filled_count > 0:
            other_price = other.filled_total_cost / other.filled_count
        elif other.resting_count > 0:
            other_price = other.resting_price
        else:
            # No position on the other side — can't check arb yet, allow placement
            return True, ""

        # Fee-adjusted: effective cost = price + fee(price)
        effective_this = fee_adjusted_cost(price, rate=rate)
        effective_other = fee_adjusted_cost(int(round(other_price)), rate=rate)
        if effective_this + effective_other >= 100:
            return (
                False,
                f"arb not profitable after fees: "
                f"{effective_this:.2f} + {effective_other:.2f} = "
                f"{effective_this + effective_other:.2f} >= 100",
            )

        return True, ""

    # ── State mutations ─────────────────────────────────────────────

    def record_fill(self, side: Side, count: int, price: int, *, fees: int = 0) -> None:
        """Record a fill. Called when polling detects new fills."""
        s = self._sides[side]
        s.filled_count += count
        s.filled_total_cost += price * count
        if fees > 0:
            s.filled_fees += fees
        # If resting order filled partially/fully, reduce resting count
        if s.resting_count > 0:
            filled_from_resting = min(count, s.resting_count)
            s.resting_count -= filled_from_resting
            if s.resting_count == 0:
                s.resting_order_id = None

    def record_resting(self, side: Side, order_id: str, count: int, price: int) -> None:
        """Record a new resting order. Called after order placement confirmed."""
        s = self._sides[side]
        s.resting_order_id = order_id
        s.resting_count = count
        s.resting_price = price

    def record_placement(self, side: Side, order_id: str, count: int, price: int) -> None:
        """Record optimistic resting state from order placement.

        Like record_resting, but marks the order as unconfirmed so that
        sync_from_orders won't clear it if given stale data from a poll
        that started before the order was created.
        """
        s = self._sides[side]
        s.resting_order_id = order_id
        s.resting_count = count
        s.resting_price = price
        s._placed_at_gen = self._sync_gen

    def record_cancel(self, side: Side, order_id: str) -> None:
        """Record an order cancellation.

        Sets _placed_at_gen so the stale-sync guard in sync_from_orders
        won't overwrite the cancel with stale API data that still shows
        the order as resting (Kalshi's eventual consistency).
        """
        s = self._sides[side]
        if s.resting_order_id != order_id:
            raise ValueError(f"order_id mismatch: expected {s.resting_order_id}, got {order_id}")
        self._recently_cancelled.add(order_id)
        s.resting_order_id = None
        s.resting_count = 0
        s.resting_price = 0
        # Protect through the NEXT cycle's sync — cancel happens after
        # bump_sync_gen in the current cycle, so sync_gen is already N.
        # The next cycle bumps to N+1; we need N+1 >= N+1 to hold.
        s._placed_at_gen = self._sync_gen + 1

    def mark_side_pending(self, side: Side) -> None:
        """Mark a side as having an unconfirmed change (stale-sync guard).

        Use when a cancel succeeded on Kalshi but record_cancel can't match
        the order_id (e.g., WS updated the ledger during the await). The
        resting state is uncertain — protect from stale sync overwrite and
        block new bids until the next confirmed sync.
        """
        self._sides[side]._placed_at_gen = self._sync_gen + 1

    def mark_order_cancelled(self, order_id: str) -> None:
        """Register an order_id as confirmed cancelled on Kalshi.

        sync_from_orders will filter this ID out until Kalshi's GET endpoint
        stops returning it (eventual consistency propagation).
        """
        self._recently_cancelled.add(order_id)

    def reset_pair(self) -> None:
        """Clear state after both sides complete. Ready for next pair."""
        self._sides[Side.A].reset()
        self._sides[Side.B].reset()

    # ── Persistence ────────────────────────────────────────────────

    def to_save_dict(self) -> dict[str, int | str | None]:
        """Export full ledger state for persistence across restarts.

        Saves fills AND resting orders so startup has accurate state
        without needing to reconstruct from Kalshi APIs.
        """
        a = self._sides[Side.A]
        b = self._sides[Side.B]
        return {
            "filled_a": a.filled_count,
            "cost_a": a.filled_total_cost,
            "fees_a": a.filled_fees,
            "filled_b": b.filled_count,
            "cost_b": b.filled_total_cost,
            "fees_b": b.filled_fees,
            "resting_id_a": a.resting_order_id,
            "resting_count_a": a.resting_count,
            "resting_price_a": a.resting_price,
            "resting_id_b": b.resting_order_id,
            "resting_count_b": b.resting_count,
            "resting_price_b": b.resting_price,
        }

    def seed_from_saved(self, data: dict[str, int | str | None] | None) -> None:
        """Seed full ledger state from persisted data.

        Fills: sets a floor (monotonic — sync can only increase).
        Resting: restored directly so check_imbalances sees accurate
        state on the first cycle instead of phantom imbalances.
        The normal sync_from_orders cycle will correct any drift.
        """
        if not data:
            return
        a = self._sides[Side.A]
        b = self._sides[Side.B]
        for side, prefix in [(a, "a"), (b, "b")]:
            saved_fills = data.get(f"filled_{prefix}", 0)
            saved_cost = data.get(f"cost_{prefix}", 0)
            saved_fees = data.get(f"fees_{prefix}", 0)
            if isinstance(saved_fills, int) and saved_fills > side.filled_count:
                logger.info(
                    "ledger_seeded_from_saved",
                    event_ticker=self.event_ticker,
                    side=prefix.upper(),
                    saved_fills=saved_fills,
                    current_fills=side.filled_count,
                )
                side.filled_count = saved_fills
                side.filled_total_cost = max(side.filled_total_cost, int(saved_cost or 0))
                side.filled_fees = max(side.filled_fees, int(saved_fees or 0))

            # Restore resting state
            saved_id = data.get(f"resting_id_{prefix}")
            saved_count = data.get(f"resting_count_{prefix}", 0)
            saved_price = data.get(f"resting_price_{prefix}", 0)
            if saved_id and isinstance(saved_count, int) and saved_count > 0:
                side.resting_order_id = str(saved_id)
                side.resting_count = saved_count
                side.resting_price = int(saved_price or 0)

    def sync_from_orders(self, orders: list, ticker_a: str, ticker_b: str) -> None:
        """Reconcile ledger against polled order state from Kalshi.

        Fill counts: monotonically increasing — the orders API archives old
        filled/cancelled orders, so it may report fewer fills than the
        positions API has already set. We never decrease fills (P7/P15).

        Resting orders: authoritative — summed across all active orders per
        side to support multiple resting orders on the same side.

        Called every polling cycle. See also sync_from_positions() which
        patches fill gaps from the positions API.
        """
        # Build mapping: same-ticker pairs use order.side, cross-ticker uses ticker
        if self._is_same_ticker:
            side_map: dict[str, Side] | None = {self._side_a_str: Side.A, self._side_b_str: Side.B}
        else:
            side_map = None
        ticker_to_side = {ticker_a: Side.A, ticker_b: Side.B}

        kalshi_filled: dict[Side, int] = {Side.A: 0, Side.B: 0}
        kalshi_fill_cost: dict[Side, int] = {Side.A: 0, Side.B: 0}
        kalshi_fees: dict[Side, int] = {Side.A: 0, Side.B: 0}
        kalshi_resting: dict[Side, list[tuple[str, int, int]]] = {
            Side.A: [],
            Side.B: [],
        }

        for order in orders:
            # Side-aware filtering
            if self._is_same_ticker:
                assert side_map is not None  # for type checker
                # Must match the pair's ticker AND be a buy on an expected side
                if order.ticker != ticker_a:
                    continue
                if order.action != "buy" or order.side not in side_map:
                    continue
                side = side_map[order.side]
            else:
                if order.side != "no" or order.action != "buy":
                    continue
                side = ticker_to_side.get(order.ticker)
                if side is None:
                    continue

            # Count fills from ALL orders including cancelled — fills are real
            # regardless of whether the order was later cancelled or amended
            if order.fill_count > 0:
                kalshi_filled[side] += order.fill_count
                kalshi_fill_cost[side] += order.maker_fill_cost + order.taker_fill_cost
                kalshi_fees[side] += order.maker_fees
            # Only track resting from active orders — skip recently cancelled
            # IDs that Kalshi's GET may still return due to eventual consistency
            if order.remaining_count > 0 and order.status in ("resting", "executed"):
                if order.order_id in self._recently_cancelled:
                    continue
                # Use correct price field based on order side
                resting_price = order.no_price if order.side == "no" else order.yes_price
                kalshi_resting[side].append((order.order_id, order.remaining_count, resting_price))

        for side in (Side.A, Side.B):
            s = self._sides[side]

            # Fills: only increase. Orders API archives old orders, so
            # kalshi_filled may be lower than positions-augmented fills.
            # When orders reports >= current, use its data (more detailed
            # cost/fee breakdown). When less, keep existing.
            if kalshi_filled[side] >= s.filled_count and kalshi_filled[side] > 0:
                s.filled_count = kalshi_filled[side]
                s.filled_total_cost = kalshi_fill_cost[side]
                s.filled_fees = kalshi_fees[side]
                s._fees_from_api = True

            # Resting: trust orders API. Sum across multiple orders.
            resting_list = kalshi_resting[side]
            if resting_list:
                # Stale-sync guard: if a cancel was recorded during the current
                # sync gen (resting_order_id is None but API still shows resting),
                # the API response predates the cancel. Skip to avoid overwriting.
                if (
                    s._placed_at_gen is not None
                    and s._placed_at_gen >= self._sync_gen
                    and s.resting_order_id is None
                ):
                    logger.info(
                        "stale_sync_resting_skipped",
                        event_ticker=self.event_ticker,
                        side=side.value,
                        placed_gen=s._placed_at_gen,
                        sync_gen=self._sync_gen,
                    )
                    continue
                total_resting = sum(cnt for _, cnt, _ in resting_list)
                s.resting_order_id = resting_list[0][0]
                s.resting_count = total_resting
                s.resting_price = resting_list[0][2]
                s._placed_at_gen = None  # Confirmed by sync
                if len(resting_list) > 1:
                    logger.info(
                        "multiple_resting_orders_summed",
                        event_ticker=self.event_ticker,
                        side=side.value,
                        order_count=len(resting_list),
                        total_resting=total_resting,
                    )
            else:
                # Stale-sync guard: if record_placement was called during
                # the current sync generation, the orders list may predate
                # the placement. Preserve the optimistic resting state.
                if s._placed_at_gen is not None and s._placed_at_gen >= self._sync_gen:
                    logger.info(
                        "stale_sync_resting_preserved",
                        event_ticker=self.event_ticker,
                        side=side.value,
                        placed_gen=s._placed_at_gen,
                        sync_gen=self._sync_gen,
                    )
                    continue

                if s.resting_order_id is not None:
                    logger.info(
                        "resting_order_cleared",
                        event_ticker=self.event_ticker,
                        side=side.value,
                        order_id=s.resting_order_id,
                    )
                s.resting_order_id = None
                s.resting_count = 0
                s.resting_price = 0
                s._placed_at_gen = None

        # Prune recently-cancelled IDs that are confirmed gone from the GET.
        # Keep only IDs that were still returned as resting (filtered above).
        if self._recently_cancelled:
            still_stale = set()
            for order in orders:
                if (
                    order.order_id in self._recently_cancelled
                    and order.remaining_count > 0
                    and order.status in ("resting", "executed")
                ):
                    still_stale.add(order.order_id)
            # IDs not in still_stale are confirmed gone — remove them
            self._recently_cancelled = still_stale

        # Two-source sync (orders + positions) keeps the ledger accurate.

    def sync_from_positions(
        self,
        position_fills: dict[Side, int],
        position_costs: dict[Side, int],
        position_fees: dict[Side, int] | None = None,
    ) -> None:
        """Augment ledger with authoritative data from positions API.

        GET /portfolio/positions always reflects the true state — it never
        archives, unlike GET /portfolio/orders. When orders-based fill counts
        are lower than what positions reports, the ledger is missing fills
        from archived orders. This method patches the gap (P7/P15).

        Called AFTER sync_from_orders so it can detect and fix shortfalls.
        """
        if self._is_same_ticker:
            return  # Positions API reports net, useless for YES/NO pairs
        if position_fees is None:
            position_fees = {Side.A: 0, Side.B: 0}

        for side in (Side.A, Side.B):
            s = self._sides[side]
            auth_fills = position_fills[side]

            if auth_fills > s.filled_count:
                logger.warning(
                    "fills_augmented_from_positions_api",
                    event_ticker=self.event_ticker,
                    side=side.value,
                    ledger_fills=s.filled_count,
                    positions_fills=auth_fills,
                )
                s.filled_count = auth_fills

            # Positions API cost is authoritative when fills were augmented
            # (orders API archived the filled orders). Also use it when
            # orders-based cost is zero or when positions reports higher
            # cost (more complete data from un-archived source).
            pos_cost = position_costs[side]
            if pos_cost > 0 and pos_cost > s.filled_total_cost:
                logger.info(
                    "cost_augmented_from_positions_api",
                    event_ticker=self.event_ticker,
                    side=side.value,
                    ledger_cost=s.filled_total_cost,
                    positions_cost=pos_cost,
                )
                s.filled_total_cost = pos_cost

            # Fees from positions API — authoritative when orders are archived
            pos_fees = position_fees[side]
            if pos_fees > 0 and pos_fees > s.filled_fees:
                s.filled_fees = pos_fees
                s._fees_from_api = True

    def format_position(self, side: Side) -> str:
        """Human-readable position string for proposals."""
        s = self._sides[side]
        parts: list[str] = []
        if s.filled_count > 0:
            avg = self.avg_filled_price(side)
            parts.append(f"{s.filled_count} filled @ {avg:.1f}c")
        if s.resting_count > 0:
            parts.append(f"{s.resting_count} resting @ {s.resting_price}c")
        return ", ".join(parts) if parts else "empty"


def _prorate(total: int, portion: int, denominator: int) -> int:
    """Proportionally allocate *total* based on portion/denominator (integer math)."""
    return total * portion // denominator if denominator > 0 else 0


def compute_display_positions(
    ledgers: dict[str, PositionLedger],
    pairs: list[ArbPair],
    queue_cache: dict[str, int],
    cpm_tracker: CPMTracker,
) -> list[EventPositionSummary]:
    """Compute position summaries from ledger state for UI display.

    Replacement for compute_event_positions() — reads from PositionLedger
    instead of raw orders. Also handles CPM/ETA enrichment inline.
    """
    summaries: list[EventPositionSummary] = []
    for pair in pairs:
        ledger = ledgers.get(pair.event_ticker)
        if ledger is None:
            continue

        filled_a = ledger.filled_count(Side.A)
        filled_b = ledger.filled_count(Side.B)
        resting_a = ledger.resting_count(Side.A)
        resting_b = ledger.resting_count(Side.B)

        if filled_a + filled_b + resting_a + resting_b == 0:
            continue

        matched = min(filled_a, filled_b)
        unmatched_a = filled_a - matched
        unmatched_b = filled_b - matched

        cost_a = ledger.filled_total_cost(Side.A)
        cost_b = ledger.filled_total_cost(Side.B)
        fees_a = ledger.filled_fees(Side.A)
        fees_b = ledger.filled_fees(Side.B)

        # When orders were archived across restart, fees are lost (zero)
        # but cost/count are restored from positions API. Estimate fees
        # from the quadratic formula to avoid showing gross-only profit.
        # Only estimate when the API has been consulted (_fees_from_api)
        # but returned zero — not when record_fill() was used without sync.
        side_a = ledger._sides[Side.A]
        side_b = ledger._sides[Side.B]
        if fees_a == 0 and filled_a > 0 and cost_a > 0 and side_a._fees_from_api:
            avg_a = cost_a // filled_a
            fees_a = round(quadratic_fee(avg_a, rate=pair.fee_rate) * filled_a)
        if fees_b == 0 and filled_b > 0 and cost_b > 0 and side_b._fees_from_api:
            avg_b = cost_b // filled_b
            fees_b = round(quadratic_fee(avg_b, rate=pair.fee_rate) * filled_b)

        if matched > 0:
            cost_a_matched = _prorate(cost_a, matched, filled_a)
            cost_b_matched = _prorate(cost_b, matched, filled_b)
            fees_a_matched = _prorate(fees_a, matched, filled_a)
            fees_b_matched = _prorate(fees_b, matched, filled_b)
            locked_profit = fee_adjusted_profit_matched(
                matched, cost_a_matched, cost_b_matched, fees_a_matched, fees_b_matched
            )
        else:
            locked_profit = 0.0

        exposure = _prorate(cost_a, unmatched_a, filled_a) + _prorate(cost_b, unmatched_b, filled_b)

        avg_a = cost_a // filled_a if filled_a > 0 else ledger.resting_price(Side.A)
        avg_b = cost_b // filled_b if filled_b > 0 else ledger.resting_price(Side.B)

        # Queue positions from cache (keyed by order_id)
        oid_a = ledger.resting_order_id(Side.A)
        qp_a = queue_cache.get(oid_a) if oid_a is not None else None
        oid_b = ledger.resting_order_id(Side.B)
        qp_b = queue_cache.get(oid_b) if oid_b is not None else None

        # CPM enrichment
        cpm_a = cpm_tracker.cpm(pair.ticker_a)
        cpm_a_partial = cpm_tracker.is_partial(pair.ticker_a)
        eta_a = cpm_tracker.eta_minutes(pair.ticker_a, qp_a) if qp_a is not None else None

        cpm_b = cpm_tracker.cpm(pair.ticker_b)
        cpm_b_partial = cpm_tracker.is_partial(pair.ticker_b)
        eta_b = cpm_tracker.eta_minutes(pair.ticker_b, qp_b) if qp_b is not None else None

        summaries.append(
            EventPositionSummary(
                event_ticker=pair.event_ticker,
                leg_a=LegSummary(
                    ticker=pair.ticker_a,
                    no_price=avg_a,
                    filled_count=filled_a,
                    resting_count=resting_a,
                    total_fill_cost=cost_a,
                    total_fees=fees_a,
                    queue_position=qp_a,
                    cpm=cpm_a,
                    cpm_partial=cpm_a_partial,
                    eta_minutes=eta_a,
                    resting_no_price=ledger.resting_price(Side.A) if resting_a > 0 else None,
                ),
                leg_b=LegSummary(
                    ticker=pair.ticker_b,
                    no_price=avg_b,
                    filled_count=filled_b,
                    resting_count=resting_b,
                    total_fill_cost=cost_b,
                    total_fees=fees_b,
                    queue_position=qp_b,
                    cpm=cpm_b,
                    cpm_partial=cpm_b_partial,
                    eta_minutes=eta_b,
                    resting_no_price=ledger.resting_price(Side.B) if resting_b > 0 else None,
                ),
                matched_pairs=matched,
                locked_profit_cents=locked_profit,
                unmatched_a=unmatched_a,
                unmatched_b=unmatched_b,
                exposure_cents=exposure,
                unit_size=ledger.unit_size,
            )
        )

    return summaries
