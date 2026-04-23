"""Confirmation-source → flag-clear matrix (spec F15 + F17 + F20).

Each authoritative source clears only the flags its evidence covers. The
F20 negative regression is the core of this file: ``sync_from_orders``
with a matching response must NOT clear ``stale_fills_unconfirmed`` or
``legacy_migration_pending`` — orders-endpoint data is archival-incomplete
for historical economics.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, cast

import pytest

from talos.position_ledger import (
    LedgerSnapshot,
    PositionLedger,
    ReconcileOutcome,
    Side,
)
from talos.rest_client import KalshiRESTClient

# ── Minimal Order stand-in for sync_from_orders -----------------------


@dataclass
class _FakeOrder:
    order_id: str
    ticker: str
    action: str = "buy"
    side: str = "no"
    status: str = "resting"
    fill_count_fp100: int = 0
    remaining_count_fp100: int = 0
    maker_fill_cost_bps: int = 0
    taker_fill_cost_bps: int = 0
    maker_fees_bps: int = 0
    no_price_bps: int = 0
    yes_price_bps: int = 0


# ── Fake REST client for reconcile flows -----------------------------


class _FakeRest:
    def __init__(self, fills_by_ticker: dict[str, list[Any]]) -> None:
        self._fills = fills_by_ticker
        self.calls: list[str] = []

    async def get_all_fills(
        self,
        *,
        ticker: str | None = None,
        order_id: str | None = None,
    ) -> list[Any]:
        assert ticker is not None
        self.calls.append(ticker)
        return list(self._fills.get(ticker, []))


def _seed_ledger_with_historical_state() -> PositionLedger:
    """Factory: a ledger with matched-pair historical state + v1 pending."""
    ledger = PositionLedger(
        "EVT",
        unit_size=10,
        ticker_a="T-A",
        ticker_b="T-B",
    )
    # Legacy v1 load — nonzero, so legacy_migration_pending=True and
    # stale_fills_unconfirmed=True. No resting state → resting flag stays False.
    ledger.seed_from_saved({"filled_a": 5, "cost_a": 240, "filled_b": 5, "cost_b": 240})
    assert ledger.stale_fills_unconfirmed is True
    assert ledger.legacy_migration_pending is True
    assert ledger.stale_resting_unconfirmed is False
    return ledger


def _persist_noop(snapshot: LedgerSnapshot, event_ticker: str) -> None:
    """persist_cb stub — reconcile tests that just care about state machine."""
    _ = snapshot
    _ = event_ticker


# ── sync_from_positions: never clears any flag (F15) ----------------


class TestSyncFromPositionsClearsNothing:
    def test_positions_alone_clears_no_flag(self) -> None:
        ledger = _seed_ledger_with_historical_state()
        # cross-ticker so sync_from_positions doesn't early-return
        ledger.sync_from_positions(
            {Side.A: 5, Side.B: 5},
            {Side.A: 240, Side.B: 240},
        )
        assert ledger.stale_fills_unconfirmed is True
        assert ledger.stale_resting_unconfirmed is False
        assert ledger.legacy_migration_pending is True


# ── sync_from_orders: clears only resting (F17 + F20) ----------------


class TestSyncFromOrdersClearsOnlyResting:
    def test_empty_response_clears_resting_only(self) -> None:
        ledger = PositionLedger(
            "EVT",
            unit_size=10,
            ticker_a="T-A",
            ticker_b="T-B",
        )
        ledger.seed_from_saved(
            {
                "filled_a": 5,
                "cost_a": 240,
                "filled_b": 5,
                "cost_b": 240,
                "resting_id_a": "ord-1",
                "resting_count_a": 3,
                "resting_price_a": 48,
            }
        )
        assert ledger.stale_fills_unconfirmed is True
        assert ledger.stale_resting_unconfirmed is True
        assert ledger.legacy_migration_pending is True

        ledger.sync_from_orders([], "T-A", "T-B")
        assert ledger.stale_resting_unconfirmed is False
        assert ledger.stale_fills_unconfirmed is True
        assert ledger.legacy_migration_pending is True

    def test_matching_response_still_does_not_clear_fills_or_legacy(self) -> None:
        """F20 NEGATIVE regression: counts-agree is not authoritative for
        historical economics (orders can be archived). stale_fills and
        legacy_migration_pending MUST stay set."""
        ledger = _seed_ledger_with_historical_state()
        # Feed orders that match the loaded state perfectly on count/cost.
        orders = [
            _FakeOrder(
                order_id="o-a",
                ticker="T-A",
                fill_count_fp100=500,
                maker_fill_cost_bps=24000,
                status="executed",
            ),
            _FakeOrder(
                order_id="o-b",
                ticker="T-B",
                fill_count_fp100=500,
                maker_fill_cost_bps=24000,
                status="executed",
            ),
        ]
        ledger.sync_from_orders(orders, "T-A", "T-B")
        assert ledger.stale_resting_unconfirmed is False
        # F20 negative: these do NOT clear.
        assert ledger.stale_fills_unconfirmed is True
        assert ledger.legacy_migration_pending is True


# ── reconcile_from_fills OK: clears fills + legacy (not resting) ----


class TestReconcileFromFillsClearsFillsAndLegacy:
    def test_successful_reconcile_clears_fills_and_legacy_only(self) -> None:
        ledger = _seed_ledger_with_historical_state()
        # Load resting-staleness manually so we can prove it survives.
        ledger.stale_resting_unconfirmed = True

        # Matching fills: 5 contracts at $0.48 each per side.
        fill_a = {
            "trade_id": "t-1",
            "order_id": "o-a",
            "ticker": "T-A",
            "side": "no",
            "action": "buy",
            "count_fp100": 500,
            "no_price_bps": 4800,
            "fee_cost_bps": 0,
        }
        fill_b = {
            "trade_id": "t-2",
            "order_id": "o-b",
            "ticker": "T-B",
            "side": "no",
            "action": "buy",
            "count_fp100": 500,
            "no_price_bps": 4800,
            "fee_cost_bps": 0,
        }
        from talos.models.order import Fill

        rest = _FakeRest(
            {
                "T-A": [Fill.model_validate(fill_a)],
                "T-B": [Fill.model_validate(fill_b)],
            }
        )

        result = asyncio.run(
            ledger.reconcile_from_fills(cast(KalshiRESTClient, rest), _persist_noop)
        )
        assert result.outcome == ReconcileOutcome.OK
        assert ledger.stale_fills_unconfirmed is False
        assert ledger.legacy_migration_pending is False
        # Resting flag is NOT cleared by fills reconcile.
        assert ledger.stale_resting_unconfirmed is True


# ── accept_pending_mismatch: clears fills + legacy -------------------


class TestAcceptPendingMismatchClearsFillsAndLegacy:
    def test_accept_clears_fills_and_legacy_only(self) -> None:
        ledger = _seed_ledger_with_historical_state()
        ledger.stale_resting_unconfirmed = True

        # Mismatching fills: 6 on side A, 5 on side B → mismatch vs loaded 5/5.
        from talos.models.order import Fill

        rest = _FakeRest(
            {
                "T-A": [
                    Fill.model_validate(
                        {
                            "trade_id": "t-1",
                            "order_id": "o-a",
                            "ticker": "T-A",
                            "side": "no",
                            "action": "buy",
                            "count_fp100": 600,
                            "no_price_bps": 4800,
                            "fee_cost_bps": 0,
                        }
                    ),
                ],
                "T-B": [
                    Fill.model_validate(
                        {
                            "trade_id": "t-2",
                            "order_id": "o-b",
                            "ticker": "T-B",
                            "side": "no",
                            "action": "buy",
                            "count_fp100": 500,
                            "no_price_bps": 4800,
                            "fee_cost_bps": 0,
                        }
                    ),
                ],
            }
        )

        result = asyncio.run(
            ledger.reconcile_from_fills(cast(KalshiRESTClient, rest), _persist_noop)
        )
        assert result.outcome == ReconcileOutcome.MISMATCH
        # Mismatch captured, but no flags cleared yet.
        assert ledger.stale_fills_unconfirmed is True
        assert ledger.legacy_migration_pending is True
        assert ledger.reconcile_mismatch_pending is True

        asyncio.run(ledger.accept_pending_mismatch(_persist_noop))
        assert ledger.stale_fills_unconfirmed is False
        assert ledger.legacy_migration_pending is False
        assert ledger.reconcile_mismatch_pending is False
        assert ledger.stale_resting_unconfirmed is True  # NOT cleared by accept


# ── Combined: orders + reconcile → all three clear -------------------


class TestCombinedOrdersPlusReconcileClearsAll:
    def test_orders_empty_plus_reconcile_ok(self) -> None:
        ledger = PositionLedger(
            "EVT",
            unit_size=10,
            ticker_a="T-A",
            ticker_b="T-B",
        )
        ledger.seed_from_saved(
            {
                "filled_a": 5,
                "cost_a": 240,
                "filled_b": 5,
                "cost_b": 240,
                "resting_id_a": "ord-1",
                "resting_count_a": 3,
                "resting_price_a": 48,
            }
        )
        assert ledger.stale_fills_unconfirmed is True
        assert ledger.stale_resting_unconfirmed is True
        assert ledger.legacy_migration_pending is True

        ledger.sync_from_orders([], "T-A", "T-B")
        assert ledger.stale_resting_unconfirmed is False

        from talos.models.order import Fill

        rest = _FakeRest(
            {
                "T-A": [
                    Fill.model_validate(
                        {
                            "trade_id": "t-1",
                            "order_id": "o-a",
                            "ticker": "T-A",
                            "side": "no",
                            "action": "buy",
                            "count_fp100": 500,
                            "no_price_bps": 4800,
                            "fee_cost_bps": 0,
                        }
                    )
                ],
                "T-B": [
                    Fill.model_validate(
                        {
                            "trade_id": "t-2",
                            "order_id": "o-b",
                            "ticker": "T-B",
                            "side": "no",
                            "action": "buy",
                            "count_fp100": 500,
                            "no_price_bps": 4800,
                            "fee_cost_bps": 0,
                        }
                    )
                ],
            }
        )
        result = asyncio.run(
            ledger.reconcile_from_fills(cast(KalshiRESTClient, rest), _persist_noop)
        )
        assert result.outcome == ReconcileOutcome.OK
        assert ledger.stale_fills_unconfirmed is False
        assert ledger.legacy_migration_pending is False
        assert ledger.stale_resting_unconfirmed is False
        assert ledger.ready() is True


# ── ready() gate semantics -------------------------------------------


class TestReadyGateSemantics:
    def test_ready_false_while_any_flag_set(self) -> None:
        ledger = PositionLedger("EVT", unit_size=10)
        # Fresh ledger: all flags False but _first_orders_sync not set.
        assert ledger.ready() is False

    @pytest.mark.parametrize(
        "flag",
        [
            "stale_fills_unconfirmed",
            "stale_resting_unconfirmed",
            "legacy_migration_pending",
            "reconcile_mismatch_pending",
        ],
    )
    def test_each_flag_blocks_ready(self, flag: str) -> None:
        ledger = PositionLedger("EVT", unit_size=10, ticker_a="T-A", ticker_b="T-B")
        ledger.sync_from_orders([], "T-A", "T-B")  # set _first_orders_sync
        assert ledger.ready() is True
        setattr(ledger, flag, True)
        assert ledger.ready() is False
