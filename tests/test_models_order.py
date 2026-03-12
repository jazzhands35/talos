"""Tests for order Pydantic models."""

from talos.models.order import BatchOrderResult, Fill, Order


class TestOrder:
    def test_parse_order_json(self) -> None:
        data = {
            "order_id": "ord-abc-123",
            "ticker": "KXBTC-26MAR-T50000",
            "side": "yes",
            "order_type": "limit",
            "price": 65,
            "count": 10,
            "remaining_count": 10,
            "fill_count": 0,
            "status": "resting",
            "created_time": "2026-03-03T12:00:00Z",
        }
        o = Order.model_validate(data)
        assert o.order_id == "ord-abc-123"
        assert o.side == "yes"
        assert o.remaining_count == 10

    def test_parse_order_dollars_fp_format(self) -> None:
        """Post March 12: _dollars/_fp string fields → int cents/int counts."""
        data = {
            "order_id": "ord-abc-123",
            "ticker": "KXBTC-26MAR-T50000",
            "side": "yes",
            "type": "limit",
            "yes_price_dollars": "0.65",
            "no_price_dollars": "0.35",
            "initial_count_fp": "10",
            "remaining_count_fp": "7",
            "fill_count_fp": "3",
            "taker_fees_dollars": "0.02",
            "maker_fees_dollars": "0.01",
            "status": "resting",
            "created_time": "2026-03-12T12:00:00Z",
        }
        o = Order.model_validate(data)
        assert o.yes_price == 65
        assert o.no_price == 35
        assert o.initial_count == 10
        assert o.remaining_count == 7
        assert o.fill_count == 3
        assert o.taker_fees == 2
        assert o.maker_fees == 1

    def test_parse_order_fill_cost_dollars(self) -> None:
        """maker_fill_cost_dollars and taker_fill_cost_dollars → cents."""
        data = {
            "order_id": "ord-fill-cost",
            "ticker": "MKT-1",
            "side": "no",
            "type": "limit",
            "maker_fill_cost_dollars": "4.48",
            "taker_fill_cost_dollars": "0.00",
            "fill_count_fp": "10",
            "remaining_count_fp": "0",
            "initial_count_fp": "10",
            "status": "executed",
        }
        o = Order.model_validate(data)
        assert o.maker_fill_cost == 448
        assert o.taker_fill_cost == 0

    def test_order_optional_fields(self) -> None:
        data = {
            "order_id": "ord-123",
            "ticker": "TEST-MKT",
            "side": "no",
            "order_type": "limit",
            "price": 40,
            "count": 5,
            "remaining_count": 5,
            "fill_count": 0,
            "status": "resting",
            "created_time": "2026-03-03T12:00:00Z",
        }
        o = Order.model_validate(data)
        assert o.expiration_time is None


class TestFill:
    def test_parse_fill_json(self) -> None:
        data = {
            "trade_id": "trade-xyz",
            "order_id": "ord-abc-123",
            "ticker": "KXBTC-26MAR-T50000",
            "side": "yes",
            "price": 65,
            "count": 5,
            "created_time": "2026-03-03T12:01:00Z",
        }
        f = Fill.model_validate(data)
        assert f.trade_id == "trade-xyz"
        assert f.count == 5

    def test_parse_fill_dollars_fp_format(self) -> None:
        """Post March 12: _dollars/_fp string fields."""
        data = {
            "trade_id": "trade-xyz",
            "order_id": "ord-abc-123",
            "ticker": "KXBTC-26MAR-T50000",
            "side": "no",
            "yes_price_dollars": "0.40",
            "no_price_dollars": "0.60",
            "count_fp": "5",
            "created_time": "2026-03-12T12:01:00Z",
        }
        f = Fill.model_validate(data)
        assert f.yes_price == 40
        assert f.no_price == 60
        assert f.count == 5

    def test_fill_fee_cost_string_conversion(self) -> None:
        """fee_cost arrives as FixedPointDollars string → cents."""
        data = {
            "trade_id": "trade-fee",
            "order_id": "ord-1",
            "ticker": "MKT-1",
            "side": "no",
            "fee_cost": "0.0130",
        }
        f = Fill.model_validate(data)
        assert f.fee_cost == 1  # rounds to 1 cent

    def test_fill_fee_cost_integer_passthrough(self) -> None:
        """fee_cost as integer should pass through unchanged."""
        data = {
            "trade_id": "trade-fee2",
            "order_id": "ord-2",
            "ticker": "MKT-2",
            "side": "yes",
            "fee_cost": 5,
        }
        f = Fill.model_validate(data)
        assert f.fee_cost == 5

    def test_fill_enriched_fields(self) -> None:
        """action, is_taker, purchased_side should be captured."""
        data = {
            "trade_id": "trade-enrich",
            "order_id": "ord-3",
            "ticker": "MKT-3",
            "side": "no",
            "action": "buy",
            "is_taker": True,
            "purchased_side": "no",
        }
        f = Fill.model_validate(data)
        assert f.action == "buy"
        assert f.is_taker is True
        assert f.purchased_side == "no"


class TestBatchOrderResult:
    def test_success_result(self) -> None:
        data = {
            "order_id": "ord-abc",
            "success": True,
        }
        r = BatchOrderResult.model_validate(data)
        assert r.success is True
        assert r.error is None

    def test_failure_result(self) -> None:
        data = {
            "order_id": "ord-def",
            "success": False,
            "error": "insufficient balance",
        }
        r = BatchOrderResult.model_validate(data)
        assert r.success is False
        assert r.error == "insufficient balance"
