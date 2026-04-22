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
