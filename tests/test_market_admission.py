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
