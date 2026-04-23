"""Rebalance detection and execution — extracted from TradingEngine.

Pure detection (compute_rebalance_proposal) and async execution
(execute_rebalance) follow the pure state + async orchestrator split.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog

from talos.errors import KalshiAPIError, KalshiRateLimitError
from talos.fees import max_profitable_price
from talos.models.order import Order
from talos.models.proposal import Proposal, ProposalKey, ProposedRebalance
from talos.position_ledger import PositionLedger, Side
from talos.units import ONE_CONTRACT_FP100, bps_to_cents_round, cents_to_bps

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from talos.bid_adjuster import BidAdjuster
    from talos.market_feed import MarketFeed
    from talos.models.strategy import ArbPair, Opportunity
    from talos.orderbook import OrderBookManager
    from talos.rest_client import KalshiRESTClient
    from talos.scanner import ArbitrageScanner

    # F36: engine-owned cancel wrapper. Signature:
    #   async def cancel_order_with_verify(order_id: str, pair: ArbPair) -> None
    CancelWithVerify = Callable[[str, ArbPair], Awaitable[None]]

logger = structlog.get_logger()


def _order_remaining_fp100(order: Order) -> int:
    """Read remaining count as fp100 (post-13a-2a: direct passthrough)."""
    return order.remaining_count_fp100


def _order_remaining_contracts(order: Order) -> int:
    """Whole-contract remaining count for display / REST-wire comparisons.

    Rebalance produces whole-contract cancel/amend payloads (Kalshi's
    amend_order / create_order on this path accept whole units only), so
    we floor fp100 → contracts.
    """
    return _order_remaining_fp100(order) // ONE_CONTRACT_FP100


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

    # No resting orders + fills balanced -> balanced, nothing actionable
    if (
        ledger.resting_count(Side.A) == 0
        and ledger.resting_count(Side.B) == 0
        and ledger.filled_count(Side.A) == ledger.filled_count(Side.B)
    ):
        return None

    # No resting + markets closed -> balanced with imbalance, nothing actionable
    if (
        ledger.resting_count(Side.A) == 0
        and ledger.resting_count(Side.B) == 0
        and not book_manager.best_ask(pair.ticker_a, side=pair.side_a)
        and not book_manager.best_ask(pair.ticker_b, side=pair.side_b)
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
            catchup_price = scanner_snapshot.no_a if under == Side.A else scanner_snapshot.no_b
        if scanner_snapshot is None or catchup_price <= 0:
            catchup_qty = 0  # Can't determine price — skip catch-up
            catchup_ticker = None

        # Pre-check P18 profitability.  If the snapshot price is
        # unprofitable against historical fills, fall back to the max
        # profitable price as a resting bid.  This resolves stuck
        # "Waiting" states where catch-up is blocked because fills were
        # at worse prices than the current market.
        if catchup_qty > 0:
            ok, _ = ledger.is_placement_safe(
                under,
                catchup_qty,
                catchup_price,
                rate=pair.fee_rate,
                catchup=True,
            )
            if not ok:
                # Compute the highest price that IS profitable
                if ledger.open_count(over) > 0:
                    other_avg = ledger.open_avg_filled_price(over)
                    fallback = max_profitable_price(
                        other_avg,
                        rate=pair.fee_rate,
                    )
                    if fallback > 0:
                        orig = catchup_price  # snapshot price before fallback
                        catchup_price = fallback
                        logger.info(
                            "catchup_price_fallback",
                            event_ticker=event_ticker,
                            snapshot_price=orig,
                            fallback_price=fallback,
                        )
                    else:
                        catchup_qty = 0
                        catchup_ticker = None
                else:
                    catchup_qty = 0
                    catchup_ticker = None

    # Determine Kalshi order side for each leg
    over_side_str = pair.side_a if over == Side.A else pair.side_b
    under_side_str = pair.side_a if under == Side.A else pair.side_b

    # Build step descriptions for the detail text
    steps: list[str] = []
    if reduce_by > 0:
        if target_over_resting == 0:
            steps.append(f"Cancel {over_resting} resting on {over.value}")
        else:
            steps.append(f"Reduce {over.value} resting {over_resting} \u2192 {target_over_resting}")
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
            reduce_side=over_side_str,
            catchup_side=under_side_str,
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


def compute_overcommit_reduction(
    event_ticker: str,
    ledger: PositionLedger,
    pair: ArbPair,
    display_name: str,
    reconciled_targets: dict[str, int] | None = None,
) -> ProposedRebalance | None:
    """Compute resting reduction for a single-side overcommit with no cross-side imbalance.

    This handles the case where committed counts are balanced (delta = 0)
    but one side violates unit capacity (filled_in_unit + resting > unit_size).
    Example: Side A has 20 filled + 3 resting = 23, Side B has 3 filled + 20
    resting = 23. Balanced, but B is overcommitted.

    reconciled_targets: optional {side_value → allowed_resting} from the
    reconciliation check. When provided, uses these authoritative targets
    instead of re-deriving from ledger (which may have a stale fill_gap).

    Returns a reduce-only ProposedRebalance (no catch-up) for the first
    overcommitted side found. After reduction, the resulting cross-side
    imbalance is handled by compute_rebalance_proposal in the next cycle.
    """
    for side in (Side.A, Side.B):
        filled = ledger.filled_count(side)
        resting = ledger.resting_count(side)
        filled_in_unit = filled % ledger.unit_size

        # Use reconciliation-derived target if available (authoritative —
        # computed from auth_fills which is max of all data sources).
        # Falls back to ledger-based fill_gap when no reconciled target.
        if reconciled_targets and side.value in reconciled_targets:
            allowed_resting = reconciled_targets[side.value]
        else:
            # Allow extra resting when it's closing a cross-side fill gap.
            # Without this, overcommit reduction cancels catch-up resting,
            # rebalance re-places it, overcommit re-cancels — infinite loop.
            other = Side.B if side == Side.A else Side.A
            fill_gap = max(0, ledger.filled_count(other) - filled)
            allowed_resting = max(ledger.unit_size - filled_in_unit, fill_gap)

        if resting <= allowed_resting:
            continue  # Not overcommitted (within unit cap or needed for fill gap)

        target_resting = allowed_resting
        order_id = ledger.resting_order_id(side)
        ticker = pair.ticker_a if side == Side.A else pair.ticker_b

        if order_id is None:
            continue  # No known order to reduce

        logger.warning(
            "overcommit_reduction",
            event_ticker=event_ticker,
            side=side.value,
            filled_in_unit=filled_in_unit,
            resting=resting,
            target_resting=target_resting,
            from_reconciliation=bool(
                reconciled_targets and side.value in reconciled_targets
            ),
        )

        # Resolve the Kalshi order side for the duplicate sweep
        kalshi_side = pair.side_a if side == Side.A else pair.side_b

        return ProposedRebalance(
            event_ticker=event_ticker,
            side=side.value,
            order_id=order_id,
            ticker=ticker,
            current_resting=resting,
            target_resting=target_resting,
            resting_price=ledger.resting_price(side),
            filled_count=filled,
            catchup_ticker=None,
            catchup_price=0,
            catchup_qty=0,
            reduce_side=kalshi_side,
        )

    return None


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

    # Skip top-up when the arb is clearly unprofitable — both sides will
    # fail (one "post only cross", the other "not profitable after fees"),
    # wasting API calls and rate-limit budget every cycle.
    if snapshot.fee_edge <= 0:
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
        # Pre-check profitability against historical fills so we don't
        # waste API calls on orders that is_placement_safe() will block.
        ok, _ = ledger.is_placement_safe(side, qty, price, rate=pair.fee_rate)
        if not ok:
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
    cancel_with_verify: CancelWithVerify,
    feed: MarketFeed | None = None,
    name: str = "",
) -> None:
    """Execute a two-step rebalance: reduce over-side, then catch up under-side.

    Step 1 (reduce) always runs before step 2 (catch-up) to maintain
    delta neutrality at every intermediate state.

    name: display name with Talos ID prefix (e.g. "#551 high temp LA").
    Prepended to all notifications so every activity log entry is traceable.
    """
    # Prefix all notifications with the display name
    _pfx = f"[{name}] " if name else ""

    def _notify(msg: str, sev: str) -> None:
        notify(f"{_pfx}{msg}", sev)


    # Resolve the pair to get api_event_ticker for Kalshi API calls
    pair = _find_pair(scanner, rebalance.event_ticker)
    api_event_ticker = pair.api_event_ticker if pair else rebalance.event_ticker

    # F36: cancel-discipline requires a pair for cancel_with_verify.
    # If we can't resolve a pair, skip the rebalance — the ledger sync
    # cycle will eventually correct the imbalance via other paths.
    if pair is None:
        logger.warning(
            "rebalance_skipped_no_pair",
            event_ticker=rebalance.event_ticker,
        )
        _notify("Rebalance SKIPPED: pair not found", "warning")
        return

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
                    api_event_ticker,
                    rebalance.ticker,
                    rebalance.order_id,
                    target_side=rebalance.reduce_side,
                    cancel_with_verify=cancel_with_verify,
                    pair=pair,
                )
                # Update ledger immediately so next cycle doesn't re-cancel
                try:
                    ledger = adjuster.get_ledger(rebalance.event_ticker)
                    with contextlib.suppress(ValueError):
                        ledger.record_cancel(Side(rebalance.side), rebalance.order_id)
                    # Register ALL cancelled IDs so sync_from_orders filters
                    # them out until Kalshi's GET confirms they're gone.
                    for oid in cancelled_ids:
                        ledger.mark_order_cancelled(oid)
                    ledger.mark_side_pending(Side(rebalance.side))
                except KeyError:
                    pass  # Ledger missing — sync will fix
                if cancelled_count > 0:
                    _notify(
                        f"Rebalance step 1: cancelled {cancelled_count}"
                        f" resting on {rebalance.side} ({rebalance.ticker})",
                        "information",
                    )
            else:
                # Use decrease_order for quantity-only reductions (preserves
                # queue position, simpler semantics than amend).
                fresh_order = await rest_client.get_order(rebalance.order_id)
                fresh_remaining = _order_remaining_contracts(fresh_order)
                if fresh_remaining <= rebalance.target_resting:
                    _notify(
                        f"Rebalance step 1: already at target"
                        f" (remaining={fresh_remaining})",
                        "information",
                    )
                else:
                    logger.info(
                        "rebalance_decrease",
                        order_id=rebalance.order_id,
                        order_remaining=fresh_remaining,
                        target_resting=rebalance.target_resting,
                    )
                    await rest_client.decrease_order(
                        rebalance.order_id,
                        reduce_to=rebalance.target_resting,
                    )
                    # Update ledger so next cycle sees reduced count.
                    # Use record_placement (not record_resting) to set
                    # _placed_at_gen — this activates the stale-sync guard
                    # so the next sync_from_orders won't overwrite the
                    # optimistic count with stale Kalshi data.
                    try:
                        ledger = adjuster.get_ledger(rebalance.event_ticker)
                        ledger.record_placement(
                            Side(rebalance.side),
                            rebalance.order_id,
                            rebalance.target_resting,
                            rebalance.resting_price,
                        )
                    except KeyError:
                        pass
                    _notify(
                        f"Rebalance step 1: {rebalance.side} resting"
                        f" {fresh_remaining}"
                        f" \u2192 {rebalance.target_resting}",
                        "information",
                    )
                # Sweep for duplicate orders on the same side/ticker.
                # The tracked order may already be at target, but extra
                # (orphan/double-bid) orders cause a persistent overcommit.
                await _cancel_duplicate_orders(
                    rest_client,
                    api_event_ticker,
                    rebalance.ticker,
                    rebalance.order_id,
                    target_side=rebalance.reduce_side,
                    notify=_notify,
                    adjuster=adjuster,
                    event_ticker=rebalance.event_ticker,
                    side=Side(rebalance.side),
                    cancel_with_verify=cancel_with_verify,
                    pair=pair,
                )
        except KalshiAPIError as e:
            if _is_no_op(e):
                # Order already at desired state (fills happened between
                # proposal and execution). Treat as success — proceed to
                # step 2.
                _notify(
                    "Rebalance step 1: already at target (no-op)",
                    "information",
                )
            else:
                _notify(f"Rebalance FAILED (reduce): {e}", "error")
                logger.exception(
                    "rebalance_reduce_error",
                    event_ticker=rebalance.event_ticker,
                    side=rebalance.side,
                    order_id=rebalance.order_id,
                )
                return  # Don't proceed to catch-up if reduce failed
        except Exception as e:
            _notify(
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
        if pair is None:
            _notify("Catch-up BLOCKED: pair not found", "error")
            return

        try:
            orders = await rest_client.get_all_orders(
                event_ticker=api_event_ticker,
            )
            ledger = adjuster.get_ledger(rebalance.event_ticker)
            ledger.sync_from_orders(orders, ticker_a=pair.ticker_a, ticker_b=pair.ticker_b)

            # Augment from positions API — orders API may have archived
            # older fills that sync_from_orders can't see (P7/P15).
            positions = await rest_client.get_all_positions(
                event_ticker=api_event_ticker,
            )
            pos_map = {p.ticker: p for p in positions}
            pos_a = pos_map.get(pair.ticker_a)
            pos_b = pos_map.get(pair.ticker_b)
            if pos_a or pos_b:
                ledger.sync_from_positions(
                    {
                        Side.A: abs(pos_a.position) if pos_a else 0,
                        Side.B: abs(pos_b.position) if pos_b else 0,
                    },
                    {
                        Side.A: pos_a.total_traded if pos_a else 0,
                        Side.B: pos_b.total_traded if pos_b else 0,
                    },
                    {
                        Side.A: pos_a.fees_paid if pos_a else 0,
                        Side.B: pos_b.fees_paid if pos_b else 0,
                    },
                )
        except Exception as e:
            logger.warning(
                "rebalance_fresh_sync_failed",
                event_ticker=rebalance.event_ticker,
                exc_info=True,
            )
            _notify(
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
            _notify(
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

        # Cap catch-up price at the live best ask to avoid "post only cross"
        # errors, but never INCREASE above the proposal price.  The proposal
        # may have computed a max-profitable fallback (e.g. 1¢) — inflating
        # that to the market ask (e.g. 55¢) defeats the fallback and causes
        # perpetual "arb not profitable" blocks.
        catchup_price = rebalance.catchup_price
        if feed is not None and rebalance.catchup_ticker:
            catchup_side_str = rebalance.catchup_side or "no"
            try:
                fresh_level = feed.book_manager.best_ask(
                    rebalance.catchup_ticker, side=catchup_side_str
                )
                if fresh_level is not None:
                    # Prefer exact-precision price_bps; fall back to legacy
                    # cents for fixtures that only populate the legacy field.
                    fresh_bps = (
                        fresh_level.price_bps
                        if fresh_level.price_bps
                        else cents_to_bps(fresh_level.price)
                    )
                    if fresh_bps > 0:
                        fresh_price = bps_to_cents_round(fresh_bps)
                        catchup_price = min(catchup_price, fresh_price)
            except Exception:
                pass  # Fall back to proposal price

        # Safety gate — same checks as place_bids (P16, P18).
        # catchup=True bypasses P16 unit boundary (risk-reducing, not speculative).
        ok, reason = ledger.is_placement_safe(
            under_side,
            fresh_catchup_qty,
            catchup_price,
            rate=pair.fee_rate,
            catchup=True,
        )
        if not ok:
            _notify(
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
            created = await rest_client.create_order(
                ticker=rebalance.catchup_ticker,
                action="buy",
                side=rebalance.catchup_side,
                yes_price=catchup_price if rebalance.catchup_side == "yes" else None,
                no_price=catchup_price if rebalance.catchup_side == "no" else None,
                count=fresh_catchup_qty,
                order_group_id=catchup_group,
            )
            # Record in ledger immediately — prevents another imbalance pass
            # from reproposing catch-up before the next poll picks it up.
            ledger.record_placement(
                under_side,
                order_id=created.order_id,
                count=_order_remaining_contracts(created),
                price=catchup_price,
            )
            _notify(
                f"Rebalance step 2: catch-up {rebalance.catchup_ticker}"
                f" {fresh_catchup_qty} @ {catchup_price}c",
                "information",
            )
            logger.info(
                "rebalance_catchup_placed",
                event_ticker=rebalance.event_ticker,
                ticker=rebalance.catchup_ticker,
                qty=fresh_catchup_qty,
                price=catchup_price,
            )
        except Exception as e:
            _notify(
                f"Catch-up FAILED: {type(e).__name__}: {e}",
                "error",
            )
            logger.exception(
                "rebalance_catchup_error",
                event_ticker=rebalance.event_ticker,
                ticker=rebalance.catchup_ticker,
            )
            # Stale local orderbook — resubscribe for fresh snapshot
            if (
                feed is not None
                and isinstance(e, KalshiAPIError)
                and "post only cross" in str(e).lower()
                and rebalance.catchup_ticker
            ):
                await feed.unsubscribe(rebalance.catchup_ticker)
                await feed.subscribe(rebalance.catchup_ticker)


# ── Helpers ──────────────────────────────────────────────────────────


async def _cancel_all_resting(
    rest_client: KalshiRESTClient,
    event_ticker: str,
    ticker: str,
    primary_order_id: str,
    *,
    cancel_with_verify: CancelWithVerify,
    pair: ArbPair,
    target_side: str = "no",
) -> tuple[int, list[str]]:
    """Cancel all resting buy orders on the target side for a specific ticker.

    First cancels the primary order_id (from the proposal), then fetches
    all orders for the event and cancels any other resting orders on the
    same ticker. Returns (total_contracts_cancelled, list_of_order_ids).

    F36: all cancels route through ``cancel_with_verify`` (the engine's
    :meth:`TradingEngine.cancel_order_with_verify`) so F33 resync runs
    on a 404 instead of a blind optimistic-clear.
    """
    total_cancelled = 0
    cancelled_ids: list[str] = []
    primary_cancelled = False

    # Cancel the primary order first
    try:
        await cancel_with_verify(primary_order_id, pair)
        cancelled_ids.append(primary_order_id)
        primary_cancelled = True
    except KalshiRateLimitError:
        raise  # Propagate — caller should retry next cycle
    except KalshiAPIError:
        pass  # Already cancelled or not found — continue to sweep

    # Sweep: fetch all resting orders for this event, cancel any on our ticker
    primary_counted = False
    try:
        orders = await rest_client.get_all_orders(
            event_ticker=event_ticker,
            status="resting",
        )
        for order in orders:
            if order.ticker != ticker:
                continue
            order_remaining_contracts = _order_remaining_contracts(order)
            if order.order_id == primary_order_id:
                total_cancelled += order_remaining_contracts
                primary_counted = True
                continue  # Already cancelled above
            if order.side != target_side or order.action != "buy":
                continue
            if order_remaining_contracts <= 0:
                continue
            try:
                await cancel_with_verify(order.order_id, pair)
                total_cancelled += order_remaining_contracts
                cancelled_ids.append(order.order_id)
                logger.info(
                    "orphan_order_cancelled",
                    event_ticker=event_ticker,
                    ticker=ticker,
                    order_id=order.order_id,
                    remaining=order_remaining_contracts,
                )
            except KalshiRateLimitError:
                raise  # Propagate — don't silently skip resting orders
            except KalshiAPIError:
                pass  # Best effort
    except KalshiRateLimitError:
        raise  # Propagate — caller must know cancel was incomplete
    except Exception:
        logger.warning(
            "orphan_sweep_failed",
            event_ticker=event_ticker,
            ticker=ticker,
            exc_info=True,
        )

    # If the primary was cancelled but the GET sweep didn't see it
    # (Kalshi eventual consistency), count at least 1 contract.
    if primary_cancelled and not primary_counted:
        total_cancelled += 1

    return total_cancelled, cancelled_ids


async def _cancel_duplicate_orders(
    rest_client: KalshiRESTClient,
    api_event_ticker: str,
    ticker: str,
    keep_order_id: str,
    target_side: str,
    *,
    notify: Callable[[str, str], None],
    adjuster: BidAdjuster,
    event_ticker: str,
    side: Side,
    cancel_with_verify: CancelWithVerify,
    pair: ArbPair,
) -> None:
    """Cancel any resting orders on ticker EXCEPT the one we want to keep.

    Handles the double-bid scenario: two separate orders exist on the same
    side, each at qty 1 with unit_size=1.  decrease_order on the kept order
    is a no-op, so the duplicate must be explicitly swept.

    F36: all cancels route through ``cancel_with_verify`` so F33 resync
    runs on a 404 instead of a blind optimistic-clear.
    """
    try:
        orders = await rest_client.get_all_orders(
            event_ticker=api_event_ticker,
            status="resting",
        )
    except KalshiRateLimitError:
        raise  # Propagate — caller must know sweep was incomplete
    except Exception:
        logger.warning(
            "duplicate_sweep_fetch_failed",
            event_ticker=event_ticker,
            ticker=ticker,
            exc_info=True,
        )
        return

    cancelled = 0
    for order in orders:
        if order.ticker != ticker:
            continue
        if order.order_id == keep_order_id:
            continue
        if order.side != target_side or order.action != "buy":
            continue
        order_remaining_contracts = _order_remaining_contracts(order)
        if order_remaining_contracts <= 0:
            continue
        try:
            await cancel_with_verify(order.order_id, pair)
            cancelled += order_remaining_contracts
            logger.info(
                "duplicate_order_cancelled",
                event_ticker=event_ticker,
                ticker=ticker,
                order_id=order.order_id,
                remaining=order_remaining_contracts,
            )
            # Register so sync_from_orders filters it out
            try:
                ledger = adjuster.get_ledger(event_ticker)
                ledger.mark_order_cancelled(order.order_id)
            except (KeyError, ValueError):
                pass
        except KalshiRateLimitError:
            raise  # Propagate — don't silently skip duplicates
        except KalshiAPIError:
            pass  # Best effort

    if cancelled > 0:
        notify(
            f"Rebalance: cancelled {cancelled} duplicate on {side.value} ({ticker})",
            "information",
        )


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
