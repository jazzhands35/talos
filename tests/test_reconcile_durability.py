"""Reconcile durability + v11 atomicity tests (spec F11/F13/F16/F18/F19).

Covers the full reconcile state machine around the persist-before-apply
contract, generation-counter stale-mismatch detection, v11 sync-mutator
atomicity under single-event-loop asyncio, and the no-async-lock regression
guard.
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
    StaleMismatchError,
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


# ── 3. F16 mismatch state is not crash-durable ----------------------


def test_mismatch_state_is_in_session_only() -> None:
    ledger = _make_ledger(matched_count_fp100=500)
    # Rebuild with DIFFERENT state → MISMATCH.
    rest = _FakeRest(
        {
            "T-A": [_make_fill("T-A", 700)],
            "T-B": [_make_fill("T-B", 500)],
        }
    )

    def persist_cb(snap: LedgerSnapshot, ticker: str) -> None:
        pytest.fail("persist_cb must not run on MISMATCH path")

    result = asyncio.run(ledger.reconcile_from_fills(cast(KalshiRESTClient, rest), persist_cb))
    assert result.outcome == ReconcileOutcome.MISMATCH
    assert ledger.reconcile_mismatch_pending is True
    assert ledger._pending_mismatch is not None

    # Serialize the envelope — no reconcile_mismatch_pending, no rebuilt blob.
    env = ledger.to_save_dict()
    assert "reconcile_mismatch_pending" not in env
    assert "_pending_mismatch" not in env
    inner = env["ledger"]
    assert isinstance(inner, dict)
    assert "reconcile_mismatch_pending" not in inner
    assert "_pending_mismatch" not in inner

    # Reload a fresh ledger from that envelope → mismatch state not preserved.
    reloaded = PositionLedger("EVT", unit_size=10, ticker_a="T-A", ticker_b="T-B")
    reloaded.seed_from_saved(env)
    assert reloaded.reconcile_mismatch_pending is False
    assert reloaded._pending_mismatch is None


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


# ── 5. Successful accept + durable persist --------------------------


def test_successful_accept_applies_and_persists() -> None:
    ledger = _make_ledger(matched_count_fp100=500)
    # Force mismatch: rebuild shows 700 A, 500 B.
    rest = _FakeRest(
        {
            "T-A": [_make_fill("T-A", 700)],
            "T-B": [_make_fill("T-B", 500)],
        }
    )
    asyncio.run(ledger.reconcile_from_fills(cast(KalshiRESTClient, rest), lambda s, t: None))
    assert ledger.reconcile_mismatch_pending is True

    persisted: list[LedgerSnapshot] = []

    def persist_cb(snap: LedgerSnapshot, ticker: str) -> None:
        persisted.append(snap)

    asyncio.run(ledger.accept_pending_mismatch(persist_cb))
    assert ledger.reconcile_mismatch_pending is False
    assert ledger._pending_mismatch is None
    assert ledger.filled_count_fp100(Side.A) == 700
    assert ledger.filled_count_fp100(Side.B) == 500
    assert len(persisted) == 1
    assert persisted[0].filled_count_fp100_a == 700


# ── 6. Stale-mismatch-accept prevention (generation counter) --------


def test_stale_mismatch_accept_raises_and_clears_pending() -> None:
    ledger = _make_ledger(matched_count_fp100=500)
    rest = _FakeRest(
        {
            "T-A": [_make_fill("T-A", 700)],
            "T-B": [_make_fill("T-B", 500)],
        }
    )
    asyncio.run(ledger.reconcile_from_fills(cast(KalshiRESTClient, rest), lambda s, t: None))
    assert ledger.reconcile_mismatch_pending is True
    captured_gen = ledger._pending_mismatch_gen

    # Intervening mutation → generation bumps past captured_gen.
    ledger.record_fill(Side.A, count=1, price=48)
    assert ledger._mutation_generation != captured_gen

    def persist_cb(snap: LedgerSnapshot, ticker: str) -> None:
        pytest.fail("persist_cb must not run on stale-mismatch path")

    with pytest.raises(StaleMismatchError):
        asyncio.run(ledger.accept_pending_mismatch(persist_cb))

    # Pending state cleared; live mutation intact.
    assert ledger.reconcile_mismatch_pending is False
    assert ledger._pending_mismatch is None
    # record_fill added 1 contract (100 fp100) to the baseline 500.
    assert ledger.filled_count_fp100(Side.A) == 600


# ── 7. Accept persist failure → ledger unchanged, pending retained --


def test_accept_persist_failure_retains_pending() -> None:
    ledger = _make_ledger(matched_count_fp100=500)
    rest = _FakeRest(
        {
            "T-A": [_make_fill("T-A", 700)],
            "T-B": [_make_fill("T-B", 500)],
        }
    )
    asyncio.run(ledger.reconcile_from_fills(cast(KalshiRESTClient, rest), lambda s, t: None))
    before_a = ledger.filled_count_fp100(Side.A)

    def persist_cb(snap: LedgerSnapshot, ticker: str) -> None:
        raise RuntimeError("disk full")

    with pytest.raises(RuntimeError, match="disk full"):
        asyncio.run(ledger.accept_pending_mismatch(persist_cb))

    # Ledger unchanged; pending retained so operator can retry.
    assert ledger.filled_count_fp100(Side.A) == before_a
    assert ledger.reconcile_mismatch_pending is True
    assert ledger._pending_mismatch is not None


# ── 8. v11 single-event-loop atomicity ------------------------------


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
