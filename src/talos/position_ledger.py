"""PositionLedger — pure state machine for per-event position tracking.

Single source of truth for filled counts, resting orders, avg prices,
and safety gates. One instance per active event. No I/O, no async.

See brain/principles.md Principles 15-19 for safety invariants.

Units
-----
Internal state is stored in exact-precision bps/fp100 (see talos.units):
  - bps: integer basis points of a dollar. $1 = 10_000 bps, 1¢ = 100 bps.
  - fp100: integer hundredths of a contract. 1 contract = 100 fp100.

The legacy accessors (filled_count, filled_total_cost, resting_count,
resting_price, filled_fees) KEEP their pre-migration names and return
cents/contracts (floor/half-even). Parallel ``_bps``/``_fp100`` accessors
expose the raw internal state for callers that need exact precision.

Legacy mutators (record_fill, record_resting, record_placement) still
accept cents/contracts and forward to exact-precision ``_bps`` variants
after ×100 scaling. New mutators (record_fill_bps, record_resting_bps,
record_placement_bps) accept bps/fp100 directly — the path the sub-cent
and fractional-fill wire-boundary parsers will feed into.
"""

from __future__ import annotations

from collections.abc import Mapping
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
from talos.units import (
    ONE_CENT_BPS,
    ONE_CONTRACT_FP100,
    bps_to_cents_round,
)

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
    """Mutable per-side position state (bps/fp100 internal units)."""

    __slots__ = (
        "filled_count_fp100",
        "filled_total_cost_bps",
        "filled_fees_bps",
        "closed_count_fp100",
        "closed_total_cost_bps",
        "closed_fees_bps",
        "_fees_from_api",
        "resting_order_id",
        "resting_count_fp100",
        "resting_price_bps",
        "_placed_at_gen",
    )

    def __init__(self) -> None:
        self.filled_count_fp100: int = 0
        self.filled_total_cost_bps: int = 0
        self.filled_fees_bps: int = 0
        self.closed_count_fp100: int = 0
        self.closed_total_cost_bps: int = 0
        self.closed_fees_bps: int = 0
        self._fees_from_api: bool = False
        self.resting_order_id: str | None = None
        self.resting_count_fp100: int = 0
        self.resting_price_bps: int = 0
        self._placed_at_gen: int | None = None

    def reset(self) -> None:
        self.filled_count_fp100 = 0
        self.filled_total_cost_bps = 0
        self.filled_fees_bps = 0
        self.closed_count_fp100 = 0
        self.closed_total_cost_bps = 0
        self.closed_fees_bps = 0
        self._fees_from_api = False
        self.resting_order_id = None
        self.resting_count_fp100 = 0
        self.resting_price_bps = 0
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
        # unit_size stays in whole contracts — it's an operator-facing control.
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

    # ── Per-side accessors (legacy cents/contracts return) ──────────

    def filled_count(self, side: Side) -> int:
        """Legacy accessor — whole contracts (floor of fp100)."""
        return self._sides[side].filled_count_fp100 // ONE_CONTRACT_FP100

    def filled_total_cost(self, side: Side) -> int:
        """Legacy accessor — total cost in cents (half-even round from bps)."""
        return bps_to_cents_round(self._sides[side].filled_total_cost_bps)

    def resting_order_id(self, side: Side) -> str | None:
        return self._sides[side].resting_order_id

    def resting_count(self, side: Side) -> int:
        """Legacy accessor — whole contracts (floor of fp100)."""
        return self._sides[side].resting_count_fp100 // ONE_CONTRACT_FP100

    def resting_price(self, side: Side) -> int:
        """Legacy accessor — cents (half-even round from bps)."""
        return bps_to_cents_round(self._sides[side].resting_price_bps)

    def filled_fees(self, side: Side) -> int:
        """Legacy accessor — fees in cents (half-even round from bps)."""
        return bps_to_cents_round(self._sides[side].filled_fees_bps)

    # ── Per-side accessors (exact-precision bps/fp100 return) ───────

    def filled_count_fp100(self, side: Side) -> int:
        return self._sides[side].filled_count_fp100

    def filled_total_cost_bps(self, side: Side) -> int:
        return self._sides[side].filled_total_cost_bps

    def filled_fees_bps(self, side: Side) -> int:
        return self._sides[side].filled_fees_bps

    def closed_count_fp100(self, side: Side) -> int:
        return self._sides[side].closed_count_fp100

    def closed_total_cost_bps(self, side: Side) -> int:
        return self._sides[side].closed_total_cost_bps

    def closed_fees_bps(self, side: Side) -> int:
        return self._sides[side].closed_fees_bps

    def resting_count_fp100(self, side: Side) -> int:
        return self._sides[side].resting_count_fp100

    def resting_price_bps(self, side: Side) -> int:
        return self._sides[side].resting_price_bps

    # ── Derived queries ─────────────────────────────────────────────

    def avg_filled_price(self, side: Side) -> float:
        """Legacy accessor — avg price in cents per contract (float).

        ONE_CONTRACT_FP100 (100) / ONE_CENT_BPS (100) cancel, so the ratio
        reduces to ``filled_total_cost_bps / filled_count_fp100``.
        """
        s = self._sides[side]
        if s.filled_count_fp100 == 0:
            return 0.0
        return s.filled_total_cost_bps / s.filled_count_fp100

    def avg_filled_price_bps(self, side: Side) -> float:
        """Exact-precision avg price in bps per whole contract (float)."""
        s = self._sides[side]
        if s.filled_count_fp100 == 0:
            return 0.0
        return s.filled_total_cost_bps * ONE_CONTRACT_FP100 / s.filled_count_fp100

    def open_count(self, side: Side) -> int:
        """Legacy accessor — open fills in whole contracts (floor)."""
        s = self._sides[side]
        return (s.filled_count_fp100 - s.closed_count_fp100) // ONE_CONTRACT_FP100

    def open_avg_filled_price(self, side: Side) -> float:
        """Average fill price of the currently-open unit on this side.

        Returns 0.0 when the open unit has no fills (fresh position or
        immediately after a matched-pair close). Decision-path callers
        (P18 profitability checks) must use this, NOT avg_filled_price —
        closed units should not influence decisions about the open unit.

        Cents-per-contract return (legacy semantics preserved by the
        ONE_CONTRACT_FP100 / ONE_CENT_BPS cancellation).
        """
        s = self._sides[side]
        open_count_fp100 = s.filled_count_fp100 - s.closed_count_fp100
        if open_count_fp100 <= 0:
            return 0.0
        open_cost_bps = s.filled_total_cost_bps - s.closed_total_cost_bps
        return open_cost_bps / open_count_fp100

    def total_committed(self, side: Side) -> int:
        """Legacy accessor — filled + resting in whole contracts (floor)."""
        s = self._sides[side]
        total_fp100 = s.filled_count_fp100 + s.resting_count_fp100
        return total_fp100 // ONE_CONTRACT_FP100

    def current_delta(self) -> int:
        return abs(self.total_committed(Side.A) - self.total_committed(Side.B))

    def unit_remaining(self, side: Side) -> int:
        """Remaining whole contracts to fill the current unit on this side."""
        s = self._sides[side]
        filled_whole = s.filled_count_fp100 // ONE_CONTRACT_FP100
        filled_in_unit = filled_whole % self.unit_size
        if filled_in_unit == 0 and filled_whole > 0:
            return 0  # unit is complete
        return self.unit_size - filled_in_unit

    def is_unit_complete(self, side: Side) -> bool:
        s = self._sides[side]
        filled_whole = s.filled_count_fp100 // ONE_CONTRACT_FP100
        return filled_whole > 0 and filled_whole % self.unit_size == 0

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

        ``count`` and ``price`` are whole contracts and cents (legacy
        signature — sub-cent/fractional callers should use the exact
        path added in a later task and are not yet wired to the gate).

        When ``catchup=True`` the P16 unit-boundary check is skipped because
        catch-up orders close an existing imbalance (risk-reducing, not
        speculative). P18 profitability is always enforced.
        """
        s = self._sides[side]
        filled_whole = s.filled_count_fp100 // ONE_CONTRACT_FP100
        resting_whole = s.resting_count_fp100 // ONE_CONTRACT_FP100

        # P16: resting + filled-in-unit + new must not exceed unit.
        # Modular arithmetic allows re-entry after a complete unit (10/10 → next pair).
        # Skipped for catch-up orders — closing a gap, not speculative exposure.
        if not catchup:
            filled_in_unit = filled_whole % self.unit_size
            if filled_in_unit + resting_whole + count > self.unit_size:
                return (
                    False,
                    f"would exceed unit: filled_in_unit={filled_in_unit} + "
                    f"resting={resting_whole} + new={count} > {self.unit_size}",
                )

        # P18: fee-adjusted profitability (open-unit scoped — matched pairs
        # are locked in and must not subsidize decisions about the open unit).
        other = self._sides[side.other]
        other_open_fp100 = other.filled_count_fp100 - other.closed_count_fp100
        if other_open_fp100 > 0:
            # open_avg_filled_price returns cents-per-contract (float).
            other_price = self.open_avg_filled_price(side.other)
        elif other.resting_count_fp100 > 0:
            other_price = bps_to_cents_round(other.resting_price_bps)
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

    # ── Open-unit reconciliation ────────────────────────────────────

    def _reconcile_closed(self, *, path: str = "fill") -> None:
        """Flush newly-matched pairs from the open bucket into the closed bucket.

        Idempotent: safe to call multiple times. If no new units can close,
        returns without mutation.

        Must be invoked after ANY mutation that increases filled_count_fp100 or
        filled_total_cost_bps. See the invariant in the open-unit avg scoping
        spec (docs/superpowers/specs/2026-04-15-open-unit-avg-scoping-design.md).

        ``path`` identifies the caller for the paper-trail log (fill,
        sync_orders, sync_positions, seed_from_saved). Matters operationally
        for distinguishing FIFO-clean reconciliation (fill) from restart-
        time blend-approximation (seed_from_saved on a migration path).

        Operates in fp100/bps internally; unit_size is in whole contracts,
        so the matchable comparison floors through ONE_CONTRACT_FP100.
        """
        a = self._sides[Side.A]
        b = self._sides[Side.B]
        open_a_fp100 = a.filled_count_fp100 - a.closed_count_fp100
        open_b_fp100 = b.filled_count_fp100 - b.closed_count_fp100
        matchable_whole = min(open_a_fp100, open_b_fp100) // ONE_CONTRACT_FP100
        units_to_close = matchable_whole // self.unit_size
        if units_to_close == 0:
            return
        contracts_whole = units_to_close * self.unit_size
        contracts_fp100 = contracts_whole * ONE_CONTRACT_FP100
        for side_state in (a, b):
            open_count_fp100 = side_state.filled_count_fp100 - side_state.closed_count_fp100
            open_cost_bps = side_state.filled_total_cost_bps - side_state.closed_total_cost_bps
            open_fees_bps = side_state.filled_fees_bps - side_state.closed_fees_bps
            side_state.closed_count_fp100 += contracts_fp100
            side_state.closed_total_cost_bps += round(
                open_cost_bps * contracts_fp100 / open_count_fp100
            )
            side_state.closed_fees_bps += round(
                open_fees_bps * contracts_fp100 / open_count_fp100
            )
        logger.info(
            "ledger_reconciled_closed",
            event_ticker=self.event_ticker,
            path=path,
            units_closed=units_to_close,
            contracts=contracts_whole,
            open_a=(a.filled_count_fp100 - a.closed_count_fp100) // ONE_CONTRACT_FP100,
            open_b=(b.filled_count_fp100 - b.closed_count_fp100) // ONE_CONTRACT_FP100,
            avg_a=self.open_avg_filled_price(Side.A),
            avg_b=self.open_avg_filled_price(Side.B),
        )

    # ── State mutations ─────────────────────────────────────────────

    def record_fill(self, side: Side, count: int, price: int, *, fees: int = 0) -> None:
        """Record a fill (legacy cents/contracts signature).

        count: whole contracts; price: cents; fees: cents. Scales ×100 and
        forwards to record_fill_bps, so the exact-precision recorder is
        the single underlying writer.
        """
        self.record_fill_bps(
            side,
            count_fp100=count * ONE_CONTRACT_FP100,
            price_bps=price * ONE_CENT_BPS,
            fees_bps=fees * ONE_CENT_BPS,
        )

    def record_fill_bps(
        self,
        side: Side,
        *,
        count_fp100: int,
        price_bps: int,
        fees_bps: int = 0,
    ) -> None:
        """Exact-precision fill recorder.

        ``count_fp100`` and ``price_bps`` may encode sub-cent / fractional
        values without loss. Cost in bps is ``count_fp100 * price_bps /
        ONE_CONTRACT_FP100`` (a 100-fp100 contract at 5300 bps contributes
        100 * 5300 / 100 = 5300 bps cost — the expected per-contract total).
        """
        s = self._sides[side]
        s.filled_count_fp100 += count_fp100
        s.filled_total_cost_bps += (count_fp100 * price_bps) // ONE_CONTRACT_FP100
        if fees_bps > 0:
            s.filled_fees_bps += fees_bps
        # If resting order filled partially/fully, reduce resting count
        if s.resting_count_fp100 > 0:
            filled_from_resting = min(count_fp100, s.resting_count_fp100)
            s.resting_count_fp100 -= filled_from_resting
            if s.resting_count_fp100 == 0:
                s.resting_order_id = None
        self._reconcile_closed(path="fill")

    def record_resting(self, side: Side, order_id: str, count: int, price: int) -> None:
        """Record a new resting order (legacy cents/contracts signature)."""
        self.record_resting_bps(
            side,
            order_id=order_id,
            count_fp100=count * ONE_CONTRACT_FP100,
            price_bps=price * ONE_CENT_BPS,
        )

    def record_resting_bps(
        self,
        side: Side,
        *,
        order_id: str,
        count_fp100: int,
        price_bps: int,
    ) -> None:
        """Exact-precision resting recorder."""
        s = self._sides[side]
        s.resting_order_id = order_id
        s.resting_count_fp100 = count_fp100
        s.resting_price_bps = price_bps

    def record_placement(self, side: Side, order_id: str, count: int, price: int) -> None:
        """Record optimistic resting state (legacy cents/contracts signature).

        Like record_resting, but marks the order as unconfirmed so that
        sync_from_orders won't clear it if given stale data from a poll
        that started before the order was created.
        """
        self.record_placement_bps(
            side,
            order_id=order_id,
            count_fp100=count * ONE_CONTRACT_FP100,
            price_bps=price * ONE_CENT_BPS,
        )

    def record_placement_bps(
        self,
        side: Side,
        *,
        order_id: str,
        count_fp100: int,
        price_bps: int,
    ) -> None:
        """Exact-precision placement recorder."""
        s = self._sides[side]
        s.resting_order_id = order_id
        s.resting_count_fp100 = count_fp100
        s.resting_price_bps = price_bps
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
        s.resting_count_fp100 = 0
        s.resting_price_bps = 0
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

    def to_save_dict(self) -> dict[str, object]:
        """Export full ledger state for persistence (v2 envelope).

        Returns::

            {
              "schema_version": 2,
              "ledger": { <bps/fp100 fields> },
            }

        The outer ``_persist_games`` writer nests this under an ``entry["ledger"]``
        key, so the full on-disk shape is ``entry["ledger"]["ledger"]["<field>"]``.
        That nesting is intentional: ``schema_version`` sits at the top of the
        envelope so ``seed_from_saved`` can detect v2 without reaching into the
        payload. Legacy v1 payloads (cents/contracts, flat shape with
        ``filled_a``/``cost_a``/... keys) are still accepted by ``seed_from_saved``.
        """
        a = self._sides[Side.A]
        b = self._sides[Side.B]
        return {
            "schema_version": 2,
            "ledger": {
                "filled_count_fp100_a": a.filled_count_fp100,
                "filled_total_cost_bps_a": a.filled_total_cost_bps,
                "filled_fees_bps_a": a.filled_fees_bps,
                "closed_count_fp100_a": a.closed_count_fp100,
                "closed_total_cost_bps_a": a.closed_total_cost_bps,
                "closed_fees_bps_a": a.closed_fees_bps,
                "resting_id_a": a.resting_order_id,
                "resting_count_fp100_a": a.resting_count_fp100,
                "resting_price_bps_a": a.resting_price_bps,
                "filled_count_fp100_b": b.filled_count_fp100,
                "filled_total_cost_bps_b": b.filled_total_cost_bps,
                "filled_fees_bps_b": b.filled_fees_bps,
                "closed_count_fp100_b": b.closed_count_fp100,
                "closed_total_cost_bps_b": b.closed_total_cost_bps,
                "closed_fees_bps_b": b.closed_fees_bps,
                "resting_id_b": b.resting_order_id,
                "resting_count_fp100_b": b.resting_count_fp100,
                "resting_price_bps_b": b.resting_price_bps,
            },
        }

    def seed_from_saved(self, data: Mapping[str, object] | None) -> None:
        """Seed full ledger state from persisted data (v1 or v2).

        Detects the persistence schema by presence of ``schema_version``:

          * v2 (``schema_version == 2``): inner ``ledger`` payload already
            in bps/fp100 — load verbatim. Closed-bucket atomic validation
            is unnecessary (the entire envelope is our own output, written
            atomically as a unit).
          * v1 (flat ``filled_a`` / ``cost_a`` / ... — no ``schema_version``):
            cents/contracts — scale ×100 into bps/fp100, preserving the
            strict closed-bucket atomic-group validation that guards against
            corruption in the pre-migration layout.

        Post-load, the terminal ``_reconcile_closed`` call mirrors the
        pre-migration behaviour: idempotent no-op in the normal-restart
        case (quiet save point), pro-rata migration in the v1 path when
        closed_* keys are missing.
        """
        if not data:
            return

        # ── v2 path ────────────────────────────────────────────────
        if data.get("schema_version") == 2:
            ledger_payload = data.get("ledger")
            if not isinstance(ledger_payload, dict):
                logger.warning(
                    "ledger_v2_payload_missing",
                    event_ticker=self.event_ticker,
                )
                return
            a = self._sides[Side.A]
            b = self._sides[Side.B]
            for side_state, prefix in ((a, "a"), (b, "b")):
                side_state.filled_count_fp100 = int(
                    ledger_payload.get(f"filled_count_fp100_{prefix}", 0) or 0
                )
                side_state.filled_total_cost_bps = int(
                    ledger_payload.get(f"filled_total_cost_bps_{prefix}", 0) or 0
                )
                side_state.filled_fees_bps = int(
                    ledger_payload.get(f"filled_fees_bps_{prefix}", 0) or 0
                )
                side_state.closed_count_fp100 = int(
                    ledger_payload.get(f"closed_count_fp100_{prefix}", 0) or 0
                )
                side_state.closed_total_cost_bps = int(
                    ledger_payload.get(f"closed_total_cost_bps_{prefix}", 0) or 0
                )
                side_state.closed_fees_bps = int(
                    ledger_payload.get(f"closed_fees_bps_{prefix}", 0) or 0
                )
                saved_id = ledger_payload.get(f"resting_id_{prefix}")
                saved_count_fp100 = int(
                    ledger_payload.get(f"resting_count_fp100_{prefix}", 0) or 0
                )
                saved_price_bps = int(
                    ledger_payload.get(f"resting_price_bps_{prefix}", 0) or 0
                )
                if saved_id and saved_count_fp100 > 0:
                    side_state.resting_order_id = str(saved_id)
                    side_state.resting_count_fp100 = saved_count_fp100
                    side_state.resting_price_bps = saved_price_bps
            logger.info(
                "ledger_restored_v2",
                event_ticker=self.event_ticker,
            )
            # Terminal reconcile — idempotent in normal-restart case.
            self._reconcile_closed(path="seed_from_saved")
            return

        # ── v1 path (legacy cents/contracts) ───────────────────────
        def _coerce_int(v: object) -> int:
            """Best-effort int coercion for loose v1 payload values."""
            if isinstance(v, int) and not isinstance(v, bool):
                return v
            if isinstance(v, str):
                try:
                    return int(v)
                except ValueError:
                    return 0
            return 0

        a = self._sides[Side.A]
        b = self._sides[Side.B]
        for side, prefix in [(a, "a"), (b, "b")]:
            saved_fills = data.get(f"filled_{prefix}", 0)
            saved_cost = data.get(f"cost_{prefix}", 0)
            saved_fees = data.get(f"fees_{prefix}", 0)
            saved_fills_fp100 = (
                saved_fills * ONE_CONTRACT_FP100
                if isinstance(saved_fills, int) and not isinstance(saved_fills, bool)
                else 0
            )
            if (
                isinstance(saved_fills, int)
                and not isinstance(saved_fills, bool)
                and saved_fills_fp100 > side.filled_count_fp100
            ):
                logger.info(
                    "ledger_seeded_from_saved",
                    event_ticker=self.event_ticker,
                    side=prefix.upper(),
                    saved_fills=saved_fills,
                    current_fills=side.filled_count_fp100 // ONE_CONTRACT_FP100,
                )
                side.filled_count_fp100 = saved_fills * ONE_CONTRACT_FP100
                side.filled_total_cost_bps = max(
                    side.filled_total_cost_bps,
                    _coerce_int(saved_cost) * ONE_CENT_BPS,
                )
                side.filled_fees_bps = max(
                    side.filled_fees_bps,
                    _coerce_int(saved_fees) * ONE_CENT_BPS,
                )

            # Restore resting state
            saved_id = data.get(f"resting_id_{prefix}")
            saved_count = data.get(f"resting_count_{prefix}", 0)
            saved_price = data.get(f"resting_price_{prefix}", 0)
            if (
                saved_id
                and isinstance(saved_count, int)
                and not isinstance(saved_count, bool)
                and saved_count > 0
            ):
                side.resting_order_id = str(saved_id)
                side.resting_count_fp100 = saved_count * ONE_CONTRACT_FP100
                side.resting_price_bps = _coerce_int(saved_price) * ONE_CENT_BPS

        # Atomic-group restore of the closed_* bucket with strict validation.
        required_closed_keys = (
            "closed_count_a", "closed_total_cost_a", "closed_fees_a",
            "closed_count_b", "closed_total_cost_b", "closed_fees_b",
        )

        def _valid_closed_value(v: object) -> bool:
            # Require exact int — reject bool (subclass of int), float, str, None, negative.
            return type(v) is int and v >= 0

        missing: list[str] = []
        invalid: list[str] = []
        for k in required_closed_keys:
            if k not in data:
                missing.append(k)
            elif not _valid_closed_value(data[k]):
                invalid.append(k)

        if missing or invalid:
            # Migration fallback: zero all six; terminal reconcile populates from blend.
            for side_state in (a, b):
                side_state.closed_count_fp100 = 0
                side_state.closed_total_cost_bps = 0
                side_state.closed_fees_bps = 0
            logger.info(
                "ledger_migrated_missing_closed",
                event_ticker=self.event_ticker,
                missing_keys=missing,
                invalid_keys=invalid,
            )
        else:
            # Normal v1 restart: restore verbatim (×100 scaled). Values validated above.
            for side_state, prefix in [(a, "a"), (b, "b")]:
                cc_raw = int(data[f"closed_count_{prefix}"])  # type: ignore[arg-type]
                ctc_raw = int(data[f"closed_total_cost_{prefix}"])  # type: ignore[arg-type]
                cf_raw = int(data[f"closed_fees_{prefix}"])  # type: ignore[arg-type]
                side_state.closed_count_fp100 = cc_raw * ONE_CONTRACT_FP100
                side_state.closed_total_cost_bps = ctc_raw * ONE_CENT_BPS
                side_state.closed_fees_bps = cf_raw * ONE_CENT_BPS
            logger.info(
                "ledger_restored_with_closed",
                event_ticker=self.event_ticker,
            )

        # Terminal reconcile — idempotent no-op in normal-restart case (save
        # should have been taken at a quiet point), populates from blend in
        # migration case. Required by the spec invariant: every mutation that
        # increases filled_count must reconcile.
        self._reconcile_closed(path="seed_from_saved")

    def sync_from_orders(self, orders: list, ticker_a: str, ticker_b: str) -> None:
        """Reconcile ledger against polled order state from Kalshi.

        Fill counts: monotonically increasing — the orders API archives old
        filled/cancelled orders, so it may report fewer fills than the
        positions API has already set. We never decrease fills (P7/P15).

        Resting orders: authoritative — summed across all active orders per
        side to support multiple resting orders on the same side.

        Called every polling cycle. See also sync_from_positions() which
        patches fill gaps from the positions API.

        Wire-shape note: the orders API still delivers whole-cent prices and
        whole-contract counts in the current deployment. We scale ×100 at
        this boundary into bps/fp100. When sub-cent/fractional markets come
        online the wire parser — not this method — gains a bps/fp100 path.
        """
        # Build mapping: same-ticker pairs use order.side, cross-ticker uses ticker
        if self._is_same_ticker:
            side_map: dict[str, Side] | None = {self._side_a_str: Side.A, self._side_b_str: Side.B}
        else:
            side_map = None
        ticker_to_side = {ticker_a: Side.A, ticker_b: Side.B}

        # Accumulators in exact-precision units.
        kalshi_filled_fp100: dict[Side, int] = {Side.A: 0, Side.B: 0}
        kalshi_fill_cost_bps: dict[Side, int] = {Side.A: 0, Side.B: 0}
        kalshi_fees_bps: dict[Side, int] = {Side.A: 0, Side.B: 0}
        # Resting list items: (order_id, count_fp100, price_bps)
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
                kalshi_filled_fp100[side] += order.fill_count * ONE_CONTRACT_FP100
                kalshi_fill_cost_bps[side] += (
                    (order.maker_fill_cost + order.taker_fill_cost) * ONE_CENT_BPS
                )
                kalshi_fees_bps[side] += order.maker_fees * ONE_CENT_BPS
            # Only track resting from active orders — skip recently cancelled
            # IDs that Kalshi's GET may still return due to eventual consistency
            if order.remaining_count > 0 and order.status in ("resting", "executed"):
                if order.order_id in self._recently_cancelled:
                    continue
                # Use correct price field based on order side
                resting_price_cents = order.no_price if order.side == "no" else order.yes_price
                kalshi_resting[side].append(
                    (
                        order.order_id,
                        order.remaining_count * ONE_CONTRACT_FP100,
                        resting_price_cents * ONE_CENT_BPS,
                    )
                )

        for side in (Side.A, Side.B):
            s = self._sides[side]

            # Fills: only increase. Orders API archives old orders, so
            # kalshi_filled may be lower than positions-augmented fills.
            # When orders reports >= current, use its data (more detailed
            # cost/fee breakdown). When less, keep existing.
            if (
                kalshi_filled_fp100[side] >= s.filled_count_fp100
                and kalshi_filled_fp100[side] > 0
            ):
                s.filled_count_fp100 = kalshi_filled_fp100[side]
                s.filled_total_cost_bps = kalshi_fill_cost_bps[side]
                s.filled_fees_bps = kalshi_fees_bps[side]
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
                total_resting_fp100 = sum(cnt for _, cnt, _ in resting_list)
                s.resting_order_id = resting_list[0][0]
                s.resting_count_fp100 = total_resting_fp100
                s.resting_price_bps = resting_list[0][2]
                s._placed_at_gen = None  # Confirmed by sync
                if len(resting_list) > 1:
                    logger.info(
                        "multiple_resting_orders_summed",
                        event_ticker=self.event_ticker,
                        side=side.value,
                        order_count=len(resting_list),
                        total_resting=total_resting_fp100 // ONE_CONTRACT_FP100,
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
                s.resting_count_fp100 = 0
                s.resting_price_bps = 0
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
        self._reconcile_closed(path="sync_orders")

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

        ``position_fills`` (contracts) and ``position_costs`` (cents) are the
        legacy whole-unit shape; we scale ×100 into fp100/bps at the boundary.
        """
        if self._is_same_ticker:
            return  # Positions API reports net, useless for YES/NO pairs
        if position_fees is None:
            position_fees = {Side.A: 0, Side.B: 0}

        for side in (Side.A, Side.B):
            s = self._sides[side]
            auth_fills_fp100 = position_fills[side] * ONE_CONTRACT_FP100

            if auth_fills_fp100 > s.filled_count_fp100:
                logger.warning(
                    "fills_augmented_from_positions_api",
                    event_ticker=self.event_ticker,
                    side=side.value,
                    ledger_fills=s.filled_count_fp100 // ONE_CONTRACT_FP100,
                    positions_fills=position_fills[side],
                )
                s.filled_count_fp100 = auth_fills_fp100

            # Positions API cost is authoritative when fills were augmented
            # (orders API archived the filled orders). Also use it when
            # orders-based cost is zero or when positions reports higher
            # cost (more complete data from un-archived source).
            pos_cost_bps = position_costs[side] * ONE_CENT_BPS
            if pos_cost_bps > 0 and pos_cost_bps > s.filled_total_cost_bps:
                logger.info(
                    "cost_augmented_from_positions_api",
                    event_ticker=self.event_ticker,
                    side=side.value,
                    ledger_cost=s.filled_total_cost_bps // ONE_CENT_BPS,
                    positions_cost=position_costs[side],
                )
                s.filled_total_cost_bps = pos_cost_bps

            # Fees from positions API — authoritative when orders are archived
            pos_fees_bps = position_fees[side] * ONE_CENT_BPS
            if pos_fees_bps > 0 and pos_fees_bps > s.filled_fees_bps:
                s.filled_fees_bps = pos_fees_bps
                s._fees_from_api = True

        self._reconcile_closed(path="sync_positions")

    def format_position(self, side: Side) -> str:
        """Human-readable position string for proposals."""
        s = self._sides[side]
        parts: list[str] = []
        filled_whole = s.filled_count_fp100 // ONE_CONTRACT_FP100
        resting_whole = s.resting_count_fp100 // ONE_CONTRACT_FP100
        if filled_whole > 0:
            avg = self.avg_filled_price(side)
            parts.append(f"{filled_whole} filled @ {avg:.1f}c")
        if resting_whole > 0:
            resting_price_cents = bps_to_cents_round(s.resting_price_bps)
            parts.append(f"{resting_whole} resting @ {resting_price_cents}c")
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
