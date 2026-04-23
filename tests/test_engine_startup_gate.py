"""Task 6b-2: engine startup gate + cancel_order_with_verify (F31+F33).

Tests cover:

1. F31 cancel bypasses the startup gate even with stale_fills_unconfirmed.
2. F31 cancel during reconcile_mismatch_pending — cancel still works.
3. F33 stale-first-ID 404 triggers full resync, not blind-clear.
4. F33 network error on get_order falls through to attempted cancel.
5. F33 race: get_order returns live, cancel returns 404 — resync.
6. Auto-reconcile fires after AUTO_RECONCILE_DELAY_S when flag sticks.
7. Gate times out at STARTUP_SYNC_TIMEOUT_S and notifies error.
8. legacy_migration_pending triggers immediate error return.
9. Fresh pair with ``_first_orders_sync`` set returns True immediately.
10. Successful create_order flow after gate clears mid-wait.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from talos.engine import TradingEngine
from talos.errors import KalshiAPIError, KalshiNotFoundError
from talos.models.order import Order
from talos.models.strategy import ArbPair
from talos.position_ledger import PositionLedger, Side

# ── Fixtures ────────────────────────────────────────────────────────


def _make_pair(
    event_ticker: str = "EVT-1",
    ticker_a: str = "MKT-A",
    ticker_b: str = "MKT-B",
) -> ArbPair:
    return ArbPair(
        talos_id=1,
        event_ticker=event_ticker,
        ticker_a=ticker_a,
        ticker_b=ticker_b,
        side_a="no",
        side_b="no",
    )


def _make_ledger(
    event_ticker: str = "EVT-1",
    ticker_a: str = "MKT-A",
    ticker_b: str = "MKT-B",
) -> PositionLedger:
    return PositionLedger(
        event_ticker=event_ticker,
        unit_size=10,
        ticker_a=ticker_a,
        ticker_b=ticker_b,
    )


def _make_engine_stub(
    ledger: PositionLedger,
    rest: Any = None,
) -> Any:
    """Build a TradingEngine-shaped stub sufficient for gate/cancel tests.

    Uses :meth:`TradingEngine.__new__` (bypasses __init__) and wires only
    the attributes the code under test accesses.
    """
    eng = cast(Any, TradingEngine.__new__(TradingEngine))
    eng._rest = rest if rest is not None else AsyncMock()
    eng._adjuster = MagicMock()
    eng._adjuster.get_ledger = MagicMock(return_value=ledger)
    eng._game_manager = MagicMock()
    eng._game_manager.active_games = []
    eng._game_manager.labels = {}
    eng._game_manager.subtitles = {}
    eng._game_manager.volumes_24h = {}
    eng.on_notification = None
    eng._notifications: list[tuple[str, str]] = []

    def _notify(message: str, severity: str = "information", *, toast: bool = False) -> None:
        eng._notifications.append((message, severity))

    eng._notify = _notify  # type: ignore[attr-defined]
    return eng


def _mk_order(
    order_id: str = "ord-1",
    ticker: str = "MKT-A",
    status: str = "resting",
    remaining_count: int = 1,
) -> Order:
    """Build a minimal ``Order`` for cancel/resync tests."""
    return Order.model_validate(
        {
            "order_id": order_id,
            "ticker": ticker,
            "status": status,
            "action": "buy",
            "side": "no",
            "type": "limit",
            "remaining_count": remaining_count,
            "fill_count": 0,
        }
    )


# ── 1. F31: cancel bypasses gate with stale_fills_unconfirmed ───────


@pytest.mark.asyncio
async def test_cancel_bypasses_gate_when_stale_fills_flag_set() -> None:
    """F31: cancel_order_with_verify must run even while ledger not ready."""
    pair = _make_pair()
    ledger = _make_ledger()
    ledger.stale_fills_unconfirmed = True  # would block create/amend

    rest = AsyncMock()
    rest.get_order.return_value = _mk_order(status="resting")
    rest.cancel_order.return_value = _mk_order(status="canceled")
    rest.get_orders.return_value = []

    eng = _make_engine_stub(ledger, rest)
    # Should NOT raise and NOT notify gate-block.
    await eng.cancel_order_with_verify("ord-1", pair)
    rest.cancel_order.assert_awaited_once_with("ord-1")
    # No error notification logged.
    assert not any(sev == "error" for _, sev in eng._notifications)


# ── 2. F31 cancel during reconcile_mismatch_pending ─────────────────


@pytest.mark.asyncio
async def test_cancel_bypasses_gate_during_reconcile_mismatch() -> None:
    """Cancel works even when operator-action-required flag is set."""
    pair = _make_pair()
    ledger = _make_ledger()
    ledger.reconcile_mismatch_pending = True
    # Gate would immediately reject create_order here.
    assert not await _eng_with_gate(ledger, pair, op="create_order")

    rest = AsyncMock()
    rest.get_order.return_value = _mk_order(status="resting")
    rest.cancel_order.return_value = _mk_order(status="canceled")
    rest.get_orders.return_value = []

    eng = _make_engine_stub(ledger, rest)
    await eng.cancel_order_with_verify("ord-1", pair)
    rest.cancel_order.assert_awaited_once()


async def _eng_with_gate(
    ledger: PositionLedger,
    pair: ArbPair,
    op: str = "create_order",
) -> bool:
    """Invoke ``_wait_for_ledger_ready`` with a 0.1s override timeout.

    We monkey-patch the module-level constant via a wrapper to keep tests
    fast; the path we care about (operator-action-required early exit)
    returns before any sleep.
    """
    eng = _make_engine_stub(ledger)
    return await eng._wait_for_ledger_ready(pair, op)


# ── 3. F33: stale-first-ID 404 triggers resync ──────────────────────


@pytest.mark.asyncio
async def test_stale_order_id_404_triggers_resync_not_blind_clear() -> None:
    """F33: a 404 on ``get_order`` must call sync_from_orders, not clear
    the ledger's resting state optimistically.

    Simulates: ledger thinks order "ord-stale" is resting on side A, but
    Kalshi returns 404. cancel_order_with_verify must fetch orders for
    both tickers and reconcile, rather than assuming the side is empty.
    """
    pair = _make_pair()
    ledger = _make_ledger()

    # Ledger believes side A has a resting order.
    ledger.record_placement(Side.A, "ord-stale", count=5, price=45)

    # Second resting order on side A that Kalshi still has (simulates the
    # F33 scenario: first tracked ID gone, others live).
    live_order = _mk_order(order_id="ord-live", ticker="MKT-A", remaining_count=3)

    rest = AsyncMock()
    rest.get_order.side_effect = KalshiNotFoundError(message="not found")
    rest.cancel_order = AsyncMock()

    def _get_orders(*, ticker: str, status: str) -> list[Order]:  # type: ignore[no-untyped-def]
        if ticker == "MKT-A":
            return [live_order]
        return []

    rest.get_orders.side_effect = _get_orders

    eng = _make_engine_stub(ledger, rest)
    await eng.cancel_order_with_verify("ord-stale", pair)

    # No raw cancel was attempted — we never reached phase 2.
    rest.cancel_order.assert_not_awaited()

    # Resync ran on both tickers.
    assert rest.get_orders.await_count == 2

    # Ledger now reflects ground truth: ord-live is the resting order.
    assert ledger.resting_order_id(Side.A) == "ord-live"


# ── 4. F33: non-404 network error on get_order falls through ────────


@pytest.mark.asyncio
async def test_network_error_on_get_order_falls_through_to_cancel() -> None:
    """Non-404 API error during probe must NOT skip the cancel attempt."""
    pair = _make_pair()
    ledger = _make_ledger()

    rest = AsyncMock()
    rest.get_order.side_effect = KalshiAPIError(status_code=503, body=None)
    rest.cancel_order.return_value = _mk_order(status="canceled")
    rest.get_orders.return_value = []

    eng = _make_engine_stub(ledger, rest)
    await eng.cancel_order_with_verify("ord-1", pair)

    rest.cancel_order.assert_awaited_once_with("ord-1")


# ── 5. F33 race: probe returns live, cancel returns 404 ─────────────


@pytest.mark.asyncio
async def test_race_probe_live_cancel_404_triggers_resync() -> None:
    """Order was resting at probe but gone by cancel time — resync runs."""
    pair = _make_pair()
    ledger = _make_ledger()
    ledger.record_placement(Side.A, "ord-raced", count=5, price=45)

    rest = AsyncMock()
    rest.get_order.return_value = _mk_order(order_id="ord-raced", status="resting")
    rest.cancel_order.side_effect = KalshiNotFoundError(message="gone")
    rest.get_orders.return_value = []

    eng = _make_engine_stub(ledger, rest)
    await eng.cancel_order_with_verify("ord-raced", pair)

    rest.cancel_order.assert_awaited_once()
    # Resync fetched (even if empty) — ledger's resting cleared by
    # sync_from_orders seeing the order gone.
    assert rest.get_orders.await_count == 2


# ── 6. Auto-reconcile fires at 5s when stale flag persists ──────────


@pytest.mark.asyncio
async def test_auto_reconcile_triggers_after_delay(monkeypatch: Any) -> None:
    """When stale_fills_unconfirmed sticks, reconcile_from_fills is called
    once the elapsed time crosses AUTO_RECONCILE_DELAY_S."""
    pair = _make_pair()
    ledger = _make_ledger()
    ledger.stale_fills_unconfirmed = True
    ledger._first_orders_sync.set()  # gate would otherwise fail on missing sync

    # Shrink timeouts so the test is fast. 1.0s total, 0.05s reconcile
    # threshold — must be well under the engine's 0.2s sleep cap so the
    # trigger fires before timeout.
    monkeypatch.setattr("talos.engine.STARTUP_SYNC_TIMEOUT_S", 1.0)
    monkeypatch.setattr("talos.engine.AUTO_RECONCILE_DELAY_S", 0.05)

    calls: list[tuple[Any, Any]] = []

    async def _fake_reconcile(rest: Any, persist_cb: Any) -> Any:
        calls.append((rest, persist_cb))
        # Clear the flag so gate ready() becomes True.
        ledger.stale_fills_unconfirmed = False
        return MagicMock()

    monkeypatch.setattr(ledger, "reconcile_from_fills", _fake_reconcile)

    eng = _make_engine_stub(ledger)
    ok = await eng._wait_for_ledger_ready(pair, "create_order")
    assert ok
    assert len(calls) == 1
    # persist_cb is the engine's sync callback (bound method — compare
    # by __func__ identity, since each attribute access creates a fresh
    # bound method object).
    assert calls[0][1].__func__ is TradingEngine._persist_games_now


# ── 7. Timeout at STARTUP_SYNC_TIMEOUT_S ────────────────────────────


@pytest.mark.asyncio
async def test_gate_times_out_and_notifies(monkeypatch: Any) -> None:
    pair = _make_pair()
    ledger = _make_ledger()
    ledger.stale_fills_unconfirmed = True
    ledger._first_orders_sync.set()

    # Short timeout; reconcile disabled by setting delay larger than timeout.
    monkeypatch.setattr("talos.engine.STARTUP_SYNC_TIMEOUT_S", 0.1)
    monkeypatch.setattr("talos.engine.AUTO_RECONCILE_DELAY_S", 10.0)

    eng = _make_engine_stub(ledger)
    ok = await eng._wait_for_ledger_ready(pair, "create_order")
    assert ok is False
    assert any(
        "blocked" in msg.lower() and sev == "error"
        for msg, sev in eng._notifications
    )


# ── 8. legacy_migration_pending early exit ──────────────────────────


@pytest.mark.asyncio
async def test_legacy_migration_pending_returns_false_immediately() -> None:
    pair = _make_pair()
    ledger = _make_ledger()
    ledger.legacy_migration_pending = True

    eng = _make_engine_stub(ledger)
    # Should return without sleeping — verify by wall-clock.
    loop = asyncio.get_event_loop()
    start = loop.time()
    ok = await eng._wait_for_ledger_ready(pair, "create_order")
    elapsed = loop.time() - start
    assert ok is False
    assert elapsed < 0.1  # bail immediately
    assert any(
        "confirm" in msg.lower() and sev == "error"
        for msg, sev in eng._notifications
    )


# ── 9. Fresh pair with _first_orders_sync set returns True ──────────


@pytest.mark.asyncio
async def test_fresh_pair_ready_returns_true_immediately() -> None:
    pair = _make_pair()
    ledger = _make_ledger()
    ledger._first_orders_sync.set()
    # All flags default-False.
    assert ledger.ready()

    eng = _make_engine_stub(ledger)
    loop = asyncio.get_event_loop()
    start = loop.time()
    ok = await eng._wait_for_ledger_ready(pair, "create_order")
    elapsed = loop.time() - start
    assert ok is True
    assert elapsed < 0.05


# ── 10. Successful create flow after gate clears mid-wait ───────────


@pytest.mark.asyncio
async def test_gate_clears_midwait_and_returns_true(monkeypatch: Any) -> None:
    """Ledger is stale at entry; flag clears externally mid-wait; gate
    returns True and caller can proceed."""
    pair = _make_pair()
    ledger = _make_ledger()
    ledger.stale_fills_unconfirmed = True
    ledger._first_orders_sync.set()

    monkeypatch.setattr("talos.engine.STARTUP_SYNC_TIMEOUT_S", 1.0)
    monkeypatch.setattr("talos.engine.AUTO_RECONCILE_DELAY_S", 10.0)

    eng = _make_engine_stub(ledger)

    async def _clear_flag_later() -> None:
        await asyncio.sleep(0.05)
        ledger.stale_fills_unconfirmed = False

    clearer = asyncio.create_task(_clear_flag_later())
    ok = await eng._wait_for_ledger_ready(pair, "create_order")
    await clearer
    assert ok is True


# ── Extra: missing ledger (KeyError) returns True (fresh pair) ──────


@pytest.mark.asyncio
async def test_missing_ledger_treated_as_ready() -> None:
    pair = _make_pair()
    eng = cast(Any, TradingEngine.__new__(TradingEngine))
    eng._adjuster = MagicMock()
    eng._adjuster.get_ledger = MagicMock(side_effect=KeyError("no ledger"))
    eng._notifications = []
    eng._notify = lambda *a, **k: None  # type: ignore[attr-defined]
    ok = await eng._wait_for_ledger_ready(pair, "create_order")
    assert ok is True
