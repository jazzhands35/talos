"""Rebalance detection and execution — extracted from TradingEngine.

Pure detection (compute_rebalance_proposal) and async execution
(execute_rebalance) follow the pure state + async orchestrator split.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog

from talos.errors import KalshiAPIError
from talos.models.proposal import Proposal, ProposalKey, ProposedRebalance
from talos.position_ledger import PositionLedger, Side

if TYPE_CHECKING:
    from collections.abc import Callable

    from talos.bid_adjuster import BidAdjuster
    from talos.models.strategy import ArbPair, Opportunity
    from talos.orderbook import OrderBookManager
    from talos.rest_client import KalshiRESTClient
    from talos.scanner import ArbitrageScanner

logger = structlog.get_logger()


def _is_no_op(err: KalshiAPIError) -> bool:
    """Check if the API error is a no-op amend (desired state already reached)."""
    if isinstance(err.body, dict):
        inner = err.body.get("error", {})
        if isinstance(inner, dict):
            return inner.get("code") == "AMEND_ORDER_NO_OP"
    return False


# ── Pure detection ───────────────────────────────────────────────────


def compute_rebalance_proposal(
    event_ticker: str,
    ledger: PositionLedger,
    pair: ArbPair,
    scanner_snapshot: Opportunity | None,
    display_name: str,
    book_manager: OrderBookManager,
) -> Proposal | None:
    """Compute a rebalance proposal for a single event, or None if balanced.

    Pure function — no I/O, no side effects beyond logging.
    """
    committed_a = ledger.total_committed(Side.A)
    committed_b = ledger.total_committed(Side.B)

    if committed_a == 0 and committed_b == 0:
        return None

    # No resting orders + fills balanced -> settled, nothing actionable
    if (
        ledger.resting_count(Side.A) == 0
        and ledger.resting_count(Side.B) == 0
        and ledger.filled_count(Side.A) == ledger.filled_count(Side.B)
    ):
        return None

    # No resting + markets closed -> settled with imbalance, nothing actionable
    if ledger.resting_count(Side.A) == 0 and ledger.resting_count(Side.B) == 0:
        if not book_manager.best_ask(pair.ticker_a) and not book_manager.best_ask(
            pair.ticker_b
        ):
            return None

    delta = committed_a - committed_b
    # Any non-zero committed delta = unhedged exposure. Unit size controls
    # entry/re-entry qty, but catch-up must close ANY gap.
    if abs(delta) == 0:
        return None

    # Determine over-extended side
    if delta > 0:
        over, under = Side.A, Side.B
        over_committed, under_committed = committed_a, committed_b
    else:
        over, under = Side.B, Side.A
        over_committed, under_committed = committed_b, committed_a

    over_resting = ledger.resting_count(over)
    over_filled = ledger.filled_count(over)
    under_resting = ledger.resting_count(under)

    over_ticker = pair.ticker_a if over == Side.A else pair.ticker_b
    under_ticker = pair.ticker_a if under == Side.A else pair.ticker_b
    over_order_id = ledger.resting_order_id(over)

    # Target = over_filled. Over-side resting is always cancelled (reduce
    # exposure first), then under-side catches up to match fills.
    target = over_filled
    target_over_resting = max(0, target - over_filled)  # always 0
    reduce_by = over_resting - target_over_resting

    # Step 2: catch-up on under-side (reduce by existing resting already working)
    gap = target - under_committed
    effective_gap = max(0, gap - under_resting)
    catchup_qty = 0
    catchup_price = 0
    catchup_ticker: str | None = None
    if effective_gap > 0:
        catchup_qty = effective_gap  # full gap — no min(effective_gap, unit_size) cap
        catchup_ticker = under_ticker
        # Get current price from scanner snapshot
        if scanner_snapshot is not None:
            catchup_price = (
                scanner_snapshot.no_a if under == Side.A else scanner_snapshot.no_b
            )
        if scanner_snapshot is None or catchup_price <= 0:
            catchup_qty = 0  # Can't determine price — skip catch-up
            catchup_ticker = None

        # Pre-check P18 profitability — skip catch-up if the arb can't
        # possibly be profitable.  The execution-time check in
        # execute_rebalance() remains as a safety net with fresh data.
        if catchup_qty > 0:
            ok, _ = ledger.is_placement_safe(
                under, catchup_qty, catchup_price,
                rate=pair.fee_rate, catchup=True,
            )
            if not ok:
                catchup_qty = 0
                catchup_ticker = None

    # Build step descriptions for the detail text
    steps: list[str] = []
    if reduce_by > 0:
        if target_over_resting == 0:
            steps.append(f"Cancel {over_resting} resting on {over.value}")
        else:
            steps.append(
                f"Reduce {over.value} resting {over_resting} \u2192 {target_over_resting}"
            )
    if catchup_qty > 0:
        steps.append(f"Place {catchup_qty} @ {catchup_price}c on {under.value}")
    if not steps:
        steps.append(
            f"Side {over.value} has {over_filled} fills vs "
            f"side {under.value} {ledger.filled_count(under)} "
            f"(under-side has {under_resting} resting \u2014 wait or adjust)"
        )

    logger.warning(
        "position_imbalance",
        event_ticker=event_ticker,
        over_side=over.value,
        committed_over=over_committed,
        committed_under=under_committed,
        delta=abs(delta),
    )

    # Build rebalance data if we have any executable step
    rebalance_data = None
    has_reduce = reduce_by > 0 and over_order_id is not None
    has_catchup = catchup_qty > 0 and catchup_ticker is not None
    if has_reduce or has_catchup:
        rebalance_data = ProposedRebalance(
            event_ticker=event_ticker,
            side=over.value,
            order_id=over_order_id if has_reduce else None,
            ticker=over_ticker if has_reduce else None,
            current_resting=over_resting if has_reduce else 0,
            target_resting=target_over_resting if has_reduce else 0,
            resting_price=ledger.resting_price(over) if has_reduce else 0,
            filled_count=over_filled if has_reduce else 0,
            catchup_ticker=catchup_ticker if has_catchup else None,
            catchup_price=catchup_price if has_catchup else 0,
            catchup_qty=catchup_qty if has_catchup else 0,
        )

    key = ProposalKey(
        event_ticker=event_ticker,
        side=over.value,
        kind="rebalance",
    )
    return Proposal(
        key=key,
        kind="rebalance",
        summary=f"REBALANCE {display_name} side {over.value}",
        detail=(
            f"Imbalanced: {over.value}={over_committed} vs "
            f"{under.value}={under_committed} "
            f"(delta {abs(delta)}). {'; '.join(steps)}"
        ),
        created_at=datetime.now(UTC),
        rebalance=rebalance_data,
    )


def compute_topup_needs(
    ledger: PositionLedger,
    pair: ArbPair,
    snapshot: Opportunity | None,
) -> dict[Side, tuple[int, int]]:
    """Compute top-up needs for mid-unit sides with no resting bids.

    Returns dict mapping Side → (qty, price) for each side needing top-up.
    Only fires when committed counts are equal (catch-up handles imbalances).
    Pure function — no I/O.
    """
    if snapshot is None:
        return {}

    filled_a = ledger.filled_count(Side.A)
    filled_b = ledger.filled_count(Side.B)

    # Only fire when both sides are in the same completed-unit "tier".
    # A cross-unit gap means catch-up should close it first.
    if filled_a // ledger.unit_size != filled_b // ledger.unit_size:
        return {}

    needs: dict[Side, tuple[int, int]] = {}
    for side in (Side.A, Side.B):
        filled = ledger.filled_count(side)
        resting = ledger.resting_count(side)

        if filled == 0:
            continue
        if resting > 0:
            continue

        filled_in_unit = filled % ledger.unit_size
        if filled_in_unit == 0:
            continue

        qty = ledger.unit_size - filled_in_unit
        price = snapshot.no_a if side == Side.A else snapshot.no_b
        if price <= 0:
            continue
        needs[side] = (qty, price)

    # Guard: if only ONE side would be topped up, verify it won't create
    # a committed delta that rebalance immediately cancels (thrashing loop).
    # Two-sided top-ups are safe — they balance each other.
    if len(needs) == 1:
        side = next(iter(needs))
        qty, _ = needs[side]
        other = Side.B if side == Side.A else Side.A
        if ledger.total_committed(side) + qty > ledger.total_committed(other):
            return {}

    return needs


# ── Async execution ─────────────────────────────────────────────────


async def execute_rebalance(
    rebalance: ProposedRebalance,
    *,
    rest_client: KalshiRESTClient,
    adjuster: BidAdjuster,
    scanner: ArbitrageScanner,
    notify: Callable[[str, str], None],
) -> None:
    """Execute a two-step rebalance: reduce over-side, then catch up under-side.

    Step 1 (reduce) always runs before step 2 (catch-up) to maintain
    delta neutrality at every intermediate state.
    """
    # Step 1: Reduce over-side resting
    has_reduce = (
        rebalance.order_id is not None
        and rebalance.ticker is not None
        and rebalance.current_resting > rebalance.target_resting
    )
    if has_reduce:
        try:
            assert rebalance.order_id is not None  # guarded by has_reduce
            assert rebalance.ticker is not None
            if rebalance.target_resting == 0:
                # Cancel ALL resting orders on this side — not just the
                # one tracked in the ledger. Orphaned orders from previous
                # sessions or race conditions may exist on Kalshi.
                cancelled_count, cancelled_ids = await _cancel_all_resting(
                    rest_client,
                    rebalance.event_ticker,
                    rebalance.ticker,
                    rebalance.order_id,
                )
                # Update ledger immediately so next cycle doesn't re-cancel
                try:
                    ledger = adjuster.get_ledger(rebalance.event_ticker)
                    try:
                        ledger.record_cancel(
                            Side(rebalance.side), rebalance.order_id
                        )
                    except ValueError:
                        pass  # Order_id mismatch — mark_side_pending below
                    # Register ALL cancelled IDs so sync_from_orders filters
                    # them out until Kalshi's GET confirms they're gone.
                    for oid in cancelled_ids:
                        ledger.mark_order_cancelled(oid)
                    ledger.mark_side_pending(Side(rebalance.side))
                except KeyError:
                    pass  # Ledger missing — sync will fix
                if cancelled_count > 0:
                    notify(
                        f"Rebalance step 1: cancelled {cancelled_count}"
                        f" resting on {rebalance.side} ({rebalance.ticker})",
                        "information",
                    )
            else:
                # Use decrease_order for quantity-only reductions (preserves
                # queue position, simpler semantics than amend).
                fresh_order = await rest_client.get_order(rebalance.order_id)
                if fresh_order.remaining_count <= rebalance.target_resting:
                    notify(
                        f"Rebalance step 1: already at target"
                        f" (remaining={fresh_order.remaining_count})",
                        "information",
                    )
                else:
                    logger.info(
                        "rebalance_decrease",
                        order_id=rebalance.order_id,
                        order_remaining=fresh_order.remaining_count,
                        target_resting=rebalance.target_resting,
                    )
                    await rest_client.decrease_order(
                        rebalance.order_id,
                        reduce_to=rebalance.target_resting,
                    )
                    # Update ledger so next cycle sees reduced count
                    try:
                        ledger = adjuster.get_ledger(rebalance.event_ticker)
                        ledger.record_resting(
                            Side(rebalance.side),
                            rebalance.order_id,
                            rebalance.target_resting,
                            rebalance.resting_price,
                        )
                    except KeyError:
                        pass
                    notify(
                        f"Rebalance step 1: {rebalance.side} resting"
                        f" {fresh_order.remaining_count}"
                        f" \u2192 {rebalance.target_resting}",
                        "information",
                    )
        except KalshiAPIError as e:
            if _is_no_op(e):
                # Order already at desired state (fills happened between
                # proposal and execution). Treat as success — proceed to
                # step 2.
                notify(
                    "Rebalance step 1: already at target (no-op)",
                    "information",
                )
            else:
                notify(f"Rebalance FAILED (reduce): {e}", "error")
                logger.exception(
                    "rebalance_reduce_error",
                    event_ticker=rebalance.event_ticker,
                    side=rebalance.side,
                    order_id=rebalance.order_id,
                )
                return  # Don't proceed to catch-up if reduce failed
        except Exception as e:
            notify(
                f"Rebalance FAILED (reduce): {type(e).__name__}: {e}",
                "error",
            )
            logger.exception(
                "rebalance_reduce_error",
                event_ticker=rebalance.event_ticker,
                side=rebalance.side,
                order_id=rebalance.order_id,
            )
            return  # Don't proceed to catch-up if reduce failed

    # Step 2: Catch-up bid on under-side
    if rebalance.catchup_ticker and rebalance.catchup_qty > 0:
        under_side = Side.A if rebalance.side == "B" else Side.B

        # Fresh sync from Kalshi before placing (P7/P21 — Kalshi is ALWAYS
        # source of truth). The proposal was computed from potentially stale
        # ledger data. Re-fetch orders and re-verify the imbalance exists.
        pair = _find_pair(scanner, rebalance.event_ticker)
        if pair is None:
            notify("Catch-up BLOCKED: pair not found", "error")
            return

        try:
            orders = await rest_client.get_all_orders(
                event_ticker=rebalance.event_ticker,
            )
            ledger = adjuster.get_ledger(rebalance.event_ticker)
            ledger.sync_from_orders(
                orders, ticker_a=pair.ticker_a, ticker_b=pair.ticker_b
            )
        except Exception as e:
            logger.warning(
                "rebalance_fresh_sync_failed",
                event_ticker=rebalance.event_ticker,
                exc_info=True,
            )
            notify(
                f"Catch-up BLOCKED: fresh sync failed ({type(e).__name__})",
                "error",
            )
            return

        # Re-check with fresh data — recalculate qty
        over_side = Side.A if rebalance.side == "A" else Side.B
        fresh_over_filled = ledger.filled_count(over_side)
        fresh_under_committed = ledger.total_committed(under_side)
        fresh_catchup_qty = max(0, fresh_over_filled - fresh_under_committed)
        if fresh_catchup_qty <= 0:
            notify(
                "Catch-up skipped — fresh sync shows gap closed (balanced)",
                "information",
            )
            logger.info(
                "rebalance_catchup_skipped_after_sync",
                event_ticker=rebalance.event_ticker,
                fresh_over_filled=fresh_over_filled,
                fresh_under_committed=fresh_under_committed,
            )
            return

        # Safety gate — same checks as place_bids (P16, P18).
        # catchup=True bypasses P16 unit boundary (risk-reducing, not speculative).
        ok, reason = ledger.is_placement_safe(
            under_side,
            fresh_catchup_qty,
            rebalance.catchup_price,
            rate=pair.fee_rate,
            catchup=True,
        )
        if not ok:
            notify(
                f"Catch-up BLOCKED ({under_side.value}): {reason}",
                "warning",
            )
            logger.warning(
                "rebalance_catchup_blocked",
                event_ticker=rebalance.event_ticker,
                side=under_side.value,
                reason=reason,
            )
            return

        catchup_group = await _create_order_group(
            rest_client,
            rebalance.event_ticker,
            under_side.value,
            fresh_catchup_qty,
        )
        try:
            await rest_client.create_order(
                ticker=rebalance.catchup_ticker,
                action="buy",
                side="no",
                no_price=rebalance.catchup_price,
                count=fresh_catchup_qty,
                order_group_id=catchup_group,
            )
            notify(
                f"Rebalance step 2: catch-up {rebalance.catchup_ticker}"
                f" {fresh_catchup_qty} @ {rebalance.catchup_price}c",
                "information",
            )
            logger.info(
                "rebalance_catchup_placed",
                event_ticker=rebalance.event_ticker,
                ticker=rebalance.catchup_ticker,
                qty=fresh_catchup_qty,
                price=rebalance.catchup_price,
            )
        except Exception as e:
            notify(
                f"Catch-up FAILED: {type(e).__name__}: {e}",
                "error",
            )
            logger.exception(
                "rebalance_catchup_error",
                event_ticker=rebalance.event_ticker,
                ticker=rebalance.catchup_ticker,
            )


# ── Helpers ──────────────────────────────────────────────────────────


async def _cancel_all_resting(
    rest_client: KalshiRESTClient,
    event_ticker: str,
    ticker: str,
    primary_order_id: str,
) -> tuple[int, list[str]]:
    """Cancel all resting NO-buy orders on a specific ticker.

    First cancels the primary order_id (from the proposal), then fetches
    all orders for the event and cancels any other resting orders on the
    same ticker. Returns (total_contracts_cancelled, list_of_order_ids).
    """
    total_cancelled = 0
    cancelled_ids: list[str] = []

    # Cancel the primary order first
    try:
        await rest_client.cancel_order(primary_order_id)
        cancelled_ids.append(primary_order_id)
    except KalshiAPIError:
        pass  # Already cancelled or not found — continue to sweep

    # Sweep: fetch all resting orders for this event, cancel any on our ticker
    try:
        orders = await rest_client.get_all_orders(
            event_ticker=event_ticker, status="resting",
        )
        for order in orders:
            if order.ticker != ticker:
                continue
            if order.order_id == primary_order_id:
                total_cancelled += order.remaining_count
                continue  # Already cancelled above
            if order.side != "no" or order.action != "buy":
                continue
            if order.remaining_count <= 0:
                continue
            try:
                await rest_client.cancel_order(order.order_id)
                total_cancelled += order.remaining_count
                cancelled_ids.append(order.order_id)
                logger.info(
                    "orphan_order_cancelled",
                    event_ticker=event_ticker,
                    ticker=ticker,
                    order_id=order.order_id,
                    remaining=order.remaining_count,
                )
            except KalshiAPIError:
                pass  # Best effort
    except Exception:
        logger.warning(
            "orphan_sweep_failed",
            event_ticker=event_ticker,
            ticker=ticker,
            exc_info=True,
        )

    return total_cancelled, cancelled_ids


def _find_pair(scanner: ArbitrageScanner, event_ticker: str) -> ArbPair | None:
    """Look up scanner pair by event ticker."""
    for pair in scanner.pairs:
        if pair.event_ticker == event_ticker:
            return pair
    return None


async def _create_order_group(
    rest_client: KalshiRESTClient,
    event_ticker: str,
    side: str,
    qty: int,
) -> str | None:
    """Create a server-side order group for fill-limit safety."""
    ts = datetime.now(UTC).strftime("%H%M%S")
    name = f"{event_ticker}-{side}-{qty}-{ts}"
    try:
        group_id = await rest_client.create_order_group(name, qty)
        logger.info(
            "order_group_created",
            event_ticker=event_ticker,
            side=side,
            limit=qty,
            group_id=group_id,
        )
        return group_id
    except Exception:
        logger.warning(
            "order_group_create_failed",
            event_ticker=event_ticker,
            side=side,
            exc_info=True,
        )
        return None
