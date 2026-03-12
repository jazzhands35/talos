"""Tests for WS message models — FP migration validators."""

from __future__ import annotations

from typing import Any

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
