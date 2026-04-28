"""Tests for the per-side max-ahead strategy helper."""

from __future__ import annotations

from talos.drip import DripConfig
from talos.position_ledger import PositionLedger, Side
from talos.strategy import per_side_max_ahead


def _ledger(unit_size: int = 5) -> PositionLedger:
    return PositionLedger(event_ticker="EVT", unit_size=unit_size)


def test_returns_drip_cap_for_drip_events() -> None:
    ledger = _ledger(unit_size=5)
    config = DripConfig(drip_size=2, max_drips=3)  # cap = 6

    assert per_side_max_ahead(ledger, Side.A, config) == 6


def test_returns_unit_room_when_drip_config_is_none() -> None:
    ledger = _ledger(unit_size=5)
    # No fills: full unit available.

    assert per_side_max_ahead(ledger, Side.A, None) == 5


def test_subtracts_filled_in_unit_for_standard_strategy() -> None:
    ledger = _ledger(unit_size=5)
    ledger.record_fill_from_ws(Side.A, trade_id="t1", count_fp100=300, price_bps=5000, fees_bps=0)

    # 3 filled in current unit, 2 ahead-room remaining
    assert per_side_max_ahead(ledger, Side.A, None) == 2


def test_full_unit_opens_next_unit_room() -> None:
    ledger = _ledger(unit_size=5)
    ledger.record_fill_from_ws(Side.A, trade_id="t1", count_fp100=500, price_bps=5000, fees_bps=0)

    # Unit is exactly complete (5 fills, unit_size=5) → next unit's worth
    # of resting room opens immediately.
    assert per_side_max_ahead(ledger, Side.A, None) == 5


def test_reflects_updated_unit_size_mid_session() -> None:
    ledger = _ledger(unit_size=5)
    assert per_side_max_ahead(ledger, Side.A, None) == 5

    ledger.unit_size = 10
    assert per_side_max_ahead(ledger, Side.A, None) == 10


def test_drip_cap_ignores_unit_size() -> None:
    ledger = _ledger(unit_size=5)
    config = DripConfig(drip_size=1, max_drips=1)  # cap = 1

    # Fill 3 contracts on side A; helper still returns drip cap, not unit room.
    ledger.record_fill_from_ws(Side.A, trade_id="t1", count_fp100=300, price_bps=5000, fees_bps=0)
    assert per_side_max_ahead(ledger, Side.A, config) == 1
