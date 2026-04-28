"""Tests for order Pydantic models."""

import pytest

from talos.models.order import BatchOrderResult, Fill, Order


class TestOrder:
    def test_parse_order_dollars_fp_format(self) -> None:
        """Post March 12: _dollars/_fp string fields → bps / fp100."""
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
        assert o.yes_price_bps == 6500
        assert o.no_price_bps == 3500
        assert o.initial_count_fp100 == 1000
        assert o.remaining_count_fp100 == 700
        assert o.fill_count_fp100 == 300
        assert o.taker_fees_bps == 200
        assert o.maker_fees_bps == 100

    def test_parse_order_fill_cost_dollars(self) -> None:
        """maker_fill_cost_dollars and taker_fill_cost_dollars → bps."""
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
        assert o.maker_fill_cost_bps == 44800
        assert o.taker_fill_cost_bps == 0

    def test_order_optional_fields(self) -> None:
        data = {
            "order_id": "ord-123",
            "ticker": "TEST-MKT",
            "side": "no",
            "order_type": "limit",
            "status": "resting",
            "created_time": "2026-03-03T12:00:00Z",
        }
        o = Order.model_validate(data)
        assert o.expiration_time is None


class TestFill:
    def test_parse_fill_dollars_fp_format(self) -> None:
        """Post March 12: _dollars/_fp string fields → bps / fp100."""
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
        assert f.yes_price_bps == 4000
        assert f.no_price_bps == 6000
        assert f.count_fp100 == 500

    def test_fill_fee_cost_string_conversion(self) -> None:
        """fee_cost arrives as FixedPointDollars string → bps."""
        data = {
            "trade_id": "trade-fee",
            "order_id": "ord-1",
            "ticker": "MKT-1",
            "side": "no",
            "fee_cost": "0.0130",
        }
        f = Fill.model_validate(data)
        assert f.fee_cost_bps == 130

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


class TestOrderBpsFp100Fields:
    """Task 3b-Order: bps/fp100 fields populate from wire _dollars/_fp."""

    def test_whole_cents(self) -> None:
        """Wire '0.53' → yes_price_bps==5300."""
        data = {
            "order_id": "ord-dual-1",
            "ticker": "MKT-1",
            "side": "yes",
            "yes_price_dollars": "0.53",
            "no_price_dollars": "0.47",
        }
        o = Order.model_validate(data)
        assert o.yes_price_bps == 5300
        assert o.no_price_bps == 4700

    def test_subcent_price_retained_in_bps(self) -> None:
        """Wire '0.0488' → yes_price_bps==488 (exact sub-cent retained)."""
        data = {
            "order_id": "ord-dual-marj",
            "ticker": "MARJ-MKT",
            "side": "yes",
            "yes_price_dollars": "0.0488",
            "no_price_dollars": "0.9512",
        }
        o = Order.model_validate(data)
        assert o.yes_price_bps == 488
        assert o.no_price_bps == 9512

    def test_fractional_count_retained_in_fp100(self) -> None:
        """Wire '1.89' → fill_count_fp100==189 (exact retained).

        This is the MARJ 1.89-contract maker-fill motivating bug: the
        fp100 field preserves 189 where legacy int would have floored to 1.
        """
        data = {
            "order_id": "ord-dual-frac",
            "ticker": "MARJ-MKT",
            "side": "yes",
            "fill_count_fp": "1.89",
            "remaining_count_fp": "8.11",
            "initial_count_fp": "10.00",
        }
        o = Order.model_validate(data)
        assert o.fill_count_fp100 == 189
        assert o.remaining_count_fp100 == 811
        assert o.initial_count_fp100 == 1000

    def test_fill_cost_and_fees_bps_fields(self) -> None:
        """maker_fill_cost / taker_fill_cost / *_fees populate as bps."""
        data = {
            "order_id": "ord-dual-fees",
            "ticker": "MKT-1",
            "side": "no",
            "taker_fees_dollars": "0.02",
            "maker_fees_dollars": "0.0150",
            "maker_fill_cost_dollars": "4.48",
            "taker_fill_cost_dollars": "0.00",
        }
        o = Order.model_validate(data)
        assert o.taker_fees_bps == 200
        assert o.maker_fees_bps == 150
        assert o.maker_fill_cost_bps == 44800
        assert o.taker_fill_cost_bps == 0

    def test_zero_defaults_when_wire_field_absent(self) -> None:
        """Wire omits price/count → bps/fp100 fields are 0."""
        data = {
            "order_id": "ord-empty",
            "ticker": "MKT-1",
            "side": "yes",
        }
        o = Order.model_validate(data)
        assert o.yes_price_bps == 0
        assert o.no_price_bps == 0
        assert o.initial_count_fp100 == 0
        assert o.remaining_count_fp100 == 0
        assert o.fill_count_fp100 == 0
        assert o.taker_fees_bps == 0
        assert o.maker_fees_bps == 0
        assert o.maker_fill_cost_bps == 0
        assert o.taker_fill_cost_bps == 0

    def test_none_wire_field_yields_zero(self) -> None:
        """Wire field explicitly None → field stays at default 0."""
        data = {
            "order_id": "ord-none",
            "ticker": "MKT-1",
            "side": "yes",
            "yes_price_dollars": None,
            "fill_count_fp": None,
        }
        o = Order.model_validate(data)
        assert o.yes_price_bps == 0
        assert o.fill_count_fp100 == 0


class TestFillBpsFp100Fields:
    """Task 3b-Order: Fill bps/fp100 parity with Order."""

    def test_whole_cents(self) -> None:
        data = {
            "trade_id": "trade-dual-1",
            "order_id": "ord-1",
            "ticker": "MKT-1",
            "side": "no",
            "yes_price_dollars": "0.40",
            "no_price_dollars": "0.60",
            "count_fp": "5",
        }
        f = Fill.model_validate(data)
        assert f.yes_price_bps == 4000
        assert f.no_price_bps == 6000
        assert f.count_fp100 == 500

    def test_subcent_price_retained_in_bps(self) -> None:
        data = {
            "trade_id": "trade-marj",
            "order_id": "ord-1",
            "ticker": "MARJ-MKT",
            "side": "yes",
            "yes_price_dollars": "0.0488",
            "no_price_dollars": "0.9512",
        }
        f = Fill.model_validate(data)
        assert f.yes_price_bps == 488
        assert f.no_price_bps == 9512

    def test_fractional_count_retained_in_fp100(self) -> None:
        """Fill.count_fp='1.89' → count_fp100==189 (exact)."""
        data = {
            "trade_id": "trade-frac",
            "order_id": "ord-1",
            "ticker": "MARJ-MKT",
            "side": "yes",
            "count_fp": "1.89",
        }
        f = Fill.model_validate(data)
        assert f.count_fp100 == 189

    def test_fee_cost_string_populates_bps(self) -> None:
        """fee_cost='0.0130' → fee_cost_bps==130 (exact)."""
        data = {
            "trade_id": "trade-fee",
            "order_id": "ord-1",
            "ticker": "MKT-1",
            "side": "no",
            "fee_cost": "0.0130",
        }
        f = Fill.model_validate(data)
        assert f.fee_cost_bps == 130

    def test_zero_defaults_when_wire_field_absent(self) -> None:
        data = {
            "trade_id": "trade-empty",
            "order_id": "ord-1",
            "ticker": "MKT-1",
            "side": "yes",
        }
        f = Fill.model_validate(data)
        assert f.yes_price_bps == 0
        assert f.no_price_bps == 0
        assert f.count_fp100 == 0
        assert f.fee_cost_bps == 0

    def test_none_wire_field_yields_zero(self) -> None:
        data = {
            "trade_id": "trade-none",
            "order_id": "ord-1",
            "ticker": "MKT-1",
            "side": "yes",
            "yes_price_dollars": None,
            "count_fp": None,
        }
        f = Fill.model_validate(data)
        assert f.yes_price_bps == 0
        assert f.count_fp100 == 0


class TestBpsWireInvariants:
    """Pin the wire→bps contract: dollars string is the single source of
    truth; 0.53 → 5_300 bps exactly.
    """

    @pytest.mark.parametrize(
        "wire_dollars,bps",
        [
            ("0.01", 100),
            ("0.53", 5_300),
            ("1.00", 10_000),
            ("0.99", 9_900),
        ],
    )
    def test_order_bps_from_wire(self, wire_dollars: str, bps: int) -> None:
        o = Order.model_validate(
            {
                "order_id": "x",
                "ticker": "y",
                "side": "yes",
                "yes_price_dollars": wire_dollars,
            }
        )
        assert o.yes_price_bps == bps

    @pytest.mark.parametrize(
        "wire_dollars,bps",
        [
            ("0.01", 100),
            ("0.53", 5_300),
            ("1.00", 10_000),
        ],
    )
    def test_fill_bps_from_wire(self, wire_dollars: str, bps: int) -> None:
        f = Fill.model_validate(
            {
                "trade_id": "t",
                "order_id": "o",
                "ticker": "y",
                "side": "yes",
                "yes_price_dollars": wire_dollars,
            }
        )
        assert f.yes_price_bps == bps

    def test_order_accepts_float_at_validation_time_for_whole_cent(self) -> None:
        """Legacy JSON payloads sometimes arrive with floats. The parser
        accepts them for whole-cent values. Pin this compatibility path."""
        o = Order.model_validate(
            {
                "order_id": "x",
                "ticker": "y",
                "side": "yes",
                "yes_price_dollars": 0.53,  # float, not str
            }
        )
        assert o.yes_price_bps == 5_300


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
