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

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

import structlog

from talos.automation_config import DEFAULT_UNIT_SIZE
from talos.fees import (
    MAKER_FEE_RATE,
    fee_adjusted_cost_bps,
    fee_adjusted_profit_matched_bps,
)
from talos.models.order import Fill
from talos.models.position import EventPositionSummary, LegSummary
from talos.units import (
    ONE_CENT_BPS,
    ONE_CONTRACT_FP100,
    ONE_DOLLAR_BPS,
    bps_to_cents_round,
    cents_to_bps,
    quadratic_fee_bps,
)

if TYPE_CHECKING:
    from talos.cpm import CPMTracker
    from talos.models.strategy import ArbPair
    from talos.rest_client import KalshiRESTClient

logger = structlog.get_logger()


class Side(Enum):
    A = "A"
    B = "B"

    @property
    def other(self) -> Side:
        return Side.B if self is Side.A else Side.A


@dataclass(frozen=True, slots=True)
class LedgerSnapshot:
    """Immutable full-state snapshot for the persistence envelope + reconcile apply.

    Built by :meth:`PositionLedger._snapshot_with_rebuild_applied` without
    touching live ledger state. :meth:`PositionLedger._apply_snapshot`
    overwrites the live ledger fields from the snapshot in a single sync
    block — no await, no interleaving window (v11 atomicity).

    ``stale_*`` flags and ``reconcile_mismatch_pending`` are in-memory only
    per Section 7 / 8a of the bps/fp100 migration spec — they never travel
    in a snapshot.
    """

    # Side A historical
    filled_count_fp100_a: int
    filled_total_cost_bps_a: int
    filled_fees_bps_a: int
    closed_count_fp100_a: int
    closed_total_cost_bps_a: int
    closed_fees_bps_a: int
    # Side A resting
    resting_id_a: str | None
    resting_count_fp100_a: int
    resting_price_bps_a: int
    # Side B historical
    filled_count_fp100_b: int
    filled_total_cost_bps_b: int
    filled_fees_bps_b: int
    closed_count_fp100_b: int
    closed_total_cost_bps_b: int
    closed_fees_bps_b: int
    # Side B resting
    resting_id_b: str | None
    resting_count_fp100_b: int
    resting_price_bps_b: int
    # Persisted flag (only one that travels on disk).
    legacy_migration_pending: bool = False


class ReconcileOutcome(Enum):
    OK = "ok"
    MISMATCH = "mismatch"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    outcome: ReconcileOutcome
    rebuilt: LedgerSnapshot | None = None
    error: str | None = None


class StaleMismatchError(Exception):
    """Raised by :meth:`PositionLedger.accept_pending_mismatch` when the
    captured mismatch is stale — the ledger mutated between detection and
    operator click. Operator must re-invoke reconcile to see a fresh diff.
    """


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

        # ── bps/fp100 migration: staleness + reconcile infrastructure ──
        # In-memory only (never persisted) — always recomputed on load.
        self.stale_fills_unconfirmed: bool = False
        self.stale_resting_unconfirmed: bool = False
        self.reconcile_mismatch_pending: bool = False
        # Persisted (may carry across restart if operator closes mid-reconcile).
        self.legacy_migration_pending: bool = False

        # Monotonically-increasing counter bumped by every sync mutator.
        # Used by accept_pending_mismatch for stale-rebuild detection (F19).
        # Plain int — sync mutators run atomically under the single event
        # loop, so no lock is required (v11 simplification).
        self._mutation_generation: int = 0

        # Fresh-pair minimum gate — set by the first sync_from_orders
        # completion (Section 8 startup sequence step 2).
        self._first_orders_sync: asyncio.Event = asyncio.Event()

        # Reconcile state (F16 — in-session only, never persisted).
        self._pending_mismatch: LedgerSnapshot | None = None
        self._pending_mismatch_gen: int = -1

        # Retained after a v1 load so the next save can embed the blob
        # under the v2 envelope while legacy_migration_pending is True.
        self._legacy_v1_snapshot: dict[str, object] | None = None

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

    # ── Startup confirmation gate (Section 8) ──────────────────────

    def ready(self) -> bool:
        """True iff all startup confirmation flags have cleared.

        Risk-increasing operations (``create_order``, ``amend_order``) must
        block while any flag is set. Cancel is NOT gated (F31 carve-out —
        wired in Task 6b-2).
        """
        if self.stale_fills_unconfirmed:
            return False
        if self.stale_resting_unconfirmed:
            return False
        if self.legacy_migration_pending:
            return False
        if self.reconcile_mismatch_pending:
            return False
        return self._first_orders_sync.is_set()

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

        # Fee-adjusted: effective cost = price + fee(price), in bps.
        effective_this_bps = fee_adjusted_cost_bps(cents_to_bps(price), rate=rate)
        effective_other_bps = fee_adjusted_cost_bps(
            cents_to_bps(int(round(other_price))), rate=rate
        )
        if effective_this_bps + effective_other_bps >= ONE_DOLLAR_BPS:
            return (
                False,
                f"arb not profitable after fees: "
                f"{effective_this_bps / ONE_CENT_BPS:.2f}¢ + "
                f"{effective_other_bps / ONE_CENT_BPS:.2f}¢ = "
                f"{(effective_this_bps + effective_other_bps) / ONE_CENT_BPS:.2f}¢ "
                f">= $1.00",
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
        # Defensive: if closed > filled on either side (corruption — e.g.
        # a reconcile rebuild that didn't preserve closed-bucket invariants,
        # or a bad v1→v2 migration), open_* goes negative; matchable_whole
        # stays negative after floor-div; units_to_close stays negative;
        # the loop below would then divide by a negative open_count_fp100,
        # raising ZeroDivisionError or silently rebuilding with wrong sign.
        # The `<= 0` guard catches both the normal "nothing to close" case
        # and the corruption path. Log once at WARNING for forensic visibility
        # so a corruption-induced skip doesn't hide.
        if open_a_fp100 < 0 or open_b_fp100 < 0:
            logger.warning(
                "ledger_closed_exceeds_filled",
                event_ticker=self.event_ticker,
                path=path,
                open_a_fp100=open_a_fp100,
                open_b_fp100=open_b_fp100,
                filled_a_fp100=a.filled_count_fp100,
                closed_a_fp100=a.closed_count_fp100,
                filled_b_fp100=b.filled_count_fp100,
                closed_b_fp100=b.closed_count_fp100,
            )
            return
        matchable_whole = min(open_a_fp100, open_b_fp100) // ONE_CONTRACT_FP100
        units_to_close = matchable_whole // self.unit_size
        if units_to_close <= 0:
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
        self._mutation_generation += 1

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
        self._mutation_generation += 1

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
        self._mutation_generation += 1

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
        self._mutation_generation += 1

    def mark_side_pending(self, side: Side) -> None:
        """Mark a side as having an unconfirmed change (stale-sync guard).

        Use when a cancel succeeded on Kalshi but record_cancel can't match
        the order_id (e.g., WS updated the ledger during the await). The
        resting state is uncertain — protect from stale sync overwrite and
        block new bids until the next confirmed sync.
        """
        self._sides[side]._placed_at_gen = self._sync_gen + 1
        self._mutation_generation += 1

    def mark_order_cancelled(self, order_id: str) -> None:
        """Register an order_id as confirmed cancelled on Kalshi.

        sync_from_orders will filter this ID out until Kalshi's GET endpoint
        stops returning it (eventual consistency propagation).
        """
        self._recently_cancelled.add(order_id)
        self._mutation_generation += 1

    def reset_pair(self) -> None:
        """Clear state after both sides complete. Ready for next pair.

        Bumps ``_mutation_generation`` so any pending reconcile mismatch
        captured before the reset is correctly flagged as stale on the
        next ``accept_pending_mismatch`` call (F19 invariant — the
        mutation counter must advance on EVERY state-changing operation,
        otherwise an operator clicking Accept after a reset would apply
        a pre-reset rebuild to a cleared ledger).
        """
        self._sides[Side.A].reset()
        self._sides[Side.B].reset()
        self._mutation_generation += 1

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
        envelope: dict[str, object] = {
            "schema_version": 2,
            "legacy_migration_pending": self.legacy_migration_pending,
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
        # Retain the original v1 payload as a sibling field until reconcile
        # clears legacy_migration_pending. See spec Section 7 save-path rules.
        if self.legacy_migration_pending and self._legacy_v1_snapshot is not None:
            envelope["legacy_v1_snapshot"] = dict(self._legacy_v1_snapshot)
        return envelope

    def _compute_staleness_on_load(self) -> None:
        """Set ``stale_*_unconfirmed`` flags based on loaded state.

        Called at the end of :meth:`seed_from_saved` regardless of v1/v2
        path. The flags themselves are in-memory only — always recomputed
        on load, never persisted.
        """
        fills_nonzero = any(
            getattr(self._sides[side], field) != 0
            for side in (Side.A, Side.B)
            for field in (
                "filled_count_fp100",
                "filled_total_cost_bps",
                "filled_fees_bps",
                "closed_count_fp100",
                "closed_total_cost_bps",
                "closed_fees_bps",
            )
        )
        resting_nonzero = any(
            (
                self._sides[side].resting_count_fp100 > 0
                or self._sides[side].resting_order_id is not None
            )
            for side in (Side.A, Side.B)
        )
        self.stale_fills_unconfirmed = fills_nonzero
        self.stale_resting_unconfirmed = resting_nonzero

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
            # Legacy-migration metadata travels on the v2 envelope; the
            # embedded v1 snapshot (if any) survives for the next save.
            self.legacy_migration_pending = bool(
                data.get("legacy_migration_pending", False)
            )
            embedded_v1 = data.get("legacy_v1_snapshot")
            if self.legacy_migration_pending and isinstance(embedded_v1, dict):
                self._legacy_v1_snapshot = dict(embedded_v1)
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
                legacy_migration_pending=self.legacy_migration_pending,
            )
            # Terminal reconcile — idempotent in normal-restart case.
            self._reconcile_closed(path="seed_from_saved")
            # Staleness flags are derived, not persisted — compute last.
            self._compute_staleness_on_load()
            self._mutation_generation += 1
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

        # F22: legacy_migration_pending gates on any nonzero safety-relevant
        # field after conversion. Zero-state v1 payloads convert trivially
        # and MUST NOT set this flag (otherwise the gate permanently blocks
        # a ledger that had nothing to reconcile). The staleness rule below
        # uses the same nonzero criterion.
        self._compute_staleness_on_load()
        if self.stale_fills_unconfirmed or self.stale_resting_unconfirmed:
            self.legacy_migration_pending = True
            # Retain the ORIGINAL v1 payload verbatim for the next save's
            # v2 envelope embedding. Freeze a shallow copy — the caller's
            # mapping may otherwise be mutated elsewhere.
            self._legacy_v1_snapshot = {str(k): data[k] for k in data}
        self._mutation_generation += 1

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
            if order.fill_count_fp100 > 0:
                kalshi_filled_fp100[side] += order.fill_count_fp100
                kalshi_fill_cost_bps[side] += (
                    order.maker_fill_cost_bps + order.taker_fill_cost_bps
                )
                kalshi_fees_bps[side] += order.maker_fees_bps
            # Only track resting from active orders — skip recently cancelled
            # IDs that Kalshi's GET may still return due to eventual consistency
            if order.remaining_count_fp100 > 0 and order.status in ("resting", "executed"):
                if order.order_id in self._recently_cancelled:
                    continue
                # Use correct price field based on order side
                resting_price_bps = (
                    order.no_price_bps if order.side == "no" else order.yes_price_bps
                )
                kalshi_resting[side].append(
                    (
                        order.order_id,
                        order.remaining_count_fp100,
                        resting_price_bps,
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
                    and order.remaining_count_fp100 > 0
                    and order.status in ("resting", "executed")
                ):
                    still_stale.add(order.order_id)
            # IDs not in still_stale are confirmed gone — remove them
            self._recently_cancelled = still_stale

        # Two-source sync (orders + positions) keeps the ledger accurate.
        self._reconcile_closed(path="sync_orders")

        # Orders endpoint authoritatively confirms live resting state — any
        # completion (including empty response) clears the flag. Does NOT
        # clear stale_fills_unconfirmed or legacy_migration_pending — see
        # spec Section 7 F20: orders-endpoint data is archival-incomplete
        # for historical economics.
        self.stale_resting_unconfirmed = False
        # Fresh-pair minimum gate (Section 8 startup step 2).
        self._first_orders_sync.set()
        self._mutation_generation += 1

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
        # positions data is count-only — does NOT clear any staleness flag
        # (spec Section 8 startup step 3). Still bump the mutation counter
        # so reconcile's stale-rebuild detection covers this path too.
        self._mutation_generation += 1

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

    # ── Fills-based reconcile (spec Section 8a) ────────────────────

    def _rebuild_from_fills(
        self,
        fills_a: list[Fill],
        fills_b: list[Fill],
    ) -> LedgerSnapshot:
        """Aggregate per-fill records into a full-state snapshot.

        Same-ticker pairs: ``fills_b`` is empty; side assignment comes from
        each fill's ``side`` string against the pair's configured
        ``_side_a_str`` / ``_side_b_str``.

        Cross-ticker pairs: each list maps to the matching side (fills_a →
        Side.A, fills_b → Side.B). Only ``action == "buy"`` contributions
        are counted (matches ``sync_from_orders`` filtering).

        ``closed_*`` and ``resting_*`` state is carried over from the live
        ledger unchanged — reconcile rebuilds the historical ``filled_*``
        state only. Closed state is the cumulative locked-in matched-pair
        bucket, which does not rebuild from fills directly; later sync
        paths re-flush via :meth:`_reconcile_closed` as usual.
        """
        sums_count: dict[Side, int] = {Side.A: 0, Side.B: 0}
        sums_cost: dict[Side, int] = {Side.A: 0, Side.B: 0}
        sums_fees: dict[Side, int] = {Side.A: 0, Side.B: 0}

        def _side_for_fill(fill: Fill) -> Side | None:
            if self._is_same_ticker:
                if fill.ticker != self._ticker_a:
                    return None
                if fill.side == self._side_a_str:
                    return Side.A
                if fill.side == self._side_b_str:
                    return Side.B
                return None
            # cross-ticker: side assignment is by the caller's list split.
            # Fills in fills_a belong to Side.A; in fills_b → Side.B.
            return None  # unused; caller routes by list

        def _price_bps_for(fill: Fill) -> int:
            # Reconcile treats the price as the leg-side execution price.
            # sync_from_orders uses no_price for "no" side, yes_price for "yes".
            return fill.no_price_bps if fill.side == "no" else fill.yes_price_bps

        if self._is_same_ticker:
            for fill in fills_a:
                if fill.action and fill.action != "buy":
                    continue
                side = _side_for_fill(fill)
                if side is None:
                    continue
                sums_count[side] += fill.count_fp100
                sums_cost[side] += (
                    fill.count_fp100 * _price_bps_for(fill) // ONE_CONTRACT_FP100
                )
                sums_fees[side] += fill.fee_cost_bps
        else:
            for side, fills in ((Side.A, fills_a), (Side.B, fills_b)):
                for fill in fills:
                    if fill.action and fill.action != "buy":
                        continue
                    sums_count[side] += fill.count_fp100
                    sums_cost[side] += (
                        fill.count_fp100 * _price_bps_for(fill) // ONE_CONTRACT_FP100
                    )
                    sums_fees[side] += fill.fee_cost_bps

        a = self._sides[Side.A]
        b = self._sides[Side.B]
        return LedgerSnapshot(
            filled_count_fp100_a=sums_count[Side.A],
            filled_total_cost_bps_a=sums_cost[Side.A],
            filled_fees_bps_a=sums_fees[Side.A],
            closed_count_fp100_a=a.closed_count_fp100,
            closed_total_cost_bps_a=a.closed_total_cost_bps,
            closed_fees_bps_a=a.closed_fees_bps,
            resting_id_a=a.resting_order_id,
            resting_count_fp100_a=a.resting_count_fp100,
            resting_price_bps_a=a.resting_price_bps,
            filled_count_fp100_b=sums_count[Side.B],
            filled_total_cost_bps_b=sums_cost[Side.B],
            filled_fees_bps_b=sums_fees[Side.B],
            closed_count_fp100_b=b.closed_count_fp100,
            closed_total_cost_bps_b=b.closed_total_cost_bps,
            closed_fees_bps_b=b.closed_fees_bps,
            resting_id_b=b.resting_order_id,
            resting_count_fp100_b=b.resting_count_fp100,
            resting_price_bps_b=b.resting_price_bps,
            legacy_migration_pending=self.legacy_migration_pending,
        )

    def _significantly_differs(self, rebuilt: LedgerSnapshot) -> bool:
        """Return True if ``rebuilt`` differs from live state on any
        safety-relevant filled field.

        Tolerance: exact-zero on ``count_fp100``; up to 1 bps drift on
        cost/fees (integer rounding in cents-to-bps conversion of legacy
        data). Resting and closed state are not compared — neither is
        rebuilt by this path.
        """
        a = self._sides[Side.A]
        b = self._sides[Side.B]
        if rebuilt.filled_count_fp100_a != a.filled_count_fp100:
            return True
        if rebuilt.filled_count_fp100_b != b.filled_count_fp100:
            return True
        if abs(rebuilt.filled_total_cost_bps_a - a.filled_total_cost_bps) > 1:
            return True
        if abs(rebuilt.filled_total_cost_bps_b - b.filled_total_cost_bps) > 1:
            return True
        if abs(rebuilt.filled_fees_bps_a - a.filled_fees_bps) > 1:
            return True
        return abs(rebuilt.filled_fees_bps_b - b.filled_fees_bps) > 1

    def _snapshot_with_rebuild_applied(
        self,
        rebuilt: LedgerSnapshot,
        *,
        clear_fills_stale: bool,
        clear_resting_stale: bool,
        clear_legacy_pending: bool,
    ) -> LedgerSnapshot:
        """Pure function: combine rebuilt overlay with current live state,
        apply flag clears, return immutable proposed snapshot.

        Does NOT mutate the ledger. Resting state + closed state carry
        over from the live ledger; filled state comes from the rebuild.
        """
        a = self._sides[Side.A]
        b = self._sides[Side.B]
        # ``clear_*_stale`` are read but only represent information already
        # baked into the snapshot's legacy_migration_pending field. The
        # in-memory stale_* flags are not part of LedgerSnapshot — they're
        # set by _apply_snapshot when it runs.
        _ = clear_fills_stale
        _ = clear_resting_stale
        new_legacy_pending = (
            False if clear_legacy_pending else self.legacy_migration_pending
        )
        return LedgerSnapshot(
            filled_count_fp100_a=rebuilt.filled_count_fp100_a,
            filled_total_cost_bps_a=rebuilt.filled_total_cost_bps_a,
            filled_fees_bps_a=rebuilt.filled_fees_bps_a,
            closed_count_fp100_a=a.closed_count_fp100,
            closed_total_cost_bps_a=a.closed_total_cost_bps,
            closed_fees_bps_a=a.closed_fees_bps,
            resting_id_a=a.resting_order_id,
            resting_count_fp100_a=a.resting_count_fp100,
            resting_price_bps_a=a.resting_price_bps,
            filled_count_fp100_b=rebuilt.filled_count_fp100_b,
            filled_total_cost_bps_b=rebuilt.filled_total_cost_bps_b,
            filled_fees_bps_b=rebuilt.filled_fees_bps_b,
            closed_count_fp100_b=b.closed_count_fp100,
            closed_total_cost_bps_b=b.closed_total_cost_bps,
            closed_fees_bps_b=b.closed_fees_bps,
            resting_id_b=b.resting_order_id,
            resting_count_fp100_b=b.resting_count_fp100,
            resting_price_bps_b=b.resting_price_bps,
            legacy_migration_pending=new_legacy_pending,
        )

    def _apply_snapshot(self, snapshot: LedgerSnapshot) -> None:
        """Synchronous overwrite of live ledger fields from ``snapshot``.

        No ``await``. Atomic relative to every other coroutine on the
        event loop (v11 atomicity — single sync block). Bumps
        ``_mutation_generation`` at the end.

        Flag clears: ``legacy_migration_pending`` comes from the snapshot.
        The ``stale_*`` flags are cleared here unconditionally under the
        assumption the caller has verified state (this is only invoked
        from :meth:`reconcile_from_fills` OK or :meth:`accept_pending_mismatch`,
        both of which mean fills have been confirmed authoritative).
        """
        a = self._sides[Side.A]
        b = self._sides[Side.B]
        a.filled_count_fp100 = snapshot.filled_count_fp100_a
        a.filled_total_cost_bps = snapshot.filled_total_cost_bps_a
        a.filled_fees_bps = snapshot.filled_fees_bps_a
        a.closed_count_fp100 = snapshot.closed_count_fp100_a
        a.closed_total_cost_bps = snapshot.closed_total_cost_bps_a
        a.closed_fees_bps = snapshot.closed_fees_bps_a
        a.resting_order_id = snapshot.resting_id_a
        a.resting_count_fp100 = snapshot.resting_count_fp100_a
        a.resting_price_bps = snapshot.resting_price_bps_a
        b.filled_count_fp100 = snapshot.filled_count_fp100_b
        b.filled_total_cost_bps = snapshot.filled_total_cost_bps_b
        b.filled_fees_bps = snapshot.filled_fees_bps_b
        b.closed_count_fp100 = snapshot.closed_count_fp100_b
        b.closed_total_cost_bps = snapshot.closed_total_cost_bps_b
        b.closed_fees_bps = snapshot.closed_fees_bps_b
        b.resting_order_id = snapshot.resting_id_b
        b.resting_count_fp100 = snapshot.resting_count_fp100_b
        b.resting_price_bps = snapshot.resting_price_bps_b

        self.legacy_migration_pending = snapshot.legacy_migration_pending
        # Fills have been confirmed authoritative — clear the fills flag.
        # Resting staleness is NOT cleared by fills reconcile (spec
        # Section 8 step 5); it requires sync_from_orders completion.
        self.stale_fills_unconfirmed = False
        # Drop the v1 payload if legacy reconciliation has now completed.
        if not self.legacy_migration_pending:
            self._legacy_v1_snapshot = None
        self._mutation_generation += 1

    async def reconcile_from_fills(
        self,
        rest: KalshiRESTClient,
        persist_cb: Callable[[LedgerSnapshot, str], None],
    ) -> ReconcileResult:
        """Authoritative rebuild from per-fill ground truth.

        Fetch phase is async (``rest.get_all_fills``). Mutation phase is a
        single sync block — no ``await`` inside — so it runs atomically
        relative to every other coroutine on the event loop (v11 atomicity).

        ``persist_cb(snapshot, event_ticker)`` is SYNC (not async) and is
        called before the snapshot is applied to the live ledger. If
        ``persist_cb`` raises, live state stays untouched and the result
        is ``ERROR`` (F13 — durable before success).

        On ``MISMATCH``, retains ``_pending_mismatch`` and sets
        ``reconcile_mismatch_pending`` without calling ``persist_cb``. The
        mismatch state is in-session only (F16); on restart the operator
        re-invokes reconcile.
        """
        # Step 1: fetch. No mutation, no lock.
        try:
            fills_a = await rest.get_all_fills(ticker=self._ticker_a)
            fills_b = (
                []
                if self._is_same_ticker
                else await rest.get_all_fills(ticker=self._ticker_b)
            )
        except Exception as exc:
            logger.warning(
                "reconcile_fetch_failed",
                event_ticker=self.event_ticker,
                error=str(exc),
            )
            return ReconcileResult(outcome=ReconcileOutcome.ERROR, error=str(exc))

        rebuilt = self._rebuild_from_fills(fills_a, fills_b)

        # Step 2: mutation phase — single sync block, no await inside.
        if self._significantly_differs(rebuilt):
            # F16 — in-session only, never persisted.
            self._pending_mismatch = rebuilt
            self.reconcile_mismatch_pending = True
            self._pending_mismatch_gen = self._mutation_generation
            logger.warning(
                "reconcile_mismatch",
                event_ticker=self.event_ticker,
                gen=self._mutation_generation,
                loaded_count_a=self._sides[Side.A].filled_count_fp100,
                rebuilt_count_a=rebuilt.filled_count_fp100_a,
                loaded_count_b=self._sides[Side.B].filled_count_fp100,
                rebuilt_count_b=rebuilt.filled_count_fp100_b,
            )
            return ReconcileResult(outcome=ReconcileOutcome.MISMATCH, rebuilt=rebuilt)

        proposed = self._snapshot_with_rebuild_applied(
            rebuilt,
            clear_fills_stale=True,
            clear_resting_stale=False,
            clear_legacy_pending=True,
        )

        # Durable persist before live apply (F13).
        try:
            persist_cb(proposed, self.event_ticker)
        except Exception as exc:
            logger.exception(
                "reconcile_persist_failed", event_ticker=self.event_ticker
            )
            return ReconcileResult(outcome=ReconcileOutcome.ERROR, error=str(exc))

        # Sync apply. Still no await. _mutation_generation bumped inside.
        self._apply_snapshot(proposed)
        logger.info(
            "ledger_reconciled_from_fills",
            event_ticker=self.event_ticker,
            fills_count=len(fills_a) + len(fills_b),
            gen=self._mutation_generation,
        )
        return ReconcileResult(outcome=ReconcileOutcome.OK, rebuilt=rebuilt)

    async def accept_pending_mismatch(
        self,
        persist_cb: Callable[[LedgerSnapshot, str], None],
    ) -> None:
        """Explicitly apply a previously-detected fills-rebuild.

        Generation guard: if the ledger mutated between mismatch detection
        and operator click, raises :class:`StaleMismatchError`. Operator
        must re-invoke :meth:`reconcile_from_fills` to see a fresh diff (F19).

        If ``persist_cb`` raises, live state is untouched and
        ``_pending_mismatch`` is retained — operator can retry after
        resolving the persistence issue.
        """
        if not self.reconcile_mismatch_pending or self._pending_mismatch is None:
            raise RuntimeError("no pending mismatch to accept")

        captured_gen = self._pending_mismatch_gen
        if self._mutation_generation != captured_gen:
            # Ledger moved on — discard the stale rebuild and force the
            # operator to re-invoke reconcile.
            self._pending_mismatch = None
            self.reconcile_mismatch_pending = False
            self._pending_mismatch_gen = -1
            raise StaleMismatchError(
                f"pending mismatch is stale "
                f"(gen {captured_gen} → {self._mutation_generation}) — "
                f"re-run reconcile to see current diff"
            )

        rebuilt = self._pending_mismatch
        proposed = self._snapshot_with_rebuild_applied(
            rebuilt,
            clear_fills_stale=True,
            clear_resting_stale=False,
            clear_legacy_pending=True,
        )

        # Durable persist before live apply.
        persist_cb(proposed, self.event_ticker)

        self._apply_snapshot(proposed)
        self.reconcile_mismatch_pending = False
        self._pending_mismatch = None
        self._pending_mismatch_gen = -1
        logger.info(
            "mismatch_accepted_by_operator",
            event_ticker=self.event_ticker,
            gen=self._mutation_generation,
        )


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

        # Exact-precision bps state from the ledger.
        cost_a_bps = ledger.filled_total_cost_bps(Side.A)
        cost_b_bps = ledger.filled_total_cost_bps(Side.B)
        fees_a_bps = ledger.filled_fees_bps(Side.A)
        fees_b_bps = ledger.filled_fees_bps(Side.B)
        filled_a_fp100 = ledger.filled_count_fp100(Side.A)
        filled_b_fp100 = ledger.filled_count_fp100(Side.B)

        # When orders were archived across restart, fees are lost (zero)
        # but cost/count are restored from positions API. Estimate fees
        # from the quadratic formula to avoid showing gross-only profit.
        # Only estimate when the API has been consulted (_fees_from_api)
        # but returned zero — not when record_fill() was used without sync.
        side_a = ledger._sides[Side.A]
        side_b = ledger._sides[Side.B]
        if fees_a_bps == 0 and filled_a_fp100 > 0 and cost_a_bps > 0 and side_a._fees_from_api:
            avg_a_bps = cost_a_bps * ONE_CONTRACT_FP100 // filled_a_fp100
            per_contract_fee = quadratic_fee_bps(avg_a_bps, rate=pair.fee_rate)
            fees_a_bps = per_contract_fee * filled_a_fp100 // ONE_CONTRACT_FP100
        if fees_b_bps == 0 and filled_b_fp100 > 0 and cost_b_bps > 0 and side_b._fees_from_api:
            avg_b_bps = cost_b_bps * ONE_CONTRACT_FP100 // filled_b_fp100
            per_contract_fee = quadratic_fee_bps(avg_b_bps, rate=pair.fee_rate)
            fees_b_bps = per_contract_fee * filled_b_fp100 // ONE_CONTRACT_FP100

        if matched > 0:
            matched_fp100 = matched * ONE_CONTRACT_FP100
            cost_a_matched_bps = _prorate(cost_a_bps, matched, filled_a)
            cost_b_matched_bps = _prorate(cost_b_bps, matched, filled_b)
            fees_a_matched_bps = _prorate(fees_a_bps, matched, filled_a)
            fees_b_matched_bps = _prorate(fees_b_bps, matched, filled_b)
            locked_profit_bps: float = fee_adjusted_profit_matched_bps(
                matched_fp100,
                cost_a_matched_bps,
                cost_b_matched_bps,
                fees_a_matched_bps,
                fees_b_matched_bps,
            )
        else:
            locked_profit_bps = 0.0

        exposure_bps = _prorate(cost_a_bps, unmatched_a, filled_a) + _prorate(
            cost_b_bps, unmatched_b, filled_b
        )

        # Average fill price per whole contract, in bps. Falls back to the
        # resting price (in bps) when no fills exist yet.
        avg_a_bps = (
            cost_a_bps * ONE_CONTRACT_FP100 // filled_a_fp100
            if filled_a_fp100 > 0
            else ledger.resting_price_bps(Side.A)
        )
        avg_b_bps = (
            cost_b_bps * ONE_CONTRACT_FP100 // filled_b_fp100
            if filled_b_fp100 > 0
            else ledger.resting_price_bps(Side.B)
        )

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
                    no_price_bps=avg_a_bps,
                    filled_count=filled_a,
                    resting_count=resting_a,
                    total_fill_cost_bps=cost_a_bps,
                    total_fees_bps=fees_a_bps,
                    queue_position=qp_a,
                    cpm=cpm_a,
                    cpm_partial=cpm_a_partial,
                    eta_minutes=eta_a,
                    resting_no_price_bps=(
                        ledger.resting_price_bps(Side.A) if resting_a > 0 else None
                    ),
                ),
                leg_b=LegSummary(
                    ticker=pair.ticker_b,
                    no_price_bps=avg_b_bps,
                    filled_count=filled_b,
                    resting_count=resting_b,
                    total_fill_cost_bps=cost_b_bps,
                    total_fees_bps=fees_b_bps,
                    queue_position=qp_b,
                    cpm=cpm_b,
                    cpm_partial=cpm_b_partial,
                    eta_minutes=eta_b,
                    resting_no_price_bps=(
                        ledger.resting_price_bps(Side.B) if resting_b > 0 else None
                    ),
                ),
                matched_pairs=matched,
                locked_profit_bps=locked_profit_bps,
                unmatched_a=unmatched_a,
                unmatched_b=unmatched_b,
                exposure_bps=exposure_bps,
                unit_size=ledger.unit_size,
            )
        )

    return summaries
