"""Tests for the centralized market-shape admission guard.

Phase 0 (historical) rejected ``fractional_trading_enabled`` and sub-cent-tick
markets while the bps/fp100 migration was in flight. Task 12 relaxed those
two guards — the function, :class:`MarketAdmissionError`, the 5 ingress-path
integrations, and the F32 quarantine-restore path all remain in place for
future shape invariants, but the Phase 0 shape classes now flow through
admission unchanged. Tests that previously asserted ``pytest.raises`` now
assert the function returns cleanly.

CommitResult / ingress-path-wiring tests that need admission to raise keep
working by patching ``validate_market_for_admission`` directly.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from talos.game_manager import MarketAdmissionError, validate_market_for_admission
from talos.models.market import Market


def _cent_market(ticker: str = "KXA-26JAN01-A") -> Market:
    return Market(
        ticker=ticker,
        event_ticker="KXA-26JAN01",
        title=f"Market {ticker}",
        status="open",
    )


def _fractional_market(ticker: str = "KXF-26JAN01-A") -> Market:
    return Market(
        ticker=ticker,
        event_ticker="KXF-26JAN01",
        title=f"Fractional {ticker}",
        status="open",
        fractional_trading_enabled=True,
    )


def _subcent_market(ticker: str = "KXS-26JAN01-A") -> Market:
    return Market.model_validate(
        {
            "ticker": ticker,
            "event_ticker": "KXS-26JAN01",
            "title": f"Sub-cent {ticker}",
            "status": "open",
            "price_ranges": [
                {
                    "min_price_dollars": "0.01",
                    "max_price_dollars": "0.99",
                    "tick_dollars": "0.001",
                }
            ],
        }
    )


def test_accepts_two_cent_markets():
    a = _cent_market("KXA-26JAN01-A")
    b = _cent_market("KXA-26JAN01-B")
    validate_market_for_admission(a, b)


# ──────────────────────────────────────────────────────────────────
# Task 12 — Phase 0 admission relax: fractional + sub-cent now admitted.
# ──────────────────────────────────────────────────────────────────


def test_admits_fractional_trading_on_side_a():
    """Post-Task-12: fractional_trading_enabled markets flow through."""
    a = _fractional_market("KXF-26JAN01-A")
    b = _cent_market("KXA-26JAN01-B")
    validate_market_for_admission(a, b)  # must not raise


def test_admits_fractional_trading_on_side_b():
    a = _cent_market("KXA-26JAN01-A")
    b = _fractional_market("KXF-26JAN01-B")
    validate_market_for_admission(a, b)  # must not raise


def test_admits_sub_cent_tick_on_side_a():
    """Post-Task-12: sub-cent-tick markets flow through."""
    a = _subcent_market("KXS-26JAN01-A")
    b = _cent_market("KXA-26JAN01-B")
    validate_market_for_admission(a, b)  # must not raise


def test_admits_sub_cent_tick_on_side_b():
    a = _cent_market("KXA-26JAN01-A")
    b = _subcent_market("KXS-26JAN01-B")
    validate_market_for_admission(a, b)  # must not raise


def test_admits_fractional_and_sub_cent_combined():
    """Both Phase 0 shape classes on the same market now flow through."""
    a = Market.model_validate(
        {
            "ticker": "KXBOTH-26JAN01-A",
            "event_ticker": "KXBOTH-26JAN01",
            "title": "Both",
            "status": "open",
            "fractional_trading_enabled": True,
            "price_ranges": [
                {
                    "min_price_dollars": "0.01",
                    "max_price_dollars": "0.99",
                    "tick_dollars": "0.001",
                }
            ],
        }
    )
    b = _cent_market()
    validate_market_for_admission(a, b)  # must not raise


def test_sub_cent_market_admitted_and_pipeline_preserves_exact_bps():
    """Integration: a sub-cent market passes admission with its exact bps
    tick preserved through the Market model — scanner can then compute edges
    on the exact bps prices rather than cent-rounded values."""
    a = _subcent_market("KXS-26JAN01-A")
    b = _subcent_market("KXS-26JAN01-B")

    # Admission is a no-op post-Task-12.
    validate_market_for_admission(a, b)

    # Sanity: the Market model preserved the sub-cent tick through parsing,
    # which is the load-bearing property for scanner exact-edge computation.
    assert a.tick_bps() == 10, f"expected sub-cent tick=10 bps, got {a.tick_bps()}"
    assert b.tick_bps() == 10


# ──────────────────────────────────────────────────────────────────
# Scanner ingress — Phase 0 shapes now admitted.
# ──────────────────────────────────────────────────────────────────

from talos.orderbook import OrderBookManager  # noqa: E402
from talos.scanner import ArbitrageScanner  # noqa: E402


def test_scanner_admits_fractional_pair_and_logs_no_admission_skip(caplog):
    """Post-Task-12: the scanner no longer short-circuits fractional pairs.
    No ``scanner_admission_skip`` warning fires; the scan proceeds to
    evaluate the books (empty here, so no opportunity — but no guard log
    either)."""
    import logging

    caplog.set_level(logging.WARNING)

    books = OrderBookManager()
    scanner = ArbitrageScanner(books)
    scanner.add_pair(
        "KXF-26JAN01",
        "KXF-26JAN01-A",
        "KXF-26JAN01-B",
        fractional_trading_enabled=True,
    )

    scanner.scan("KXF-26JAN01-A")

    admission_warnings = [r for r in caplog.records if "admission" in r.message.lower()]
    assert admission_warnings == [], (
        f"expected no admission skip warnings post-Task-12, got "
        f"{[r.message for r in admission_warnings]}"
    )


def test_scanner_admits_subcent_pair(caplog):
    """Post-Task-12: a pair stored with tick_bps < 100 no longer triggers
    a scanner admission skip."""
    import logging

    caplog.set_level(logging.WARNING)

    books = OrderBookManager()
    scanner = ArbitrageScanner(books)
    scanner.add_pair(
        "KXS-26JAN01",
        "KXS-26JAN01-A",
        "KXS-26JAN01-B",
        tick_bps=10,
    )
    scanner.scan("KXS-26JAN01-A")

    admission_warnings = [r for r in caplog.records if "admission" in r.message.lower()]
    assert admission_warnings == []


def test_scanner_admits_ordinary_cent_pair(caplog):
    """Regression guard — the ordinary cent path is still admission-clean."""
    import logging

    caplog.set_level(logging.WARNING)
    books = OrderBookManager()
    scanner = ArbitrageScanner(books)
    scanner.add_pair("KXA-26JAN01", "KXA-26JAN01-A", "KXA-26JAN01-B")
    scanner.scan("KXA-26JAN01-A")
    admission_warnings = [r for r in caplog.records if "admission" in r.message.lower()]
    assert admission_warnings == []


# ──────────────────────────────────────────────────────────────────
# Engine.add_pairs_from_selection returns CommitResult
#
# The CommitResult machinery is general-purpose (for any future shape
# invariant). To exercise the rejected-path, we patch
# ``validate_market_for_admission`` at its engine import site so the
# guard raises on demand.
# ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_add_pairs_from_selection_returns_commit_result_with_mixed_batch(
    engine_fixture,
):
    """Mixed admitted/rejected batch returns a CommitResult, not a bare list.

    Uses a patched admission guard that rejects KXF- tickers so we still
    exercise the structural mixed-outcome path even after Task 12 relaxed
    the Phase 0 shape checks.
    """
    from talos.game_manager import CommitResult

    good_record = {
        "event_ticker": "KXA-26JAN01",
        "ticker_a": "KXA-26JAN01-A",
        "ticker_b": "KXA-26JAN01-B",
        "side_a": "no",
        "side_b": "no",
        "kalshi_event_ticker": "KXA-26JAN01",
        "series_ticker": "KXA",
        "fee_type": "quadratic_with_maker_fees",
        "fee_rate": 0.0175,
        "close_time": "2026-12-31T00:00:00Z",
        "expected_expiration_time": None,
    }
    bad_record = {
        "event_ticker": "KXF-26JAN01",
        "ticker_a": "KXF-26JAN01-A",
        "ticker_b": "KXF-26JAN01-B",
        "side_a": "no",
        "side_b": "no",
        "kalshi_event_ticker": "KXF-26JAN01",
        "series_ticker": "KXF",
        "fee_type": "quadratic_with_maker_fees",
        "fee_rate": 0.0175,
        "close_time": "2026-12-31T00:00:00Z",
        "expected_expiration_time": None,
    }

    def _raise_for_kxf(market_a: Market, market_b: Market) -> None:
        for m in (market_a, market_b):
            if m.ticker.startswith("KXF-"):
                raise MarketAdmissionError(f"{m.ticker}: test-only shape invariant violation")

    with patch(
        "talos.engine.validate_market_for_admission",
        side_effect=_raise_for_kxf,
    ):
        result = await engine_fixture.add_pairs_from_selection([good_record, bad_record])

    assert isinstance(result, CommitResult)
    assert len(result.admitted) == 1
    assert len(result.rejected) == 1
    rejected_record, rejected_error = result.rejected[0]
    assert rejected_record["event_ticker"] == "KXF-26JAN01"
    assert isinstance(rejected_error, MarketAdmissionError)


@pytest.mark.asyncio
async def test_add_pairs_from_selection_all_admitted(engine_fixture):
    """All-clean batch returns CommitResult with only admitted populated."""
    from talos.game_manager import CommitResult

    record = {
        "event_ticker": "KXA-26JAN01",
        "ticker_a": "KXA-26JAN01-A",
        "ticker_b": "KXA-26JAN01-B",
        "side_a": "no",
        "side_b": "no",
        "kalshi_event_ticker": "KXA-26JAN01",
        "series_ticker": "KXA",
        "fee_type": "quadratic_with_maker_fees",
        "fee_rate": 0.0175,
        "close_time": "2026-12-31T00:00:00Z",
        "expected_expiration_time": None,
    }
    result = await engine_fixture.add_pairs_from_selection([record])
    assert isinstance(result, CommitResult)
    assert len(result.admitted) == 1
    assert result.rejected == []


@pytest.mark.asyncio
async def test_add_pairs_from_selection_admits_sub_cent_post_task_12(
    engine_fixture,
):
    """Post-Task-12: a sub-cent record flows straight through admission
    and lands in ``admitted`` rather than ``rejected``."""
    from talos.game_manager import CommitResult

    subcent_record = {
        "event_ticker": "KXS-26JAN01",
        "ticker_a": "KXS-26JAN01-A",
        "ticker_b": "KXS-26JAN01-B",
        "side_a": "no",
        "side_b": "no",
        "kalshi_event_ticker": "KXS-26JAN01",
        "series_ticker": "KXS",
        "fee_type": "quadratic_with_maker_fees",
        "fee_rate": 0.0175,
        "close_time": "2026-12-31T00:00:00Z",
        "expected_expiration_time": None,
    }
    result = await engine_fixture.add_pairs_from_selection([subcent_record])
    assert isinstance(result, CommitResult)
    assert result.rejected == []
    assert len(result.admitted) == 1


# ──────────────────────────────────────────────────────────────────
# UI ingress (manual add + market picker) admission surfacing.
#
# Post-Task-12, the Phase 0 shape classes flow through. We still want
# regression coverage that the ingress-path plumbing surfaces admission
# errors correctly — we do that by patching the guard to raise.
# ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_game_manager_add_market_as_pair_admits_fractional_post_task_12():
    """Post-Task-12: fractional_trading_enabled markets flow through
    GameManager.add_market_as_pair — no admission error."""
    from unittest.mock import AsyncMock, MagicMock

    from talos.game_manager import GameManager
    from talos.models.market import Event

    rest = MagicMock()
    # Force the fee-metadata fetch to fall back to the hard-coded defaults
    # (quadratic_with_maker_fees / 0.0175). An AsyncMock()'s return value has
    # .fee_type = MagicMock() which fails ArbPair Pydantic validation, and we
    # don't care about the specific series here — the test is about
    # admission.
    rest.get_series = AsyncMock(side_effect=RuntimeError("unused in this test"))
    feed = MagicMock()
    feed.subscribe = AsyncMock()
    scanner = MagicMock()
    gm = GameManager(rest=rest, feed=feed, scanner=scanner)

    event = Event(
        event_ticker="KXF-26JAN01",
        series_ticker="KXF",
        title="Test event",
        sub_title="",
        category="test",
        markets=[],
    )
    fractional_market = Market(
        ticker="KXF-26JAN01-A",
        event_ticker="KXF-26JAN01",
        title="Fractional",
        status="open",
        fractional_trading_enabled=True,
    )
    # Must not raise — the call reaches scanner.add_pair.
    pair = await gm.add_market_as_pair(event, fractional_market)
    assert pair is not None
    scanner.add_pair.assert_called_once()


@pytest.mark.asyncio
async def test_add_market_pairs_surfaces_rejection_notification(engine_fixture):
    """Ingress-path wiring regression: when the admission guard raises
    (for any future shape invariant), engine.add_market_pairs surfaces a
    consolidated error toast listing the rejected ticker(s). The OK market
    still flows through successfully.

    Uses a patched guard to exercise the wiring after Task 12 relaxed the
    Phase 0 shape checks.
    """
    from talos.models.market import Event
    from talos.models.strategy import ArbPair

    def _raise_for_kxf(market_a: Market, market_b: Market) -> None:
        for m in (market_a, market_b):
            if m.ticker.startswith("KXF-"):
                raise MarketAdmissionError(f"{m.ticker}: test-only shape invariant violation")

    async def _add_market_as_pair(event, market):
        # Route through the (patched) guard so the structured-rejection path
        # exercises the same code that real ingress would.
        from talos.game_manager import (
            validate_market_for_admission as _guard,
        )

        _guard(market, market)
        return ArbPair(
            event_ticker=market.ticker,
            ticker_a=market.ticker,
            ticker_b=market.ticker,
            side_a="yes",
            side_b="no",
        )

    engine_fixture._game_manager.add_market_as_pair = _add_market_as_pair

    captured: list[tuple[str, str, bool]] = []

    def _capture_notify(msg, severity="information", *, toast=False):
        captured.append((msg, severity, toast))

    engine_fixture._notify = _capture_notify

    event = Event(
        event_ticker="KXF-26JAN01",
        series_ticker="KXF",
        title="Test event",
        sub_title="",
        category="test",
        markets=[],
    )
    fractional_market = Market(
        ticker="KXF-26JAN01-A",
        event_ticker="KXF-26JAN01",
        title="Fractional",
        status="open",
        fractional_trading_enabled=True,
    )
    ok_market = Market(
        ticker="KXA-26JAN01-A",
        event_ticker="KXA-26JAN01",
        title="OK",
        status="open",
    )

    with patch(
        "talos.game_manager.validate_market_for_admission",
        side_effect=_raise_for_kxf,
    ):
        pairs = await engine_fixture.add_market_pairs(
            event,
            [fractional_market, ok_market],
        )

    # The OK market succeeded.
    assert len(pairs) == 1
    assert pairs[0].ticker_a == "KXA-26JAN01-A"

    # A rejection notification fired, mentioning the fractional ticker and
    # with severity=error.
    rejection_notifs = [
        (msg, sev) for msg, sev, _toast in captured if sev == "error" and "KXF-26JAN01-A" in msg
    ]
    assert rejection_notifs, (
        f"expected rejection notification mentioning KXF-26JAN01-A, got {captured}"
    )


@pytest.mark.asyncio
async def test_add_games_surfaces_admission_rejection_as_specific_toast(
    engine_fixture,
):
    """Ingress-path wiring regression: engine.add_games catches
    MarketAdmissionError from the underlying game_manager.add_game path and
    surfaces a 'Market rejected (admission guard): ...' toast rather than
    the generic 'Error: ...' path.

    The guard is stubbed at the game_manager layer (add_games raises
    directly) so this test is independent of which specific shape
    invariant is being enforced — it only verifies the toast wiring.
    """

    async def _raise(urls):
        raise MarketAdmissionError("KXF-26JAN01-A: test-only shape invariant violation")

    engine_fixture._game_manager.add_games = _raise

    captured: list[tuple[str, str, bool]] = []

    def _capture_notify(msg, severity="information", *, toast=False):
        captured.append((msg, severity, toast))

    engine_fixture._notify = _capture_notify

    result = await engine_fixture.add_games(["https://kalshi.com/x"])

    assert result == []
    rejection_notifs = [
        (msg, sev)
        for msg, sev, _toast in captured
        if sev == "error" and "admission guard" in msg.lower()
    ]
    assert rejection_notifs, f"expected 'admission guard' rejection notification, got {captured}"
    assert any("Market rejected" in msg for msg, _sev, _toast in captured), (
        f"expected 'Market rejected' prefix, got {captured}"
    )
