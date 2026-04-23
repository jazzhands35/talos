"""Tests for WS message models — FP migration validators."""

from __future__ import annotations

from typing import Any

import pytest

from talos.models.ws import (
    FillMessage,
    MarketLifecycleMessage,
    MarketPositionMessage,
    OrderBookDelta,
    OrderBookSnapshot,
    TickerMessage,
    TradeMessage,
    UserOrderMessage,
)


class TestOrderBookSnapshotFP:
    """OrderBookSnapshot should convert yes_dollars_fp/no_dollars_fp arrays."""

    def test_converts_fp_strings_to_cents_and_int(self) -> None:
        raw: dict[str, Any] = {
            "market_ticker": "MKT-1",
            "market_id": "uuid-1",
            "yes_dollars_fp": [["0.65", "100.00"], ["0.70", "50.00"]],
            "no_dollars_fp": [["0.35", "200.00"]],
        }
        snap = OrderBookSnapshot.model_validate(raw)
        assert snap.yes == [[65, 100], [70, 50]]
        assert snap.no == [[35, 200]]

    def test_passes_through_legacy_integer_format(self) -> None:
        raw: dict[str, Any] = {
            "market_ticker": "MKT-1",
            "market_id": "uuid-1",
            "yes": [[65, 100]],
            "no": [[35, 50]],
        }
        snap = OrderBookSnapshot.model_validate(raw)
        assert snap.yes == [[65, 100]]
        assert snap.no == [[35, 50]]


class TestOrderBookDeltaFP:
    """OrderBookDelta should convert price_dollars and delta_fp."""

    def test_converts_fp_strings(self) -> None:
        raw: dict[str, Any] = {
            "market_ticker": "MKT-1",
            "market_id": "uuid-1",
            "price_dollars": "0.65",
            "delta_fp": "150.00",
            "side": "yes",
            "ts": "2026-03-12T12:00:00Z",
        }
        delta = OrderBookDelta.model_validate(raw)
        assert delta.price == 65
        assert delta.delta == 150


class TestTickerMessageFP:
    """TickerMessage should convert _dollars fields to cents and _fp to int."""

    def test_converts_dollar_prices(self) -> None:
        raw: dict[str, Any] = {
            "market_ticker": "MKT-1",
            "yes_bid_dollars": "0.62",
            "yes_ask_dollars": "0.65",
            "no_bid_dollars": "0.35",
            "no_ask_dollars": "0.38",
            "last_price_dollars": "0.63",
        }
        ticker = TickerMessage.model_validate(raw)
        assert ticker.yes_bid == 62
        assert ticker.yes_ask == 65
        assert ticker.no_bid == 35
        assert ticker.no_ask == 38
        assert ticker.last_price == 63

    def test_converts_fp_counts(self) -> None:
        raw: dict[str, Any] = {
            "market_ticker": "MKT-1",
            "volume_fp": "5000.00",
            "open_interest_fp": "2500.00",
        }
        ticker = TickerMessage.model_validate(raw)
        assert ticker.volume == 5000
        assert ticker.open_interest == 2500

    def test_dollar_volume_and_dollar_oi(self) -> None:
        raw: dict[str, Any] = {
            "market_ticker": "MKT-1",
            "dollar_volume_dollars": "1234.56",
            "dollar_open_interest_dollars": "789.01",
        }
        ticker = TickerMessage.model_validate(raw)
        assert ticker.dollar_volume == 123456
        assert ticker.dollar_open_interest == 78901


class TestTradeMessageFP:
    """TradeMessage should convert price and count from FP fields."""

    def test_converts_yes_price_dollars(self) -> None:
        raw: dict[str, Any] = {
            "market_ticker": "MKT-1",
            "yes_price_dollars": "0.72",
            "count_fp": "25.00",
            "side": "yes",
            "ts": "2026-03-12T12:00:00Z",
            "trade_id": "trade-1",
        }
        trade = TradeMessage.model_validate(raw)
        assert trade.price == 72
        assert trade.count == 25

    def test_converts_no_price_dollars(self) -> None:
        raw: dict[str, Any] = {
            "market_ticker": "MKT-1",
            "no_price_dollars": "0.40",
            "count_fp": "10.00",
            "side": "no",
            "ts": "2026-03-12T12:00:00Z",
            "trade_id": "trade-2",
        }
        trade = TradeMessage.model_validate(raw)
        assert trade.price == 40


class TestUserOrderMessage:
    """UserOrderMessage from user_orders WS channel."""

    def test_converts_fp_fields(self) -> None:
        raw: dict[str, Any] = {
            "order_id": "order-1",
            "ticker": "MKT-1",
            "status": "resting",
            "side": "yes",
            "is_yes": True,
            "yes_price_dollars": "0.65",
            "no_price_dollars": "0.35",
            "fill_count_fp": "10.00",
            "remaining_count_fp": "5.00",
            "initial_count_fp": "15.00",
            "maker_fill_cost_dollars": "6.50",
            "taker_fill_cost_dollars": "0.00",
            "maker_fees_dollars": "0.11",
            "taker_fees_dollars": "0.00",
            "client_order_id": "client-1",
            "created_time": "2026-03-12T12:00:00Z",
            "last_update_time": "2026-03-12T12:01:00Z",
        }
        msg = UserOrderMessage.model_validate(raw)
        assert msg.order_id == "order-1"
        assert msg.ticker == "MKT-1"
        assert msg.yes_price == 65
        assert msg.no_price == 35
        assert msg.fill_count == 10
        assert msg.remaining_count == 5
        assert msg.initial_count == 15
        assert msg.maker_fill_cost == 650
        assert msg.taker_fill_cost == 0
        assert msg.maker_fees == 11
        assert msg.taker_fees == 0

    def test_defaults_with_minimal_payload(self) -> None:
        raw: dict[str, Any] = {
            "order_id": "order-2",
            "ticker": "MKT-2",
        }
        msg = UserOrderMessage.model_validate(raw)
        assert msg.status == ""
        assert msg.yes_price == 0
        assert msg.fill_count == 0

    def test_ignores_extra_fields(self) -> None:
        raw: dict[str, Any] = {
            "order_id": "order-3",
            "ticker": "MKT-3",
            "some_future_field": "value",
        }
        msg = UserOrderMessage.model_validate(raw)
        assert msg.order_id == "order-3"


class TestFillMessage:
    """FillMessage from fill WS channel."""

    def test_converts_fp_fields(self) -> None:
        raw: dict[str, Any] = {
            "trade_id": "trade-1",
            "order_id": "order-1",
            "market_ticker": "MKT-1",
            "is_taker": False,
            "side": "yes",
            "action": "buy",
            "yes_price_dollars": "0.65",
            "count_fp": "10.00",
            "post_position_fp": "25.00",
            "purchased_side": "yes",
            "ts": 1741795200,
            "client_order_id": "client-1",
        }
        msg = FillMessage.model_validate(raw)
        assert msg.yes_price == 65
        assert msg.count == 10
        assert msg.post_position == 25
        assert msg.trade_id == "trade-1"

    def test_fee_cost_string_conversion(self) -> None:
        """fee_cost can arrive as a FixedPointDollars string."""
        raw: dict[str, Any] = {
            "trade_id": "trade-2",
            "order_id": "order-2",
            "market_ticker": "MKT-2",
            "fee_cost": "0.12",
        }
        msg = FillMessage.model_validate(raw)
        assert msg.fee_cost == 12

    def test_negative_post_position(self) -> None:
        """post_position can be negative (NO contracts)."""
        raw: dict[str, Any] = {
            "trade_id": "trade-3",
            "order_id": "order-3",
            "market_ticker": "MKT-3",
            "post_position_fp": "-15.00",
        }
        msg = FillMessage.model_validate(raw)
        assert msg.post_position == -15


class TestMarketPositionMessage:
    """MarketPositionMessage from market_positions WS channel."""

    def test_converts_fp_fields(self) -> None:
        raw: dict[str, Any] = {
            "market_ticker": "MKT-1",
            "position_fp": "20.00",
            "position_cost_dollars": "13.00",
            "realized_pnl_dollars": "2.50",
            "fees_paid_dollars": "0.35",
            "volume_fp": "100.00",
        }
        msg = MarketPositionMessage.model_validate(raw)
        assert msg.position == 20
        assert msg.position_cost == 1300
        assert msg.realized_pnl == 250
        assert msg.fees_paid == 35
        assert msg.volume == 100

    def test_defaults_with_minimal_payload(self) -> None:
        raw: dict[str, Any] = {
            "market_ticker": "MKT-2",
        }
        msg = MarketPositionMessage.model_validate(raw)
        assert msg.position == 0
        assert msg.position_cost == 0


class TestMarketLifecycleMessage:
    """MarketLifecycleMessage from market_lifecycle_v2 WS channel."""

    def test_determined_event(self) -> None:
        raw: dict[str, Any] = {
            "event_type": "determined",
            "market_ticker": "MKT-1",
            "result": "yes",
            "settlement_value": "1.00",
            "determination_ts": 1741795200,
        }
        msg = MarketLifecycleMessage.model_validate(raw)
        assert msg.event_type == "determined"
        assert msg.result == "yes"
        assert msg.settlement_value == 100
        assert msg.determination_ts == 1741795200

    def test_deactivated_event(self) -> None:
        raw: dict[str, Any] = {
            "event_type": "deactivated",
            "market_ticker": "MKT-2",
            "is_deactivated": True,
        }
        msg = MarketLifecycleMessage.model_validate(raw)
        assert msg.is_deactivated is True

    def test_settlement_value_none_when_absent(self) -> None:
        raw: dict[str, Any] = {
            "event_type": "created",
            "market_ticker": "MKT-3",
        }
        msg = MarketLifecycleMessage.model_validate(raw)
        assert msg.settlement_value is None


# ─────────────────────────────────────────────────────────────────────
# Task 3b-WS: bps/fp100 dual-field coverage.
# Each message type gets a dedicated class verifying that the new
# _bps / _fp100 fields populate alongside the legacy cents / int fields
# from the same wire payload. Sub-cent / fractional precision cases
# pin the exact-precision contract; NO-side derivation pins the
# complement_bps wiring.
# See: docs/superpowers/specs/2026-04-17-bps-fp100-unit-migration-design.md
# ─────────────────────────────────────────────────────────────────────


class TestOrderBookSnapshotDualBpsFp100:
    """OrderBookSnapshot: yes_bps_fp100 / no_bps_fp100 populate alongside
    legacy yes / no when wire arrives as yes_dollars_fp / no_dollars_fp.
    """

    def test_dual_population_whole_cents(self) -> None:
        raw: dict[str, Any] = {
            "market_ticker": "MKT-1",
            "market_id": "uuid-1",
            "yes_dollars_fp": [["0.65", "100.00"], ["0.70", "50.00"]],
            "no_dollars_fp": [["0.35", "200.00"]],
        }
        snap = OrderBookSnapshot.model_validate(raw)
        assert snap.yes == [[65, 100], [70, 50]]
        assert snap.no == [[35, 200]]
        # New bps / fp100 pairs — exact precision.
        assert snap.yes_bps_fp100 == [[6500, 10000], [7000, 5000]]
        assert snap.no_bps_fp100 == [[3500, 20000]]

    def test_subcent_and_fractional_retained(self) -> None:
        """Wire ['0.0488', '1.89'] → legacy [5, 1] AND new [488, 189]."""
        raw: dict[str, Any] = {
            "market_ticker": "MARJ-MKT",
            "market_id": "uuid-marj",
            "yes_dollars_fp": [["0.0488", "1.89"]],
            "no_dollars_fp": [["0.9512", "10.00"]],
        }
        snap = OrderBookSnapshot.model_validate(raw)
        # Legacy path: banker's round + floor.
        assert snap.yes == [[5, 1]]
        assert snap.no == [[95, 10]]
        # New path: exact.
        assert snap.yes_bps_fp100 == [[488, 189]]
        assert snap.no_bps_fp100 == [[9512, 1000]]

    def test_empty_arrays_stay_empty_not_none(self) -> None:
        """No wire arrays → both legacy and new lists default to []."""
        raw: dict[str, Any] = {
            "market_ticker": "MKT-EMPTY",
            "market_id": "uuid-empty",
        }
        snap = OrderBookSnapshot.model_validate(raw)
        assert snap.yes == []
        assert snap.no == []
        assert snap.yes_bps_fp100 == []
        assert snap.no_bps_fp100 == []


class TestOrderBookDeltaDualBpsFp100:
    """OrderBookDelta: price_bps / delta_fp100 siblings."""

    def test_dual_population_whole_cents(self) -> None:
        raw: dict[str, Any] = {
            "market_ticker": "MKT-1",
            "market_id": "uuid-1",
            "price_dollars": "0.65",
            "delta_fp": "150.00",
            "side": "yes",
            "ts": "2026-03-12T12:00:00Z",
        }
        d = OrderBookDelta.model_validate(raw)
        assert d.price == 65 and d.price_bps == 6500
        assert d.delta == 150 and d.delta_fp100 == 15000

    def test_subcent_price_and_fractional_delta(self) -> None:
        raw: dict[str, Any] = {
            "market_ticker": "MARJ-MKT",
            "market_id": "uuid-marj",
            "price_dollars": "0.0488",
            "delta_fp": "1.89",
            "side": "yes",
            "ts": "2026-03-12T12:00:00Z",
        }
        d = OrderBookDelta.model_validate(raw)
        assert d.price == 5 and d.price_bps == 488
        assert d.delta == 1 and d.delta_fp100 == 189

    def test_negative_delta_preserved_in_fp100(self) -> None:
        """Sign preserved on both legacy and fp100 paths."""
        raw: dict[str, Any] = {
            "market_ticker": "MKT-1",
            "market_id": "uuid-1",
            "price_dollars": "0.50",
            "delta_fp": "-20.00",
            "side": "no",
            "ts": "2026-03-12T12:00:00Z",
        }
        d = OrderBookDelta.model_validate(raw)
        assert d.delta == -20 and d.delta_fp100 == -2000


class TestTickerMessageDualBpsFp100:
    """TickerMessage: every price / volume / oi / dollar_* field gets a
    _bps or _fp100 sibling. NO-side derived via complement_bps.
    """

    def test_dual_population_whole_cents(self) -> None:
        raw: dict[str, Any] = {
            "market_ticker": "MKT-1",
            "yes_bid_dollars": "0.62",
            "yes_ask_dollars": "0.65",
            "no_bid_dollars": "0.35",
            "no_ask_dollars": "0.38",
            "last_price_dollars": "0.63",
            "volume_fp": "5000.00",
            "open_interest_fp": "2500.00",
            "dollar_volume_dollars": "1234.56",
            "dollar_open_interest_dollars": "789.01",
        }
        t = TickerMessage.model_validate(raw)
        # Legacy cents / ints.
        assert t.yes_bid == 62 and t.yes_ask == 65
        assert t.no_bid == 35 and t.no_ask == 38
        assert t.last_price == 63
        assert t.volume == 5000 and t.open_interest == 2500
        assert t.dollar_volume == 123456
        assert t.dollar_open_interest == 78901
        # New bps / fp100.
        assert t.yes_bid_bps == 6200 and t.yes_ask_bps == 6500
        assert t.no_bid_bps == 3500 and t.no_ask_bps == 3800
        assert t.last_price_bps == 6300
        assert t.volume_fp100 == 500000
        assert t.open_interest_fp100 == 250000
        assert t.dollar_volume_bps == 12345600
        assert t.dollar_open_interest_bps == 7890100

    def test_subcent_price_retained_in_bps(self) -> None:
        raw: dict[str, Any] = {
            "market_ticker": "MARJ-MKT",
            "yes_bid_dollars": "0.0488",
            "yes_ask_dollars": "0.0500",
        }
        t = TickerMessage.model_validate(raw)
        assert t.yes_bid == 5 and t.yes_bid_bps == 488
        assert t.yes_ask == 5 and t.yes_ask_bps == 500

    def test_fractional_volume_retained_in_fp100(self) -> None:
        raw: dict[str, Any] = {
            "market_ticker": "MARJ-MKT",
            "volume_fp": "1.89",
            "open_interest_fp": "10.50",
        }
        t = TickerMessage.model_validate(raw)
        assert t.volume == 1 and t.volume_fp100 == 189
        assert t.open_interest == 10 and t.open_interest_fp100 == 1050

    def test_no_side_derivation_uses_complement_bps(self) -> None:
        """WS only sends YES side — NO side derived. For yes at 5300 bps,
        NO = 1 - 0.53 = 0.47 = 4700 bps (complement_bps(5300))."""
        raw: dict[str, Any] = {
            "market_ticker": "MKT-1",
            "yes_bid_dollars": "0.53",
            "yes_ask_dollars": "0.53",
        }
        t = TickerMessage.model_validate(raw)
        assert t.no_bid == 47 and t.no_bid_bps == 4700
        assert t.no_ask == 47 and t.no_ask_bps == 4700

    def test_last_price_from_price_dollars(self) -> None:
        """WS uses price_dollars (not last_price_dollars) for last trade."""
        raw: dict[str, Any] = {
            "market_ticker": "MKT-1",
            "price_dollars": "0.66",
        }
        t = TickerMessage.model_validate(raw)
        assert t.last_price == 66
        assert t.last_price_bps == 6600

    def test_last_price_from_last_price_dollars(self) -> None:
        """REST variant uses last_price_dollars."""
        raw: dict[str, Any] = {
            "market_ticker": "MKT-1",
            "last_price_dollars": "0.66",
        }
        t = TickerMessage.model_validate(raw)
        assert t.last_price == 66
        assert t.last_price_bps == 6600

    def test_last_price_dollars_wins_when_both_present(self) -> None:
        """Matches existing ordering: last_price_dollars is the second
        iteration entry and overwrites the price_dollars result."""
        raw: dict[str, Any] = {
            "market_ticker": "MKT-1",
            "price_dollars": "0.10",
            "last_price_dollars": "0.66",
        }
        t = TickerMessage.model_validate(raw)
        assert t.last_price == 66
        assert t.last_price_bps == 6600


class TestTradeMessageDualBpsFp100:
    """TradeMessage: price_bps / count_fp100 populate from wire."""

    def test_dual_population_yes_price(self) -> None:
        raw: dict[str, Any] = {
            "market_ticker": "MKT-1",
            "yes_price_dollars": "0.72",
            "count_fp": "25.00",
            "side": "yes",
            "ts": "2026-03-12T12:00:00Z",
            "trade_id": "trade-1",
        }
        t = TradeMessage.model_validate(raw)
        assert t.price == 72 and t.price_bps == 7200
        assert t.count == 25 and t.count_fp100 == 2500

    def test_dual_population_no_price(self) -> None:
        raw: dict[str, Any] = {
            "market_ticker": "MKT-1",
            "no_price_dollars": "0.40",
            "count_fp": "10.00",
            "side": "no",
            "ts": "2026-03-12T12:00:00Z",
            "trade_id": "trade-2",
        }
        t = TradeMessage.model_validate(raw)
        assert t.price == 40 and t.price_bps == 4000
        assert t.count == 10 and t.count_fp100 == 1000

    def test_subcent_price_and_fractional_count(self) -> None:
        raw: dict[str, Any] = {
            "market_ticker": "MARJ-MKT",
            "yes_price_dollars": "0.0488",
            "count_fp": "1.89",
            "side": "yes",
            "ts": "2026-03-12T12:00:00Z",
            "trade_id": "trade-marj",
        }
        t = TradeMessage.model_validate(raw)
        assert t.price == 5 and t.price_bps == 488
        assert t.count == 1 and t.count_fp100 == 189


class TestUserOrderMessageDualBpsFp100:
    """UserOrderMessage: 6 money + 3 count siblings, NO derived via
    complement_bps."""

    def test_dual_population_all_fields(self) -> None:
        raw: dict[str, Any] = {
            "order_id": "order-1",
            "ticker": "MKT-1",
            "yes_price_dollars": "0.65",
            "no_price_dollars": "0.35",
            "fill_count_fp": "10.00",
            "remaining_count_fp": "5.00",
            "initial_count_fp": "15.00",
            "maker_fill_cost_dollars": "6.50",
            "taker_fill_cost_dollars": "0.00",
            "maker_fees_dollars": "0.11",
            "taker_fees_dollars": "0.02",
        }
        m = UserOrderMessage.model_validate(raw)
        # Legacy.
        assert m.yes_price == 65 and m.no_price == 35
        assert m.fill_count == 10 and m.remaining_count == 5
        assert m.initial_count == 15
        assert m.maker_fill_cost == 650 and m.taker_fill_cost == 0
        assert m.maker_fees == 11 and m.taker_fees == 2
        # New.
        assert m.yes_price_bps == 6500 and m.no_price_bps == 3500
        assert m.fill_count_fp100 == 1000
        assert m.remaining_count_fp100 == 500
        assert m.initial_count_fp100 == 1500
        assert m.maker_fill_cost_bps == 65000
        assert m.taker_fill_cost_bps == 0
        assert m.maker_fees_bps == 1100
        assert m.taker_fees_bps == 200

    def test_subcent_price_retained_in_bps(self) -> None:
        raw: dict[str, Any] = {
            "order_id": "order-marj",
            "ticker": "MARJ-MKT",
            "yes_price_dollars": "0.0488",
        }
        m = UserOrderMessage.model_validate(raw)
        assert m.yes_price == 5 and m.yes_price_bps == 488

    def test_fractional_count_retained_in_fp100(self) -> None:
        raw: dict[str, Any] = {
            "order_id": "order-frac",
            "ticker": "MARJ-MKT",
            "fill_count_fp": "1.89",
            "remaining_count_fp": "8.11",
            "initial_count_fp": "10.00",
        }
        m = UserOrderMessage.model_validate(raw)
        assert m.fill_count == 1 and m.fill_count_fp100 == 189
        assert m.remaining_count == 8 and m.remaining_count_fp100 == 811
        assert m.initial_count == 10 and m.initial_count_fp100 == 1000

    def test_no_side_derivation_uses_complement_bps(self) -> None:
        """yes_price_dollars='0.53' (no explicit no_price) → no_price == 47
        AND no_price_bps == complement_bps(5300) == 4700."""
        raw: dict[str, Any] = {
            "order_id": "order-derive",
            "ticker": "MKT-1",
            "yes_price_dollars": "0.53",
        }
        m = UserOrderMessage.model_validate(raw)
        assert m.yes_price == 53 and m.yes_price_bps == 5300
        assert m.no_price == 47 and m.no_price_bps == 4700

    def test_zero_defaults_when_wire_field_absent(self) -> None:
        raw: dict[str, Any] = {
            "order_id": "order-empty",
            "ticker": "MKT-1",
        }
        m = UserOrderMessage.model_validate(raw)
        assert m.yes_price == 0 and m.yes_price_bps == 0
        assert m.no_price == 0 and m.no_price_bps == 0
        assert m.fill_count == 0 and m.fill_count_fp100 == 0
        assert m.remaining_count == 0 and m.remaining_count_fp100 == 0
        assert m.initial_count == 0 and m.initial_count_fp100 == 0
        assert m.maker_fill_cost == 0 and m.maker_fill_cost_bps == 0
        assert m.taker_fill_cost == 0 and m.taker_fill_cost_bps == 0
        assert m.maker_fees == 0 and m.maker_fees_bps == 0
        assert m.taker_fees == 0 and m.taker_fees_bps == 0


class TestFillMessageDualBpsFp100:
    """FillMessage: 2 price + 1 count + 1 fee + 1 position siblings."""

    def test_dual_population_all_fields(self) -> None:
        raw: dict[str, Any] = {
            "trade_id": "trade-1",
            "order_id": "order-1",
            "market_ticker": "MKT-1",
            "yes_price_dollars": "0.65",
            "count_fp": "10.00",
            "post_position_fp": "25.00",
            "fee_cost": "0.12",
        }
        m = FillMessage.model_validate(raw)
        # Legacy.
        assert m.yes_price == 65
        assert m.count == 10
        assert m.post_position == 25
        assert m.fee_cost == 12
        # Derived NO-side (yes=65¢ → no=35¢; yes=6500bps → no=3500bps).
        assert m.no_price == 35
        # New bps / fp100.
        assert m.yes_price_bps == 6500
        assert m.no_price_bps == 3500  # complement_bps(6500) == 3500
        assert m.count_fp100 == 1000
        assert m.post_position_fp100 == 2500
        assert m.fee_cost_bps == 1200

    def test_no_side_derivation_uses_complement_bps(self) -> None:
        """yes_price_dollars='0.53' → no_price == 47 AND no_price_bps ==
        complement_bps(5300) == 4700."""
        raw: dict[str, Any] = {
            "trade_id": "trade-derive",
            "order_id": "order-1",
            "market_ticker": "MKT-1",
            "yes_price_dollars": "0.53",
        }
        m = FillMessage.model_validate(raw)
        assert m.yes_price == 53 and m.yes_price_bps == 5300
        assert m.no_price == 47 and m.no_price_bps == 4700

    def test_subcent_price_retained_in_bps(self) -> None:
        raw: dict[str, Any] = {
            "trade_id": "trade-marj",
            "order_id": "order-1",
            "market_ticker": "MARJ-MKT",
            "yes_price_dollars": "0.0488",
        }
        m = FillMessage.model_validate(raw)
        assert m.yes_price == 5 and m.yes_price_bps == 488

    def test_fractional_count_and_position_retained_in_fp100(self) -> None:
        """Wire '-15.50' → post_position_fp100 == -1550 (exact) but
        legacy post_position == -16 (Python floor-div on negative
        rounds toward -inf — the silent-truncation bug this migration
        exists to eliminate)."""
        raw: dict[str, Any] = {
            "trade_id": "trade-frac",
            "order_id": "order-1",
            "market_ticker": "MARJ-MKT",
            "count_fp": "1.89",
            "post_position_fp": "-15.50",
        }
        m = FillMessage.model_validate(raw)
        assert m.count == 1 and m.count_fp100 == 189
        # Legacy floor-div: -1550 // 100 == -16 (toward -inf).
        assert m.post_position == -16
        # Exact sign + magnitude preserved in fp100.
        assert m.post_position_fp100 == -1550

    def test_fee_cost_string_populates_bps(self) -> None:
        raw: dict[str, Any] = {
            "trade_id": "trade-fee",
            "order_id": "order-1",
            "market_ticker": "MKT-1",
            "fee_cost": "0.0130",
        }
        m = FillMessage.model_validate(raw)
        assert m.fee_cost == 1
        assert m.fee_cost_bps == 130

    def test_fee_cost_integer_passthrough_leaves_bps_default(self) -> None:
        raw: dict[str, Any] = {
            "trade_id": "trade-fee-int",
            "order_id": "order-1",
            "market_ticker": "MKT-1",
            "fee_cost": 5,
        }
        m = FillMessage.model_validate(raw)
        assert m.fee_cost == 5
        assert m.fee_cost_bps == 0


class TestMarketPositionMessageDualBpsFp100:
    """MarketPositionMessage: 1 count + 3 money + 1 volume siblings."""

    def test_dual_population_all_fields(self) -> None:
        raw: dict[str, Any] = {
            "market_ticker": "MKT-1",
            "position_fp": "20.00",
            "position_cost_dollars": "13.00",
            "realized_pnl_dollars": "2.50",
            "fees_paid_dollars": "0.35",
            "volume_fp": "100.00",
        }
        m = MarketPositionMessage.model_validate(raw)
        # Legacy.
        assert m.position == 20
        assert m.position_cost == 1300
        assert m.realized_pnl == 250
        assert m.fees_paid == 35
        assert m.volume == 100
        # New.
        assert m.position_fp100 == 2000
        assert m.position_cost_bps == 130000
        assert m.realized_pnl_bps == 25000
        assert m.fees_paid_bps == 3500
        assert m.volume_fp100 == 10000

    def test_subcent_fee_retained_in_bps(self) -> None:
        raw: dict[str, Any] = {
            "market_ticker": "MARJ-MKT",
            "fees_paid_dollars": "0.0488",
        }
        m = MarketPositionMessage.model_validate(raw)
        assert m.fees_paid == 5 and m.fees_paid_bps == 488

    def test_fractional_position_retained_in_fp100(self) -> None:
        raw: dict[str, Any] = {
            "market_ticker": "MARJ-MKT",
            "position_fp": "1.89",
            "volume_fp": "10.50",
        }
        m = MarketPositionMessage.model_validate(raw)
        assert m.position == 1 and m.position_fp100 == 189
        assert m.volume == 10 and m.volume_fp100 == 1050


class TestMarketLifecycleMessageDualBpsFp100:
    """MarketLifecycleMessage: settlement_value_bps sibling."""

    def test_dual_population_settlement_value(self) -> None:
        raw: dict[str, Any] = {
            "event_type": "determined",
            "market_ticker": "MKT-1",
            "result": "yes",
            "settlement_value": "1.00",
        }
        m = MarketLifecycleMessage.model_validate(raw)
        assert m.settlement_value == 100
        assert m.settlement_value_bps == 10000

    def test_subcent_settlement_retained_in_bps(self) -> None:
        raw: dict[str, Any] = {
            "event_type": "determined",
            "market_ticker": "MARJ-MKT",
            "settlement_value": "0.0488",
        }
        m = MarketLifecycleMessage.model_validate(raw)
        assert m.settlement_value == 5
        assert m.settlement_value_bps == 488

    def test_settlement_value_bps_none_when_absent(self) -> None:
        raw: dict[str, Any] = {
            "event_type": "created",
            "market_ticker": "MKT-3",
        }
        m = MarketLifecycleMessage.model_validate(raw)
        assert m.settlement_value is None
        assert m.settlement_value_bps is None


class TestWSDualFieldInvariants:
    """Parallel-field contract across every migrated WS message type:
    for any whole-cent / whole-contract wire value the _bps field
    equals cents × 100 and the _fp100 field equals contracts × 100
    exactly. Parametrized values are checked against both attrs
    (rather than attr × 100 vs attr) to keep Pyright happy on the
    ``int | None`` fields.
    """

    @pytest.mark.parametrize(
        "wire_dollars,cents,bps",
        [("0.01", 1, 100), ("0.53", 53, 5_300), ("0.99", 99, 9_900)],
    )
    def test_orderbook_delta(
        self, wire_dollars: str, cents: int, bps: int
    ) -> None:
        d = OrderBookDelta.model_validate({
            "market_ticker": "MKT",
            "market_id": "uuid",
            "price_dollars": wire_dollars,
            "delta_fp": "1.00",
            "side": "yes",
            "ts": "2026-03-12T12:00:00Z",
        })
        assert d.price == cents
        assert d.price_bps == bps
        assert bps == cents * 100

    @pytest.mark.parametrize(
        "wire_dollars,cents,bps",
        [("0.01", 1, 100), ("0.53", 53, 5_300), ("0.99", 99, 9_900)],
    )
    def test_ticker(self, wire_dollars: str, cents: int, bps: int) -> None:
        t = TickerMessage.model_validate({
            "market_ticker": "MKT",
            "yes_bid_dollars": wire_dollars,
        })
        assert t.yes_bid == cents
        assert t.yes_bid_bps == bps
        assert bps == cents * 100

    @pytest.mark.parametrize(
        "wire_dollars,cents,bps",
        [("0.01", 1, 100), ("0.53", 53, 5_300), ("0.99", 99, 9_900)],
    )
    def test_trade(self, wire_dollars: str, cents: int, bps: int) -> None:
        t = TradeMessage.model_validate({
            "market_ticker": "MKT",
            "yes_price_dollars": wire_dollars,
            "count_fp": "1.00",
            "side": "yes",
            "ts": "2026-03-12T12:00:00Z",
            "trade_id": "x",
        })
        assert t.price == cents
        assert t.price_bps == bps
        assert bps == cents * 100

    @pytest.mark.parametrize(
        "wire_dollars,cents,bps",
        [("0.01", 1, 100), ("0.53", 53, 5_300), ("0.99", 99, 9_900)],
    )
    def test_user_order(
        self, wire_dollars: str, cents: int, bps: int
    ) -> None:
        m = UserOrderMessage.model_validate({
            "order_id": "o",
            "ticker": "t",
            "yes_price_dollars": wire_dollars,
        })
        assert m.yes_price == cents
        assert m.yes_price_bps == bps
        assert bps == cents * 100

    @pytest.mark.parametrize(
        "wire_dollars,cents,bps",
        [("0.01", 1, 100), ("0.53", 53, 5_300), ("0.99", 99, 9_900)],
    )
    def test_fill(self, wire_dollars: str, cents: int, bps: int) -> None:
        m = FillMessage.model_validate({
            "trade_id": "t",
            "order_id": "o",
            "market_ticker": "m",
            "yes_price_dollars": wire_dollars,
        })
        assert m.yes_price == cents
        assert m.yes_price_bps == bps
        assert bps == cents * 100

    @pytest.mark.parametrize(
        "wire_dollars,cents,bps",
        [("0.01", 1, 100), ("0.53", 53, 5_300), ("0.99", 99, 9_900)],
    )
    def test_market_position(
        self, wire_dollars: str, cents: int, bps: int
    ) -> None:
        m = MarketPositionMessage.model_validate({
            "market_ticker": "MKT",
            "position_cost_dollars": wire_dollars,
        })
        assert m.position_cost == cents
        assert m.position_cost_bps == bps
        assert bps == cents * 100

    @pytest.mark.parametrize(
        "wire_dollars,cents,bps",
        [("0.01", 1, 100), ("0.53", 53, 5_300), ("0.99", 99, 9_900)],
    )
    def test_market_lifecycle(
        self, wire_dollars: str, cents: int, bps: int
    ) -> None:
        m = MarketLifecycleMessage.model_validate({
            "event_type": "determined",
            "market_ticker": "MKT",
            "settlement_value": wire_dollars,
        })
        assert m.settlement_value == cents
        assert m.settlement_value_bps == bps
        assert bps == cents * 100

    @pytest.mark.parametrize(
        "wire_dollars,cents,bps",
        [("0.01", 1, 100), ("0.53", 53, 5_300), ("0.99", 99, 9_900)],
    )
    def test_orderbook_snapshot(
        self, wire_dollars: str, cents: int, bps: int
    ) -> None:
        snap = OrderBookSnapshot.model_validate({
            "market_ticker": "MKT",
            "market_id": "uuid",
            "yes_dollars_fp": [[wire_dollars, "1.00"]],
        })
        assert snap.yes == [[cents, 1]]
        assert snap.yes_bps_fp100 == [[bps, 100]]
        assert bps == cents * 100
