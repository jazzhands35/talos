"""Tests for Market shape-metadata fields and tick_bps() helper (Phase 0)."""

from __future__ import annotations

from talos.models.market import Market


def _make(**overrides) -> Market:
    """Construct a minimal valid Market for testing. Fill required base fields."""
    base = {
        "ticker": "KXTEST-26JAN01-A",
        "event_ticker": "KXTEST-26JAN01",
        "title": "Test market",
        "status": "open",
    }
    base.update(overrides)
    return Market(**base)


def test_defaults_are_cent_only_non_fractional():
    m = _make()
    assert m.fractional_trading_enabled is False
    assert m.price_level_structure == ""
    assert m.price_ranges == []
    assert m.tick_bps() == 100  # 1 cent = 100 bps


def test_fractional_trading_flag_parses_from_payload():
    m = Market.model_validate({
        "ticker": "KXFRAC-26JAN01-A",
        "event_ticker": "KXFRAC-26JAN01",
        "title": "Fractional",
        "status": "open",
        "fractional_trading_enabled": True,
    })
    assert m.fractional_trading_enabled is True


def test_price_level_structure_parses_from_payload():
    m = Market.model_validate({
        "ticker": "KXTICK-26JAN01-A",
        "event_ticker": "KXTICK-26JAN01",
        "title": "Sub-cent",
        "status": "open",
        "price_level_structure": "subpenny_0_001",
    })
    assert m.price_level_structure == "subpenny_0_001"


def test_tick_bps_from_structured_price_ranges_sub_cent():
    """A market with an explicit 0.001 dollar tick returns 10 bps (= 0.1¢)."""
    m = Market.model_validate({
        "ticker": "KXTICK-26JAN01-A",
        "event_ticker": "KXTICK-26JAN01",
        "title": "Sub-cent",
        "status": "open",
        "price_ranges": [{"min_price_dollars": "0.01", "max_price_dollars": "0.99", "tick_dollars": "0.001"}],
    })
    assert m.tick_bps() == 10


def test_tick_bps_from_structured_price_ranges_whole_cent():
    """A market with an explicit 0.01 dollar tick returns 100 bps (= 1¢)."""
    m = Market.model_validate({
        "ticker": "KXTICK-26JAN01-A",
        "event_ticker": "KXTICK-26JAN01",
        "title": "Cent tick",
        "status": "open",
        "price_ranges": [{"min_price_dollars": "0.01", "max_price_dollars": "0.99", "tick_dollars": "0.01"}],
    })
    assert m.tick_bps() == 100


def test_tick_bps_min_across_multiple_ranges():
    """When a market defines multiple price_ranges, tick_bps returns the minimum."""
    m = Market.model_validate({
        "ticker": "KXMULTI-26JAN01-A",
        "event_ticker": "KXMULTI-26JAN01",
        "title": "Multi-range",
        "status": "open",
        "price_ranges": [
            {"min_price_dollars": "0.01", "max_price_dollars": "0.10", "tick_dollars": "0.001"},
            {"min_price_dollars": "0.10", "max_price_dollars": "0.99", "tick_dollars": "0.01"},
        ],
    })
    assert m.tick_bps() == 10
