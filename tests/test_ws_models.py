"""Tests for WS message models — post-13a-2d bps/fp100 only."""

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


class TestOrderBookSnapshot:
    """OrderBookSnapshot should convert yes_dollars_fp/no_dollars_fp arrays."""

    def test_converts_fp_strings_to_bps_fp100(self) -> None:
        raw: dict[str, Any] = {
            "market_ticker": "MKT-1",
            "market_id": "uuid-1",
            "yes_dollars_fp": [["0.65", "100.00"], ["0.70", "50.00"]],
            "no_dollars_fp": [["0.35", "200.00"]],
        }
        snap = OrderBookSnapshot.model_validate(raw)
        assert snap.yes_bps_fp100 == [[6500, 10_000], [7000, 5000]]
        assert snap.no_bps_fp100 == [[3500, 20_000]]

    def test_integer_wire_promotes_to_bps_fp100(self) -> None:
        raw: dict[str, Any] = {
            "market_ticker": "MKT-1",
            "market_id": "uuid-1",
            "yes": [[65, 100]],
            "no": [[35, 50]],
        }
        snap = OrderBookSnapshot.model_validate(raw)
        assert snap.yes_bps_fp100 == [[6500, 10_000]]
        assert snap.no_bps_fp100 == [[3500, 5000]]


class TestOrderBookDelta:
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
        assert delta.price_bps == 6500
        assert delta.delta_fp100 == 15_000

    def test_integer_wire_promotes_to_bps_fp100(self) -> None:
        raw: dict[str, Any] = {
            "market_ticker": "MKT-1",
            "market_id": "uuid-1",
            "price": 65,
            "delta": 150,
            "side": "yes",
            "ts": "2026-03-12T12:00:00Z",
        }
        delta = OrderBookDelta.model_validate(raw)
        assert delta.price_bps == 6500
        assert delta.delta_fp100 == 15_000


class TestTickerMessage:
    """TickerMessage converts _dollars/_fp fields to bps/fp100."""

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
        assert ticker.yes_bid_bps == 6200
        assert ticker.yes_ask_bps == 6500
        # When both yes_bid and no_ask from wire, no derivation happens.
        # Here no_ask wire explicitly given, so keep it.
        assert ticker.no_bid_bps == 3500
        assert ticker.no_ask_bps == 3800
        assert ticker.last_price_bps == 6300

    def test_converts_fp_counts(self) -> None:
        raw: dict[str, Any] = {
            "market_ticker": "MKT-1",
            "volume_fp": "5000.00",
            "open_interest_fp": "2500.00",
        }
        ticker = TickerMessage.model_validate(raw)
        assert ticker.volume_fp100 == 500_000
        assert ticker.open_interest_fp100 == 250_000

    def test_dollar_volume_and_dollar_oi(self) -> None:
        raw: dict[str, Any] = {
            "market_ticker": "MKT-1",
            "dollar_volume_dollars": "1234.56",
            "dollar_open_interest_dollars": "789.01",
        }
        ticker = TickerMessage.model_validate(raw)
        assert ticker.dollar_volume_bps == 12_345_600
        assert ticker.dollar_open_interest_bps == 7_890_100

    def test_no_side_derived_from_yes_bid_via_complement(self) -> None:
        raw: dict[str, Any] = {
            "market_ticker": "MKT-1",
            "yes_ask_dollars": "0.65",
            "yes_bid_dollars": "0.60",
        }
        ticker = TickerMessage.model_validate(raw)
        # no_bid = complement_bps(yes_ask_bps) = 10000 - 6500 = 3500
        assert ticker.no_bid_bps == 3500
        assert ticker.no_ask_bps == 4000


class TestTradeMessage:
    def test_converts_dollar_price_and_fp_count(self) -> None:
        raw: dict[str, Any] = {
            "market_ticker": "MKT-1",
            "yes_price_dollars": "0.65",
            "count_fp": "5.00",
            "side": "yes",
            "ts": "2026-03-12T12:00:00Z",
            "trade_id": "t1",
        }
        trade = TradeMessage.model_validate(raw)
        assert trade.price_bps == 6500
        assert trade.count_fp100 == 500
        assert trade.side == "yes"

    def test_from_no_price_dollars(self) -> None:
        raw: dict[str, Any] = {
            "market_ticker": "MKT-1",
            "no_price_dollars": "0.35",
            "count_fp": "10.00",
            "side": "no",
            "ts": "2026-03-12T12:00:00Z",
            "trade_id": "t2",
        }
        trade = TradeMessage.model_validate(raw)
        assert trade.price_bps == 3500
        assert trade.count_fp100 == 1000


class TestUserOrderMessage:
    def test_converts_all_dollar_fields(self) -> None:
        raw: dict[str, Any] = {
            "order_id": "o1",
            "ticker": "MKT-1",
            "yes_price_dollars": "0.60",
            "no_price_dollars": "0.40",
            "fill_count_fp": "5",
            "remaining_count_fp": "5",
            "initial_count_fp": "10",
            "maker_fill_cost_dollars": "3.00",
            "taker_fill_cost_dollars": "0.50",
            "maker_fees_dollars": "0.05",
            "taker_fees_dollars": "0.01",
            "side": "no",
            "status": "resting",
            "is_yes": False,
        }
        msg = UserOrderMessage.model_validate(raw)
        assert msg.yes_price_bps == 6000
        assert msg.no_price_bps == 4000
        assert msg.fill_count_fp100 == 500
        assert msg.remaining_count_fp100 == 500
        assert msg.initial_count_fp100 == 1000
        assert msg.maker_fill_cost_bps == 30_000
        assert msg.taker_fill_cost_bps == 5000
        assert msg.maker_fees_bps == 500
        assert msg.taker_fees_bps == 100

    def test_no_price_derived_from_yes_price_complement(self) -> None:
        """WS only sends yes_price — derive no_price via complement."""
        raw: dict[str, Any] = {
            "order_id": "o1",
            "ticker": "MKT-1",
            "yes_price_dollars": "0.60",
            "side": "yes",
            "status": "resting",
        }
        msg = UserOrderMessage.model_validate(raw)
        assert msg.yes_price_bps == 6000
        # no_price_bps = complement_bps(6000) = 4000
        assert msg.no_price_bps == 4000


class TestFillMessage:
    def test_converts_all_fields(self) -> None:
        raw: dict[str, Any] = {
            "trade_id": "t1",
            "order_id": "o1",
            "market_ticker": "MKT-1",
            "yes_price_dollars": "0.60",
            "count_fp": "5",
            "fee_cost": "0.02",
            "post_position_fp": "5",
            "side": "yes",
        }
        msg = FillMessage.model_validate(raw)
        assert msg.yes_price_bps == 6000
        assert msg.no_price_bps == 4000  # complement_bps(6000)
        assert msg.count_fp100 == 500
        assert msg.fee_cost_bps == 200
        assert msg.post_position_fp100 == 500

    def test_fee_cost_integer_passthrough_promotes(self) -> None:
        """Integer fee_cost (legacy) promotes to bps via ×100."""
        raw: dict[str, Any] = {
            "trade_id": "t2",
            "order_id": "o2",
            "market_ticker": "MKT-1",
            "yes_price_dollars": "0.60",
            "count_fp": "1",
            "fee_cost": 5,  # int cents, pre-migration
            "post_position_fp": "1",
            "side": "yes",
        }
        msg = FillMessage.model_validate(raw)
        assert msg.fee_cost_bps == 500


class TestMarketPositionMessage:
    def test_converts_all_fields(self) -> None:
        raw: dict[str, Any] = {
            "market_ticker": "MKT-1",
            "position_fp": "-5",
            "position_cost_dollars": "2.50",
            "realized_pnl_dollars": "-0.10",
            "fees_paid_dollars": "0.05",
            "volume_fp": "100",
        }
        msg = MarketPositionMessage.model_validate(raw)
        assert msg.position_fp100 == -500
        assert msg.position_cost_bps == 25_000
        assert msg.realized_pnl_bps == -1000
        assert msg.fees_paid_bps == 500
        assert msg.volume_fp100 == 10_000


class TestMarketLifecycleMessage:
    def test_determined_event(self) -> None:
        raw: dict[str, Any] = {
            "event_type": "determined",
            "market_ticker": "MKT-1",
            "result": "yes",
            "settlement_value": "1.00",
            "determination_ts": 1234567890,
        }
        msg = MarketLifecycleMessage.model_validate(raw)
        assert msg.event_type == "determined"
        assert msg.result == "yes"
        assert msg.settlement_value_bps == 10_000

    def test_settlement_value_bps_none_when_absent(self) -> None:
        raw: dict[str, Any] = {
            "event_type": "settled",
            "market_ticker": "MKT-1",
            "settled_ts": 1234567890,
        }
        msg = MarketLifecycleMessage.model_validate(raw)
        assert msg.settlement_value_bps is None

    def test_deactivated_event(self) -> None:
        raw: dict[str, Any] = {
            "event_type": "deactivated",
            "market_ticker": "MKT-1",
            "is_deactivated": True,
        }
        msg = MarketLifecycleMessage.model_validate(raw)
        assert msg.is_deactivated is True

    def test_integer_settlement_value_promotes(self) -> None:
        raw: dict[str, Any] = {
            "event_type": "determined",
            "market_ticker": "MKT-1",
            "result": "yes",
            "settlement_value": 100,  # int cents
        }
        msg = MarketLifecycleMessage.model_validate(raw)
        assert msg.settlement_value_bps == 10_000


class TestBpsFp100SubCentExactness:
    """Sub-cent wire values retained exactly in bps/fp100."""

    def test_orderbook_snapshot_subcent(self) -> None:
        raw: dict[str, Any] = {
            "market_ticker": "MARJ-MKT",
            "market_id": "u1",
            "yes_dollars_fp": [["0.0488", "1.89"]],
            "no_dollars_fp": [["0.9512", "10.00"]],
        }
        snap = OrderBookSnapshot.model_validate(raw)
        assert snap.yes_bps_fp100 == [[488, 189]]
        assert snap.no_bps_fp100 == [[9512, 1000]]

    def test_orderbook_delta_subcent(self) -> None:
        raw: dict[str, Any] = {
            "market_ticker": "MARJ-MKT",
            "market_id": "u1",
            "price_dollars": "0.0488",
            "delta_fp": "1.89",
            "side": "yes",
            "ts": "2026-03-12T12:00:00Z",
        }
        delta = OrderBookDelta.model_validate(raw)
        assert delta.price_bps == 488
        assert delta.delta_fp100 == 189

    def test_fill_message_subcent(self) -> None:
        raw: dict[str, Any] = {
            "trade_id": "t1",
            "order_id": "o1",
            "market_ticker": "MARJ-MKT",
            "yes_price_dollars": "0.0488",
            "count_fp": "1.89",
            "fee_cost": "0.0013",
            "post_position_fp": "1.89",
            "side": "yes",
        }
        msg = FillMessage.model_validate(raw)
        assert msg.yes_price_bps == 488
        assert msg.count_fp100 == 189
        assert msg.fee_cost_bps == 13
        assert msg.post_position_fp100 == 189


class TestWSFieldInvariants:
    """Pin the wire→bps contract for whole-cent values."""

    @pytest.mark.parametrize(
        "wire_dollars,bps",
        [("0.01", 100), ("0.53", 5300), ("0.99", 9900)],
    )
    def test_orderbook_delta(self, wire_dollars: str, bps: int) -> None:
        d = OrderBookDelta.model_validate(
            {
                "market_ticker": "x",
                "market_id": "y",
                "price_dollars": wire_dollars,
                "delta_fp": "1.00",
                "side": "yes",
                "ts": "0",
            }
        )
        assert d.price_bps == bps

    @pytest.mark.parametrize(
        "wire_dollars,bps",
        [("0.01", 100), ("0.53", 5300), ("0.99", 9900)],
    )
    def test_ticker(self, wire_dollars: str, bps: int) -> None:
        t = TickerMessage.model_validate(
            {
                "market_ticker": "x",
                "yes_bid_dollars": wire_dollars,
            }
        )
        assert t.yes_bid_bps == bps

    @pytest.mark.parametrize(
        "wire_dollars,bps",
        [("0.01", 100), ("0.53", 5300), ("0.99", 9900)],
    )
    def test_trade(self, wire_dollars: str, bps: int) -> None:
        tr = TradeMessage.model_validate(
            {
                "market_ticker": "x",
                "yes_price_dollars": wire_dollars,
                "count_fp": "1",
                "side": "yes",
                "ts": "0",
                "trade_id": "t",
            }
        )
        assert tr.price_bps == bps

    @pytest.mark.parametrize(
        "wire_dollars,bps",
        [("0.01", 100), ("0.53", 5300), ("0.99", 9900)],
    )
    def test_user_order(self, wire_dollars: str, bps: int) -> None:
        u = UserOrderMessage.model_validate(
            {
                "order_id": "o",
                "ticker": "x",
                "yes_price_dollars": wire_dollars,
            }
        )
        assert u.yes_price_bps == bps

    @pytest.mark.parametrize(
        "wire_dollars,bps",
        [("0.01", 100), ("0.53", 5300), ("0.99", 9900)],
    )
    def test_fill(self, wire_dollars: str, bps: int) -> None:
        f = FillMessage.model_validate(
            {
                "trade_id": "t",
                "order_id": "o",
                "market_ticker": "x",
                "yes_price_dollars": wire_dollars,
            }
        )
        assert f.yes_price_bps == bps

    @pytest.mark.parametrize(
        "wire_dollars,bps",
        [("0.01", 100), ("0.53", 5300), ("0.99", 9900)],
    )
    def test_market_position(self, wire_dollars: str, bps: int) -> None:
        mp = MarketPositionMessage.model_validate(
            {
                "market_ticker": "x",
                "position_cost_dollars": wire_dollars,
            }
        )
        assert mp.position_cost_bps == bps

    @pytest.mark.parametrize(
        "wire_dollars,bps",
        [("0.01", 100), ("0.53", 5300), ("0.99", 9900)],
    )
    def test_market_lifecycle(self, wire_dollars: str, bps: int) -> None:
        ml = MarketLifecycleMessage.model_validate(
            {
                "event_type": "determined",
                "market_ticker": "x",
                "result": "yes",
                "settlement_value": wire_dollars,
            }
        )
        assert ml.settlement_value_bps == bps

    @pytest.mark.parametrize(
        "wire_dollars,bps",
        [("0.01", 100), ("0.53", 5300), ("0.99", 9900)],
    )
    def test_orderbook_snapshot(self, wire_dollars: str, bps: int) -> None:
        s = OrderBookSnapshot.model_validate(
            {
                "market_ticker": "x",
                "market_id": "y",
                "yes_dollars_fp": [[wire_dollars, "10.00"]],
            }
        )
        assert s.yes_bps_fp100[0][0] == bps
