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


class TestOrderDualBpsFp100Fields:
    """Task 3b-Order: bps/fp100 fields populate alongside legacy cents/int."""

    def test_dual_population_whole_cents(self) -> None:
        """Wire '0.53' → yes_price==53 (legacy) AND yes_price_bps==5300 (new)."""
        data = {
            "order_id": "ord-dual-1",
            "ticker": "MKT-1",
            "side": "yes",
            "yes_price_dollars": "0.53",
            "no_price_dollars": "0.47",
        }
        o = Order.model_validate(data)
        assert o.yes_price == 53
        assert o.yes_price_bps == 5300
        assert o.no_price == 47
        assert o.no_price_bps == 4700

    def test_subcent_price_retained_in_bps(self) -> None:
        """Wire '0.0488' → yes_price==5 (lossy banker's round) AND yes_price_bps==488 (exact)."""
        data = {
            "order_id": "ord-dual-marj",
            "ticker": "MARJ-MKT",
            "side": "yes",
            "yes_price_dollars": "0.0488",
            "no_price_dollars": "0.9512",
        }
        o = Order.model_validate(data)
        # Legacy path: 4.88¢ banker's-rounds to 5.
        assert o.yes_price == 5
        # New path: exact sub-cent retained.
        assert o.yes_price_bps == 488
        # Complement: 9512 bps = 95.12¢ → 95 (half-even from .12 → down).
        assert o.no_price == 95
        assert o.no_price_bps == 9512

    def test_fractional_count_retained_in_fp100(self) -> None:
        """Wire '1.89' → fill_count==1 (legacy truncation) AND fill_count_fp100==189 (exact).

        This is the MARJ 1.89-contract maker-fill motivating bug: the legacy
        int field silently truncates to 1; the fp100 field preserves 189.
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
        # Legacy path: fractional → floor.
        assert o.fill_count == 1
        assert o.remaining_count == 8
        assert o.initial_count == 10
        # New path: exact fractional retained.
        assert o.fill_count_fp100 == 189
        assert o.remaining_count_fp100 == 811
        assert o.initial_count_fp100 == 1000

    def test_fill_cost_and_fees_dual_fields(self) -> None:
        """maker_fill_cost / taker_fill_cost / *_fees each get their _bps sibling."""
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
        # Legacy cents (lossy for 1.50¢ → 2 half-even).
        assert o.taker_fees == 2
        assert o.maker_fees == 2
        assert o.maker_fill_cost == 448
        assert o.taker_fill_cost == 0
        # New bps (exact).
        assert o.taker_fees_bps == 200
        assert o.maker_fees_bps == 150
        assert o.maker_fill_cost_bps == 44800
        assert o.taker_fill_cost_bps == 0

    def test_zero_defaults_when_wire_field_absent(self) -> None:
        """Wire omits price/count → BOTH legacy and new fields are 0."""
        data = {
            "order_id": "ord-empty",
            "ticker": "MKT-1",
            "side": "yes",
        }
        o = Order.model_validate(data)
        assert o.yes_price == 0 and o.yes_price_bps == 0
        assert o.no_price == 0 and o.no_price_bps == 0
        assert o.initial_count == 0 and o.initial_count_fp100 == 0
        assert o.remaining_count == 0 and o.remaining_count_fp100 == 0
        assert o.fill_count == 0 and o.fill_count_fp100 == 0
        assert o.taker_fees == 0 and o.taker_fees_bps == 0
        assert o.maker_fees == 0 and o.maker_fees_bps == 0
        assert o.maker_fill_cost == 0 and o.maker_fill_cost_bps == 0
        assert o.taker_fill_cost == 0 and o.taker_fill_cost_bps == 0

    def test_none_wire_field_yields_zero_in_both(self) -> None:
        """Wire field explicitly None → BOTH fields stay at default 0."""
        data = {
            "order_id": "ord-none",
            "ticker": "MKT-1",
            "side": "yes",
            "yes_price_dollars": None,
            "fill_count_fp": None,
        }
        o = Order.model_validate(data)
        assert o.yes_price == 0
        assert o.yes_price_bps == 0
        assert o.fill_count == 0
        assert o.fill_count_fp100 == 0


class TestFillDualBpsFp100Fields:
    """Task 3b-Order: Fill dual-field parity with Order."""

    def test_dual_population_whole_cents(self) -> None:
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
        assert f.yes_price == 40 and f.yes_price_bps == 4000
        assert f.no_price == 60 and f.no_price_bps == 6000
        assert f.count == 5 and f.count_fp100 == 500

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
        assert f.yes_price == 5  # lossy
        assert f.yes_price_bps == 488  # exact
        assert f.no_price_bps == 9512

    def test_fractional_count_retained_in_fp100(self) -> None:
        """Fill.count_fp='1.89' → count==1 (legacy trunc) AND count_fp100==189 (exact)."""
        data = {
            "trade_id": "trade-frac",
            "order_id": "ord-1",
            "ticker": "MARJ-MKT",
            "side": "yes",
            "count_fp": "1.89",
        }
        f = Fill.model_validate(data)
        assert f.count == 1
        assert f.count_fp100 == 189

    def test_fee_cost_string_populates_bps(self) -> None:
        """fee_cost='0.0130' → fee_cost==1 (rounded) AND fee_cost_bps==130 (exact)."""
        data = {
            "trade_id": "trade-fee",
            "order_id": "ord-1",
            "ticker": "MKT-1",
            "side": "no",
            "fee_cost": "0.0130",
        }
        f = Fill.model_validate(data)
        assert f.fee_cost == 1  # legacy half-even round
        assert f.fee_cost_bps == 130  # exact

    def test_fee_cost_integer_passthrough_leaves_bps_default(self) -> None:
        """Integer fee_cost path is passthrough (legacy-only) — fee_cost_bps stays default 0."""
        data = {
            "trade_id": "trade-fee-int",
            "order_id": "ord-1",
            "ticker": "MKT-1",
            "side": "no",
            "fee_cost": 5,
        }
        f = Fill.model_validate(data)
        assert f.fee_cost == 5
        # Integer path is the pre-migration shape; no bps promotion.
        assert f.fee_cost_bps == 0

    def test_zero_defaults_when_wire_field_absent(self) -> None:
        data = {
            "trade_id": "trade-empty",
            "order_id": "ord-1",
            "ticker": "MKT-1",
            "side": "yes",
        }
        f = Fill.model_validate(data)
        assert f.yes_price == 0 and f.yes_price_bps == 0
        assert f.no_price == 0 and f.no_price_bps == 0
        assert f.count == 0 and f.count_fp100 == 0
        assert f.fee_cost == 0 and f.fee_cost_bps == 0

    def test_none_wire_field_yields_zero_in_both(self) -> None:
        data = {
            "trade_id": "trade-none",
            "order_id": "ord-1",
            "ticker": "MKT-1",
            "side": "yes",
            "yes_price_dollars": None,
            "count_fp": None,
        }
        f = Fill.model_validate(data)
        assert f.yes_price == 0 and f.yes_price_bps == 0
        assert f.count == 0 and f.count_fp100 == 0


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
