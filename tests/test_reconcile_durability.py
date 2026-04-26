"""Reconcile durability + v11 atomicity tests (spec F11/F13/F18).

Covers the full reconcile state machine around the persist-before-apply
contract, the auto-adopt-on-mismatch semantics (Principle 7 — Kalshi is
the single source of truth), v11 sync-mutator atomicity under
single-event-loop asyncio, and the no-async-lock regression guard.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, cast

import pytest

from talos.models.order import Fill
from talos.position_ledger import (
    LedgerSnapshot,
    PositionLedger,
    ReconcileOutcome,
    Side,
)
from talos.rest_client import KalshiRESTClient

# ── Fakes ------------------------------------------------------------


class _FakeRest:
    """Minimal stand-in for KalshiRESTClient for reconcile tests."""

    def __init__(
        self,
        fills_by_ticker: dict[str, list[Fill]] | None = None,
        raise_for_ticker: dict[str, Exception] | None = None,
    ) -> None:
        self._fills = fills_by_ticker or {}
        self._raise = raise_for_ticker or {}
        self.calls: list[str] = []

    async def get_all_fills(
        self,
        *,
        ticker: str | None = None,
        order_id: str | None = None,
    ) -> list[Fill]:
        assert ticker is not None
        self.calls.append(ticker)
        if ticker in self._raise:
            raise self._raise[ticker]
        return list(self._fills.get(ticker, []))


def _make_fill(ticker: str, count_fp100: int, price_bps: int = 4800) -> Fill:
    return Fill.model_validate(
        {
            "trade_id": f"t-{ticker}-{count_fp100}",
            "order_id": f"o-{ticker}",
            "ticker": ticker,
            "side": "no",
            "action": "buy",
            "count_fp100": count_fp100,
            "no_price_bps": price_bps,
            "fee_cost_bps": 0,
        }
    )


def _make_ledger(matched_count_fp100: int = 500) -> PositionLedger:
    """A ledger pre-loaded with matched-pair historical state."""
    ledger = PositionLedger(
        "EVT",
        unit_size=10,
        ticker_a="T-A",
        ticker_b="T-B",
    )
    ledger.record_fill_bps(Side.A, count_fp100=matched_count_fp100, price_bps=4800)
    ledger.record_fill_bps(Side.B, count_fp100=matched_count_fp100, price_bps=4800)
    return ledger


# ── 1. Successful reconcile + durable persist ----------------------


def test_successful_reconcile_persists_then_applies() -> None:
    ledger = _make_ledger(matched_count_fp100=500)
    rest = _FakeRest(
        {
            "T-A": [_make_fill("T-A", 500)],
            "T-B": [_make_fill("T-B", 500)],
        }
    )

    persisted: list[tuple[LedgerSnapshot, str]] = []

    def persist_cb(snap: LedgerSnapshot, ticker: str) -> None:
        persisted.append((snap, ticker))

    result = asyncio.run(ledger.reconcile_from_fills(cast(KalshiRESTClient, rest), persist_cb))
    assert result.outcome == ReconcileOutcome.OK
    assert len(persisted) == 1
    snap, ticker = persisted[0]
    assert ticker == "EVT"
    # Snapshot matches post-mutation state.
    assert snap.filled_count_fp100_a == 500
    assert snap.filled_count_fp100_b == 500
    assert ledger.filled_count_fp100(Side.A) == 500
    assert ledger.filled_count_fp100(Side.B) == 500
    assert ledger.stale_fills_unconfirmed is False


# ── 2. Reconcile persist failure → ledger unchanged ------------------


def test_reconcile_persist_failure_leaves_ledger_unchanged() -> None:
    ledger = _make_ledger(matched_count_fp100=500)
    # Rebuild produces the same state → no mismatch path, so we'd apply.
    rest = _FakeRest(
        {
            "T-A": [_make_fill("T-A", 500)],
            "T-B": [_make_fill("T-B", 500)],
        }
    )

    before_a = ledger.filled_count_fp100(Side.A)
    before_b = ledger.filled_count_fp100(Side.B)
    before_gen = ledger._mutation_generation

    def persist_cb(snap: LedgerSnapshot, ticker: str) -> None:
        raise RuntimeError("disk full")

    result = asyncio.run(ledger.reconcile_from_fills(cast(KalshiRESTClient, rest), persist_cb))
    assert result.outcome == ReconcileOutcome.ERROR
    assert "disk full" in (result.error or "")
    # Ledger completely unchanged.
    assert ledger.filled_count_fp100(Side.A) == before_a
    assert ledger.filled_count_fp100(Side.B) == before_b
    assert ledger._mutation_generation == before_gen


# ── 3. Mismatch is auto-adopted (Principle 7) -----------------------


def test_mismatch_auto_adopts_kalshi_fills() -> None:
    """On detected mismatch, reconcile_from_fills adopts Kalshi's view as
    authoritative (Principle 7) — no pending state, no operator gate.
    Persist runs; the loaded values are overwritten with the rebuilt values.
    """
    ledger = _make_ledger(matched_count_fp100=500)
    # Rebuild with DIFFERENT state → would have been MISMATCH pre-refactor.
    rest = _FakeRest(
        {
            "T-A": [_make_fill("T-A", 700)],
            "T-B": [_make_fill("T-B", 500)],
        }
    )

    persisted: list[LedgerSnapshot] = []

    def persist_cb(snap: LedgerSnapshot, ticker: str) -> None:
        persisted.append(snap)

    result = asyncio.run(
        ledger.reconcile_from_fills(cast(KalshiRESTClient, rest), persist_cb)
    )
    # No MISMATCH variant exists anymore — auto-adopt returns OK.
    assert result.outcome == ReconcileOutcome.OK
    # Kalshi's view is now live.
    assert ledger.filled_count_fp100(Side.A) == 700
    assert ledger.filled_count_fp100(Side.B) == 500
    # Persisted durably (unlike the pre-refactor mismatch path).
    assert len(persisted) == 1
    assert persisted[0].filled_count_fp100_a == 700
    assert persisted[0].filled_count_fp100_b == 500
    # No pending-mismatch infrastructure survives.
    assert not hasattr(ledger, "reconcile_mismatch_pending")
    assert not hasattr(ledger, "_pending_mismatch")
    assert not hasattr(ledger, "accept_pending_mismatch")
    # Envelope has no pending fields.
    env = ledger.to_save_dict()
    assert "reconcile_mismatch_pending" not in env
    inner = env["ledger"]
    assert isinstance(inner, dict)
    assert "reconcile_mismatch_pending" not in inner


# ── 4. Pagination failure → ERROR, no mutation -----------------------


def test_pagination_failure_preserves_live_state() -> None:
    ledger = _make_ledger(matched_count_fp100=500)
    rest = _FakeRest(raise_for_ticker={"T-A": RuntimeError("page 3 failed")})

    before_a = ledger.filled_count_fp100(Side.A)
    before_gen = ledger._mutation_generation

    called = {"persist": False}

    def persist_cb(snap: LedgerSnapshot, ticker: str) -> None:
        called["persist"] = True

    result = asyncio.run(ledger.reconcile_from_fills(cast(KalshiRESTClient, rest), persist_cb))
    assert result.outcome == ReconcileOutcome.ERROR
    assert called["persist"] is False
    assert ledger.filled_count_fp100(Side.A) == before_a
    assert ledger._mutation_generation == before_gen


# ── 5. Auto-adopt persist failure → ledger unchanged -----------------


def test_auto_adopt_persist_failure_leaves_ledger_unchanged() -> None:
    """On a detected mismatch where persist fails, the ledger must not
    mutate — the pre-reconcile state is retained and the caller sees ERROR.
    (F13 durable-before-success contract applies to the auto-adopt path.)
    """
    ledger = _make_ledger(matched_count_fp100=500)
    rest = _FakeRest(
        {
            "T-A": [_make_fill("T-A", 700)],
            "T-B": [_make_fill("T-B", 500)],
        }
    )
    before_a = ledger.filled_count_fp100(Side.A)
    before_b = ledger.filled_count_fp100(Side.B)
    before_gen = ledger._mutation_generation

    def persist_cb(snap: LedgerSnapshot, ticker: str) -> None:
        raise RuntimeError("disk full")

    result = asyncio.run(
        ledger.reconcile_from_fills(cast(KalshiRESTClient, rest), persist_cb)
    )
    assert result.outcome == ReconcileOutcome.ERROR
    assert "disk full" in (result.error or "")
    # Live state untouched — Kalshi's view not applied because persist failed.
    assert ledger.filled_count_fp100(Side.A) == before_a
    assert ledger.filled_count_fp100(Side.B) == before_b
    assert ledger._mutation_generation == before_gen


# ── 6. v11 single-event-loop atomicity ------------------------------


def test_v11_atomicity_blocks_concurrent_record_fill() -> None:
    """While the reconcile mutation phase is in progress (simulated
    as a slow sync block via monkey-patched _apply_snapshot that sleeps
    synchronously), a concurrent record_fill coroutine must NOT interleave.

    Because the mutation phase is sync with no await, the event loop
    cannot schedule other coroutines until the block completes. Final
    state = rebuild + fill, not a mix.
    """
    ledger = _make_ledger(matched_count_fp100=500)
    rest = _FakeRest(
        {
            "T-A": [_make_fill("T-A", 500)],
            "T-B": [_make_fill("T-B", 500)],
        }
    )

    observed_order: list[str] = []
    original_apply = ledger._apply_snapshot

    def slow_apply(snap: LedgerSnapshot) -> None:
        observed_order.append("apply_start")
        time.sleep(0.05)  # sync sleep simulates slow disk-applied block
        original_apply(snap)
        observed_order.append("apply_end")

    ledger._apply_snapshot = slow_apply  # type: ignore[method-assign]

    async def _main() -> None:
        async def _reconcile() -> None:
            await ledger.reconcile_from_fills(cast(KalshiRESTClient, rest), lambda s, t: None)

        async def _fill_later() -> None:
            # Yield so reconcile starts first.
            await asyncio.sleep(0.001)
            observed_order.append("fill_start")
            ledger.record_fill_bps(Side.A, count_fp100=100, price_bps=4800)
            observed_order.append("fill_end")

        await asyncio.gather(_reconcile(), _fill_later())

    asyncio.run(_main())

    # record_fill must not have run between apply_start and apply_end.
    apply_start_idx = observed_order.index("apply_start")
    apply_end_idx = observed_order.index("apply_end")
    fill_start_idx = observed_order.index("fill_start")
    assert not (apply_start_idx < fill_start_idx < apply_end_idx), (
        f"record_fill interleaved with _apply_snapshot: {observed_order}"
    )
    # Final state = rebuild (500) + later record_fill (100) = 600.
    assert ledger.filled_count_fp100(Side.A) == 600


# ── 9. Mutator generation counter discipline ------------------------

MUTATORS: list[tuple[str, Any]] = [
    (
        "record_fill",
        lambda lg: lg.record_fill(Side.A, count=1, price=48),
    ),
    (
        "record_fill_bps",
        lambda lg: lg.record_fill_bps(Side.A, count_fp100=100, price_bps=4800),
    ),
    (
        "record_resting",
        lambda lg: lg.record_resting(Side.A, order_id="r-1", count=1, price=48),
    ),
    (
        "record_resting_bps",
        lambda lg: lg.record_resting_bps(Side.A, order_id="r-2", count_fp100=100, price_bps=4800),
    ),
    (
        "record_placement",
        lambda lg: lg.record_placement(Side.A, order_id="p-1", count=1, price=48),
    ),
    (
        "record_placement_bps",
        lambda lg: lg.record_placement_bps(Side.A, order_id="p-2", count_fp100=100, price_bps=4800),
    ),
    (
        "record_cancel",
        lambda lg: _setup_and_cancel(lg),
    ),
    (
        "mark_side_pending",
        lambda lg: lg.mark_side_pending(Side.A),
    ),
    (
        "mark_order_cancelled",
        lambda lg: lg.mark_order_cancelled("ord-x"),
    ),
    (
        "sync_from_orders",
        lambda lg: lg.sync_from_orders([], "T-A", "T-B"),
    ),
    (
        "sync_from_positions",
        lambda lg: lg.sync_from_positions({Side.A: 0, Side.B: 0}, {Side.A: 0, Side.B: 0}),
    ),
    (
        "seed_from_saved",
        lambda lg: lg.seed_from_saved({"filled_a": 0}),
    ),
]


def _setup_and_cancel(ledger: PositionLedger) -> None:
    ledger.record_resting_bps(Side.B, order_id="c-1", count_fp100=100, price_bps=4800)
    # That bumped the counter once. We want to measure ONLY the cancel.
    before = ledger._mutation_generation
    ledger.record_cancel(Side.B, "c-1")
    # If record_cancel bumps by exactly 1, the outer assertion will see
    # the +2 total. Expose the fact that we measure the final delta by
    # staging a throwaway post-mutation check.
    assert ledger._mutation_generation == before + 1


@pytest.mark.parametrize("name, op", MUTATORS, ids=[m[0] for m in MUTATORS])
def test_each_sync_mutator_bumps_generation_by_one(name: str, op: Any) -> None:
    """Every sync mutator bumps _mutation_generation by exactly 1 per call."""
    ledger = PositionLedger("EVT", unit_size=10, ticker_a="T-A", ticker_b="T-B")
    # For record_cancel we need an existing resting order in a prior step
    # that also bumps the counter. The helper asserts bump-by-1 internally.
    before = ledger._mutation_generation
    op(ledger)
    after = ledger._mutation_generation
    # record_cancel helper sets up + cancels (2 bumps); everything else is 1.
    expected_delta = 2 if name == "record_cancel" else 1
    assert after - before == expected_delta, (
        f"{name}: generation delta {after - before}, expected {expected_delta}"
    )


# ── 10. No async lock regression guard ------------------------------


def test_position_ledger_has_no_mutation_lock() -> None:
    """Regression guard: v6-v10 drafts added an asyncio.Lock to guard
    mutations. v11 removes it — sync mutators under a single event loop
    are already atomic. Re-introducing the lock would collapse the
    atomicity argument and bring back the bugs Codex flagged."""
    ledger = PositionLedger("EVT", unit_size=10)
    assert not hasattr(ledger, "_mutation_lock"), (
        "PositionLedger has _mutation_lock — v11 removed this; "
        "re-adding it collapses the sync-mutator atomicity argument."
    )


# ── Extra: sync persist_cb contract regression ----------------------


def test_reconcile_accepts_plain_sync_persist_cb() -> None:
    """persist_cb MUST be sync — making it async would reintroduce the
    await window v11 eliminated. This test holds the line by calling
    reconcile with a plain `def` and asserting success."""
    ledger = _make_ledger(matched_count_fp100=500)
    rest = _FakeRest(
        {
            "T-A": [_make_fill("T-A", 500)],
            "T-B": [_make_fill("T-B", 500)],
        }
    )

    def plain_sync_persist(snap: LedgerSnapshot, ticker: str) -> None:
        return None

    result = asyncio.run(
        ledger.reconcile_from_fills(cast(KalshiRESTClient, rest), plain_sync_persist)
    )
    assert result.outcome == ReconcileOutcome.OK
