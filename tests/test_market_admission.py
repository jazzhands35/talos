"""Tests for the centralized market-shape admission guard (Phase 0)."""

from __future__ import annotations

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
    return Market.model_validate({
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
    })


def test_accepts_two_cent_markets():
    a = _cent_market("KXA-26JAN01-A")
    b = _cent_market("KXA-26JAN01-B")
    validate_market_for_admission(a, b)


def test_rejects_fractional_trading_on_side_a():
    a = _fractional_market("KXF-26JAN01-A")
    b = _cent_market("KXA-26JAN01-B")
    with pytest.raises(MarketAdmissionError) as exc_info:
        validate_market_for_admission(a, b)
    assert "fractional" in str(exc_info.value).lower()
    assert "KXF-26JAN01-A" in str(exc_info.value)


def test_rejects_fractional_trading_on_side_b():
    a = _cent_market("KXA-26JAN01-A")
    b = _fractional_market("KXF-26JAN01-B")
    with pytest.raises(MarketAdmissionError) as exc_info:
        validate_market_for_admission(a, b)
    assert "KXF-26JAN01-B" in str(exc_info.value)


def test_rejects_sub_cent_tick_on_side_a():
    a = _subcent_market("KXS-26JAN01-A")
    b = _cent_market("KXA-26JAN01-B")
    with pytest.raises(MarketAdmissionError) as exc_info:
        validate_market_for_admission(a, b)
    assert "sub-cent" in str(exc_info.value).lower() or "tick" in str(exc_info.value).lower()
    assert "KXS-26JAN01-A" in str(exc_info.value)


def test_rejects_sub_cent_tick_on_side_b():
    a = _cent_market("KXA-26JAN01-A")
    b = _subcent_market("KXS-26JAN01-B")
    with pytest.raises(MarketAdmissionError) as exc_info:
        validate_market_for_admission(a, b)
    assert "KXS-26JAN01-B" in str(exc_info.value)


def test_rejects_fractional_even_if_sub_cent_also():
    """Either bad property triggers rejection — we don't require both."""
    a = Market.model_validate({
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
    })
    b = _cent_market()
    with pytest.raises(MarketAdmissionError):
        validate_market_for_admission(a, b)


# ──────────────────────────────────────────────────────────────────
# Scanner ingress
# ──────────────────────────────────────────────────────────────────

from talos.orderbook import OrderBookManager  # noqa: E402
from talos.scanner import ArbitrageScanner  # noqa: E402


def test_scanner_skips_fractional_pair_and_produces_no_opportunity(caplog):
    """Scanner rejects pairs whose stored shape violates admission invariants,
    logs exactly one WARNING per event ticker across multiple scan ticks."""
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

    # Scan multiple times — should dedup the warning.
    scanner.scan("KXF-26JAN01-A")
    scanner.scan("KXF-26JAN01-A")
    scanner.scan("KXF-26JAN01-B")

    assert scanner.opportunities == []
    admission_warnings = [r for r in caplog.records if "admission" in r.message.lower()]
    assert len(admission_warnings) == 1, (
        f"expected exactly one admission warning (dedup), got {len(admission_warnings)}"
    )


def test_scanner_skips_subcent_pair():
    """A pair stored with tick_bps < 100 triggers the guard."""
    books = OrderBookManager()
    scanner = ArbitrageScanner(books)
    scanner.add_pair(
        "KXS-26JAN01",
        "KXS-26JAN01-A",
        "KXS-26JAN01-B",
        tick_bps=10,
    )
    scanner.scan("KXS-26JAN01-A")
    assert scanner.opportunities == []


def test_scanner_admits_ordinary_cent_pair(caplog):
    """A pair with default (cent, non-fractional) shape passes the guard."""
    import logging
    caplog.set_level(logging.WARNING)
    books = OrderBookManager()
    scanner = ArbitrageScanner(books)
    scanner.add_pair("KXA-26JAN01", "KXA-26JAN01-A", "KXA-26JAN01-B")
    # No opportunities (books are empty) but no admission warning either.
    scanner.scan("KXA-26JAN01-A")
    admission_warnings = [r for r in caplog.records if "admission" in r.message.lower()]
    assert admission_warnings == []


# ──────────────────────────────────────────────────────────────────
# Engine.add_pairs_from_selection returns CommitResult
# ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_add_pairs_from_selection_returns_commit_result_with_mixed_batch(engine_fixture):
    """Mixed admitted/rejected batch returns a CommitResult, not a bare list."""
    from talos.game_manager import CommitResult, MarketAdmissionError

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
        "side_a": "no", "side_b": "no",
        "kalshi_event_ticker": "KXA-26JAN01",
        "series_ticker": "KXA",
        "fee_type": "quadratic_with_maker_fees", "fee_rate": 0.0175,
        "close_time": "2026-12-31T00:00:00Z", "expected_expiration_time": None,
    }
    result = await engine_fixture.add_pairs_from_selection([record])
    assert isinstance(result, CommitResult)
    assert len(result.admitted) == 1
    assert result.rejected == []


@pytest.mark.asyncio
async def test_add_pairs_from_selection_rejects_sub_cent(engine_fixture):
    """End-to-end: a sub-cent record flows through admission and lands in rejected."""
    from talos.game_manager import CommitResult, MarketAdmissionError

    subcent_record = {
        "event_ticker": "KXS-26JAN01",
        "ticker_a": "KXS-26JAN01-A",
        "ticker_b": "KXS-26JAN01-B",
        "side_a": "no", "side_b": "no",
        "kalshi_event_ticker": "KXS-26JAN01",
        "series_ticker": "KXS",
        "fee_type": "quadratic_with_maker_fees", "fee_rate": 0.0175,
        "close_time": "2026-12-31T00:00:00Z", "expected_expiration_time": None,
    }
    result = await engine_fixture.add_pairs_from_selection([subcent_record])
    assert isinstance(result, CommitResult)
    assert result.admitted == []
    assert len(result.rejected) == 1
    rejected_record, rejected_error = result.rejected[0]
    assert rejected_record["event_ticker"] == "KXS-26JAN01"
    assert isinstance(rejected_error, MarketAdmissionError)
    assert "sub-cent" in str(rejected_error).lower() or "tick" in str(rejected_error).lower()


# ──────────────────────────────────────────────────────────────────
# UI ingress (manual add + market picker) admission surfacing
# ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_game_manager_add_market_as_pair_raises_for_fractional():
    """GameManager.add_market_as_pair raises MarketAdmissionError when the
    market is fractional, BEFORE touching the scanner / feed / ArbPair ctor.
    This is the guard at the market-picker ingress path in game_manager.py.
    """
    from unittest.mock import AsyncMock, MagicMock

    from talos.game_manager import GameManager
    from talos.models.market import Event

    # Minimal stand-up — the guard fires before any collaborator is touched,
    # so MagicMock stubs are sufficient.
    rest = MagicMock()
    rest.get_series = AsyncMock()
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
    with pytest.raises(MarketAdmissionError) as exc_info:
        await gm.add_market_as_pair(event, fractional_market)
    assert "KXF-26JAN01-A" in str(exc_info.value)
    # Guard fires before scanner.add_pair is called.
    scanner.add_pair.assert_not_called()


@pytest.mark.asyncio
async def test_add_market_pairs_surfaces_rejection_notification(engine_fixture):
    """engine.add_market_pairs catches MarketAdmissionError per market and
    surfaces a consolidated notification listing the rejected ticker(s).
    The OK market still flows through successfully."""
    from talos.game_manager import validate_market_for_admission
    from talos.models.market import Event
    from talos.models.strategy import ArbPair

    # Wire the fixture's gm.add_market_as_pair to honour the real admission
    # guard — the fixture normally returns a MagicMock for every call.
    async def _add_market_as_pair(event, market):
        validate_market_for_admission(market, market)
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

    pairs = await engine_fixture.add_market_pairs(
        event, [fractional_market, ok_market],
    )

    # The OK market succeeded.
    assert len(pairs) == 1
    assert pairs[0].ticker_a == "KXA-26JAN01-A"

    # A rejection notification fired, mentioning the fractional ticker and
    # with severity=error.
    rejection_notifs = [
        (msg, sev) for msg, sev, _toast in captured
        if sev == "error" and "KXF-26JAN01-A" in msg
    ]
    assert rejection_notifs, (
        f"expected rejection notification mentioning KXF-26JAN01-A, "
        f"got {captured}"
    )


@pytest.mark.asyncio
async def test_add_games_surfaces_admission_rejection_as_specific_toast(
    engine_fixture,
):
    """engine.add_games catches MarketAdmissionError from the underlying
    game_manager.add_game path and surfaces a 'Market rejected (admission
    guard): ...' toast rather than the generic 'Error: ...' path."""
    async def _raise(urls):
        raise MarketAdmissionError(
            "KXF-26JAN01-A: fractional_trading_enabled markets ..."
        )

    engine_fixture._game_manager.add_games = _raise

    captured: list[tuple[str, str, bool]] = []

    def _capture_notify(msg, severity="information", *, toast=False):
        captured.append((msg, severity, toast))

    engine_fixture._notify = _capture_notify

    result = await engine_fixture.add_games(["https://kalshi.com/x"])

    assert result == []
    rejection_notifs = [
        (msg, sev) for msg, sev, _toast in captured
        if sev == "error" and "admission guard" in msg.lower()
    ]
    assert rejection_notifs, (
        f"expected 'admission guard' rejection notification, got {captured}"
    )
    # And the notification should carry the specific reason, not a bare
    # "Error: ..." prefix.
    assert any("Market rejected" in msg for msg, _sev, _toast in captured), (
        f"expected 'Market rejected' prefix, got {captured}"
    )
