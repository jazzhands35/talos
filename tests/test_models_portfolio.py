"""Tests for portfolio Pydantic models."""

import pytest

from talos.models.portfolio import Balance, EventPosition, ExchangeStatus, Position, Settlement


class TestBalance:
    def test_parse_balance_json(self) -> None:
        data = {
            "balance": 500000,
            "portfolio_value": 750000,
        }
        b = Balance.model_validate(data)
        assert b.balance == 500000
        assert b.portfolio_value == 750000

    def test_parse_balance_dollars_format(self) -> None:
        """Post March 12: balance_dollars/portfolio_value_dollars strings."""
        data = {
            "balance_dollars": "5000.00",
            "portfolio_value_dollars": "7500.00",
        }
        b = Balance.model_validate(data)
        assert b.balance == 500000
        assert b.portfolio_value == 750000


class TestPosition:
    def test_parse_position_json(self) -> None:
        data = {
            "ticker": "KXBTC-26MAR-T50000",
            "position": 10,
            "total_traded": 25,
            "market_exposure": 650,
        }
        p = Position.model_validate(data)
        assert p.ticker == "KXBTC-26MAR-T50000"
        assert p.position == 10

    def test_parse_position_fp_dollars_format(self) -> None:
        """Post March 12: _fp/_dollars string fields."""
        data = {
            "ticker": "KXBTC-26MAR-T50000",
            "position_fp": "10",
            "total_traded_dollars": "0.25",
            "market_exposure_dollars": "6.50",
            "resting_orders_count_fp": "3",
        }
        p = Position.model_validate(data)
        assert p.position == 10
        assert p.total_traded == 25
        assert p.market_exposure == 650
        assert p.resting_orders_count == 3

    def test_negative_position(self) -> None:
        data = {
            "ticker": "KXBTC-26MAR-T50000",
            "position": -5,
            "total_traded": 10,
            "market_exposure": 250,
        }
        p = Position.model_validate(data)
        assert p.position == -5


class TestSettlement:
    def test_parse_settlement_mixed_units(self) -> None:
        """revenue is cents int, fee_cost is dollars string — mixed in same response."""
        data = {
            "ticker": "MKT-YES",
            "event_ticker": "EVT-1",
            "market_result": "yes",
            "revenue": 500,
            "fee_cost": "0.0770",
            "yes_count_fp": "10",
            "no_count_fp": "0",
            "yes_total_cost_dollars": "4.50",
            "no_total_cost_dollars": "0.00",
            "settled_time": "2026-03-12T00:00:00Z",
        }
        s = Settlement.model_validate(data)
        assert s.revenue == 500  # cents int — untouched
        assert s.fee_cost == 8  # "0.0770" → round(7.70) = 8
        assert s.yes_count == 10
        assert s.no_count == 0
        assert s.yes_total_cost == 450
        assert s.no_total_cost == 0

    def test_parse_settlement_fee_cost_already_int(self) -> None:
        """If fee_cost is already int, leave it."""
        data = {
            "ticker": "MKT-YES",
            "revenue": 100,
            "fee_cost": 5,
        }
        s = Settlement.model_validate(data)
        assert s.fee_cost == 5

    def test_parse_settlement_value_dollars(self) -> None:
        """Per-contract payout from value_dollars."""
        data = {
            "ticker": "MKT-YES",
            "value_dollars": "1.00",
        }
        s = Settlement.model_validate(data)
        assert s.value == 100

    def test_settlement_minimal(self) -> None:
        """Only ticker is required."""
        s = Settlement.model_validate({"ticker": "MKT-1"})
        assert s.ticker == "MKT-1"
        assert s.revenue == 0
        assert s.fee_cost == 0


class TestEventPosition:
    def test_parse_event_position_minimal(self) -> None:
        """Only event_ticker required — backward compat."""
        ep = EventPosition.model_validate({"event_ticker": "EVT-1"})
        assert ep.event_ticker == "EVT-1"
        assert ep.total_cost == 0
        assert ep.realized_pnl == 0

    def test_parse_event_position_dollars_fp(self) -> None:
        """Full enriched response from Kalshi."""
        data = {
            "event_ticker": "EVT-1",
            "total_cost_dollars": "12.50",
            "total_cost_shares_fp": "25",
            "event_exposure_dollars": "10.00",
            "realized_pnl_dollars": "3.50",
            "resting_orders_count_fp": "4",
            "fees_paid_dollars": "0.44",
        }
        ep = EventPosition.model_validate(data)
        assert ep.total_cost == 1250
        assert ep.total_cost_shares == 25
        assert ep.event_exposure == 1000
        assert ep.realized_pnl == 350
        assert ep.resting_orders_count == 4
        assert ep.fees_paid == 44


class TestExchangeStatus:
    def test_parse_status_json(self) -> None:
        data = {
            "trading_active": True,
            "exchange_active": True,
        }
        es = ExchangeStatus.model_validate(data)
        assert es.trading_active is True


class TestBalanceDualBpsFp100Fields:
    """Task 3b-Portfolio: bps fields populate alongside legacy cents."""

    def test_dual_population_whole_cents(self) -> None:
        """Wire '5000.00' → balance==500000 cents AND balance_bps==50000000."""
        data = {
            "balance_dollars": "5000.00",
            "portfolio_value_dollars": "7500.00",
        }
        b = Balance.model_validate(data)
        assert b.balance == 500_000
        assert b.balance_bps == 50_000_000
        assert b.portfolio_value == 750_000
        assert b.portfolio_value_bps == 75_000_000

    def test_subcent_balance_retained_in_bps(self) -> None:
        """Sub-cent balance — legacy rounds, bps retains exact precision."""
        data = {
            "balance_dollars": "0.0488",
            "portfolio_value_dollars": "0.9512",
        }
        b = Balance.model_validate(data)
        # Legacy path: 4.88¢ banker's-rounds to 5.
        assert b.balance == 5
        assert b.balance_bps == 488  # exact
        assert b.portfolio_value == 95
        assert b.portfolio_value_bps == 9512

    def test_zero_defaults_when_wire_field_absent(self) -> None:
        """Legacy-only wire (integer cents) → new bps fields stay at default 0."""
        data = {"balance": 500_000, "portfolio_value": 750_000}
        b = Balance.model_validate(data)
        assert b.balance == 500_000
        assert b.portfolio_value == 750_000
        # No _dollars wire → no bps promotion.
        assert b.balance_bps == 0
        assert b.portfolio_value_bps == 0

    def test_none_wire_field_yields_zero_in_both(self) -> None:
        data = {
            "balance": 100,
            "portfolio_value": 200,
            "balance_dollars": None,
            "portfolio_value_dollars": None,
        }
        b = Balance.model_validate(data)
        assert b.balance == 100
        assert b.balance_bps == 0
        assert b.portfolio_value == 200
        assert b.portfolio_value_bps == 0


class TestPositionDualBpsFp100Fields:
    """Task 3b-Portfolio: Position dual-field parity."""

    def test_dual_population_whole_values(self) -> None:
        data = {
            "ticker": "KXBTC-26MAR-T50000",
            "position_fp": "10",
            "total_traded_dollars": "0.25",
            "market_exposure_dollars": "6.50",
            "resting_orders_count_fp": "3",
            "realized_pnl_dollars": "1.00",
            "fees_paid_dollars": "0.44",
        }
        p = Position.model_validate(data)
        # Legacy cents / whole contracts.
        assert p.position == 10
        assert p.total_traded == 25
        assert p.market_exposure == 650
        assert p.resting_orders_count == 3
        assert p.realized_pnl == 100
        assert p.fees_paid == 44
        # New bps / fp100.
        assert p.position_fp100 == 1000
        assert p.total_traded_bps == 2500
        assert p.market_exposure_bps == 65_000
        assert p.resting_orders_count_fp100 == 300
        assert p.realized_pnl_bps == 10_000
        assert p.fees_paid_bps == 4400

    def test_subcent_costs_retained_in_bps(self) -> None:
        """Sub-cent legacy rounding vs exact bps retention."""
        data = {
            "ticker": "MARJ-MKT",
            "total_traded_dollars": "0.0488",
            "market_exposure_dollars": "0.0150",
            "realized_pnl_dollars": "0.0025",
            "fees_paid_dollars": "0.0075",
        }
        p = Position.model_validate(data)
        # Legacy cents (lossy / half-even).
        assert p.total_traded == 5  # 4.88¢ → 5
        assert p.market_exposure == 2  # 1.50¢ → 2 half-even
        # New bps (exact).
        assert p.total_traded_bps == 488
        assert p.market_exposure_bps == 150
        assert p.realized_pnl_bps == 25
        assert p.fees_paid_bps == 75

    def test_fractional_position_retained_in_fp100(self) -> None:
        """position_fp='1.89' → position==1 (legacy trunc) AND position_fp100==189 (exact).

        This is the MARJ fractional-contract bug: the legacy int field
        silently truncates, losing 0.89 contracts of exposure. fp100
        preserves the exact count.
        """
        data = {
            "ticker": "MARJ-MKT",
            "position_fp": "1.89",
            "resting_orders_count_fp": "0.50",
        }
        p = Position.model_validate(data)
        assert p.position == 1
        assert p.position_fp100 == 189
        assert p.resting_orders_count == 0
        assert p.resting_orders_count_fp100 == 50

    def test_negative_fractional_position_floor_divergence(self) -> None:
        """Python // floors toward -∞: fp_to_int('-5.50') = -550 // 100 = -6.

        The legacy path therefore reports MORE NO contracts than Kalshi actually
        holds, while fp100 keeps the exact signed count. This is a divergence
        we document rather than fix — legacy callers reading `position` on a
        fractional NO position are subtly wrong; fp100 callers are correct.
        """
        data = {
            "ticker": "MARJ-MKT",
            "position_fp": "-5.50",
        }
        p = Position.model_validate(data)
        # Legacy floor-div toward -∞ overshoots by one.
        assert p.position == -6
        # fp100 exact.
        assert p.position_fp100 == -550

    def test_zero_defaults_when_wire_fields_absent(self) -> None:
        p = Position.model_validate({"ticker": "MKT-1"})
        assert p.position == 0 and p.position_fp100 == 0
        assert p.total_traded == 0 and p.total_traded_bps == 0
        assert p.market_exposure == 0 and p.market_exposure_bps == 0
        assert p.resting_orders_count == 0 and p.resting_orders_count_fp100 == 0
        assert p.realized_pnl == 0 and p.realized_pnl_bps == 0
        assert p.fees_paid == 0 and p.fees_paid_bps == 0


class TestEventPositionDualBpsFp100Fields:
    """Task 3b-Portfolio: EventPosition dual-field parity."""

    def test_dual_population_whole_values(self) -> None:
        data = {
            "event_ticker": "EVT-1",
            "total_cost_dollars": "12.50",
            "total_cost_shares_fp": "25",
            "event_exposure_dollars": "10.00",
            "realized_pnl_dollars": "3.50",
            "resting_orders_count_fp": "4",
            "fees_paid_dollars": "0.44",
        }
        ep = EventPosition.model_validate(data)
        # Legacy.
        assert ep.total_cost == 1250
        assert ep.total_cost_shares == 25
        assert ep.event_exposure == 1000
        assert ep.realized_pnl == 350
        assert ep.resting_orders_count == 4
        assert ep.fees_paid == 44
        # New bps / fp100.
        assert ep.total_cost_bps == 125_000
        assert ep.total_cost_shares_fp100 == 2500
        assert ep.event_exposure_bps == 100_000
        assert ep.realized_pnl_bps == 35_000
        assert ep.resting_orders_count_fp100 == 400
        assert ep.fees_paid_bps == 4400

    def test_subcent_costs_retained_in_bps(self) -> None:
        data = {
            "event_ticker": "MARJ-EVT",
            "total_cost_dollars": "0.0488",
            "event_exposure_dollars": "0.0150",
            "fees_paid_dollars": "0.0075",
            "realized_pnl_dollars": "0.0025",
        }
        ep = EventPosition.model_validate(data)
        # Legacy (lossy).
        assert ep.total_cost == 5  # half-even
        # New bps (exact).
        assert ep.total_cost_bps == 488
        assert ep.event_exposure_bps == 150
        assert ep.fees_paid_bps == 75
        assert ep.realized_pnl_bps == 25

    def test_fractional_total_cost_shares_retained_in_fp100(self) -> None:
        """total_cost_shares_fp='1.89' → shares==1 legacy, 189 fp100."""
        data = {
            "event_ticker": "MARJ-EVT",
            "total_cost_shares_fp": "1.89",
            "resting_orders_count_fp": "2.50",
        }
        ep = EventPosition.model_validate(data)
        assert ep.total_cost_shares == 1
        assert ep.total_cost_shares_fp100 == 189
        assert ep.resting_orders_count == 2
        assert ep.resting_orders_count_fp100 == 250

    def test_zero_defaults_when_wire_fields_absent(self) -> None:
        ep = EventPosition.model_validate({"event_ticker": "EVT-1"})
        assert ep.total_cost == 0 and ep.total_cost_bps == 0
        assert ep.total_cost_shares == 0 and ep.total_cost_shares_fp100 == 0
        assert ep.event_exposure == 0 and ep.event_exposure_bps == 0
        assert ep.realized_pnl == 0 and ep.realized_pnl_bps == 0
        assert ep.resting_orders_count == 0 and ep.resting_orders_count_fp100 == 0
        assert ep.fees_paid == 0 and ep.fees_paid_bps == 0


class TestSettlementDualBpsFp100Fields:
    """Task 3b-Portfolio: Settlement dual-field parity + special cases."""

    def test_dual_population_whole_values(self) -> None:
        data = {
            "ticker": "MKT-YES",
            "event_ticker": "EVT-1",
            "market_result": "yes",
            "revenue": 500,  # cents int, NOT dollars string
            "fee_cost": "0.0770",
            "yes_count_fp": "10",
            "no_count_fp": "0",
            "yes_total_cost_dollars": "4.50",
            "no_total_cost_dollars": "0.00",
            "value_dollars": "1.00",
            "settled_time": "2026-03-12T00:00:00Z",
        }
        s = Settlement.model_validate(data)
        # Legacy.
        assert s.revenue == 500
        assert s.fee_cost == 8  # 0.0770 → 7.70¢ → 8 half-even
        assert s.yes_count == 10
        assert s.no_count == 0
        assert s.yes_total_cost == 450
        assert s.no_total_cost == 0
        assert s.value == 100
        # New bps / fp100.
        assert s.revenue_bps == 50_000  # cents * 100
        assert s.fee_cost_bps == 770  # exact
        assert s.yes_count_fp100 == 1000
        assert s.no_count_fp100 == 0
        assert s.yes_total_cost_bps == 45_000
        assert s.no_total_cost_bps == 0
        assert s.value_bps == 10_000

    def test_revenue_special_case_cents_times_100(self) -> None:
        """revenue is int cents on the wire; revenue_bps == revenue * 100 exactly."""
        s = Settlement.model_validate({"ticker": "MKT-1", "revenue": 53})
        assert s.revenue == 53
        assert s.revenue_bps == 5300

    def test_revenue_zero_default(self) -> None:
        """Revenue absent → both legacy and bps stay at 0."""
        s = Settlement.model_validate({"ticker": "MKT-1"})
        assert s.revenue == 0
        assert s.revenue_bps == 0

    def test_fee_cost_from_fee_cost_dollars_path(self) -> None:
        """New FP-variant wire (fee_cost_dollars) dual-populates."""
        data = {
            "ticker": "MKT-1",
            "fee_cost_dollars": "0.0770",
        }
        s = Settlement.model_validate(data)
        assert s.fee_cost == 8
        assert s.fee_cost_bps == 770

    def test_fee_cost_from_legacy_string_path(self) -> None:
        """Legacy string-fee_cost wire dual-populates identically."""
        data = {
            "ticker": "MKT-1",
            "fee_cost": "0.0770",
        }
        s = Settlement.model_validate(data)
        assert s.fee_cost == 8
        assert s.fee_cost_bps == 770

    def test_fee_cost_integer_passthrough_leaves_bps_default(self) -> None:
        """Integer fee_cost path (pre-migration) — fee_cost_bps stays 0."""
        data = {"ticker": "MKT-1", "fee_cost": 5}
        s = Settlement.model_validate(data)
        assert s.fee_cost == 5
        assert s.fee_cost_bps == 0

    def test_subcent_value_retained_in_bps(self) -> None:
        """Sub-cent per-contract payout — legacy rounds, bps exact."""
        data = {"ticker": "MKT-1", "value_dollars": "0.0488"}
        s = Settlement.model_validate(data)
        assert s.value == 5  # half-even
        assert s.value_bps == 488

    def test_fractional_yes_count_retained_in_fp100(self) -> None:
        """yes_count_fp='1.89' → yes_count==1 (legacy floor) AND yes_count_fp100==189."""
        data = {
            "ticker": "MARJ-MKT",
            "yes_count_fp": "1.89",
            "no_count_fp": "0.50",
        }
        s = Settlement.model_validate(data)
        assert s.yes_count == 1
        assert s.yes_count_fp100 == 189
        assert s.no_count == 0
        assert s.no_count_fp100 == 50

    def test_zero_defaults_when_wire_fields_absent(self) -> None:
        s = Settlement.model_validate({"ticker": "MKT-1"})
        assert s.revenue == 0 and s.revenue_bps == 0
        assert s.fee_cost == 0 and s.fee_cost_bps == 0
        assert s.yes_count == 0 and s.yes_count_fp100 == 0
        assert s.no_count == 0 and s.no_count_fp100 == 0
        assert s.yes_total_cost == 0 and s.yes_total_cost_bps == 0
        assert s.no_total_cost == 0 and s.no_total_cost_bps == 0
        assert s.value is None and s.value_bps is None


class TestPortfolioDualFieldInvariants:
    """Parallel-field contract: for whole-cent wire values the _bps field
    equals the legacy cents field × 100 exactly. Expressed against
    parametrized values (NOT ``m.field * 100``) so Pyright's ``int | None``
    narrowing cannot trip — and so the contract is pinned per-class.
    """

    @pytest.mark.parametrize(
        "wire_dollars,cents,bps",
        [
            ("0.01", 1, 100),
            ("500.00", 50_000, 5_000_000),
            ("7500.50", 750_050, 75_005_000),
        ],
    )
    def test_balance_bps_equals_cents_times_100(
        self, wire_dollars: str, cents: int, bps: int
    ) -> None:
        b = Balance.model_validate({
            "balance_dollars": wire_dollars,
            "portfolio_value_dollars": wire_dollars,
        })
        assert b.balance == cents
        assert b.balance_bps == bps
        assert bps == cents * 100

    @pytest.mark.parametrize(
        "wire_dollars,cents,bps",
        [
            ("0.25", 25, 2_500),
            ("6.50", 650, 65_000),
            ("100.00", 10_000, 1_000_000),
        ],
    )
    def test_position_total_traded_bps_equals_cents_times_100(
        self, wire_dollars: str, cents: int, bps: int
    ) -> None:
        p = Position.model_validate({
            "ticker": "MKT-1",
            "total_traded_dollars": wire_dollars,
        })
        assert p.total_traded == cents
        assert p.total_traded_bps == bps
        assert bps == cents * 100

    @pytest.mark.parametrize(
        "wire_dollars,cents,bps",
        [
            ("12.50", 1_250, 125_000),
            ("0.44", 44, 4_400),
            ("100.00", 10_000, 1_000_000),
        ],
    )
    def test_event_position_total_cost_bps_equals_cents_times_100(
        self, wire_dollars: str, cents: int, bps: int
    ) -> None:
        ep = EventPosition.model_validate({
            "event_ticker": "EVT-1",
            "total_cost_dollars": wire_dollars,
        })
        assert ep.total_cost == cents
        assert ep.total_cost_bps == bps
        assert bps == cents * 100

    @pytest.mark.parametrize(
        "revenue_cents",
        [1, 53, 500, 10_000],
    )
    def test_settlement_revenue_bps_equals_cents_times_100(
        self, revenue_cents: int
    ) -> None:
        """revenue is already cents on the wire — revenue_bps is cents * 100 exactly."""
        s = Settlement.model_validate({"ticker": "MKT-1", "revenue": revenue_cents})
        assert s.revenue == revenue_cents
        assert s.revenue_bps == revenue_cents * 100


class TestPortfolioAggregateSubBpsPrecision:
    """Kalshi's /portfolio endpoints emit aggregate money fields (sums
    across many trades) with 6-decimal precision — e.g.
    event_exposure_dollars='20.168040' scales to 201680.4 bps, a
    sub-bps value.

    The strict :func:`dollars_to_bps` parser fail-closes on those,
    which was correct for per-contract prices but wrong for aggregate
    sums. Portfolio model validators route aggregate fields through
    :func:`dollars_to_bps_round` (half-even to nearest bps) so these
    payloads load cleanly.

    Regression for the crash on production-like payload at runtime
    (EventPosition validator refused '20.168040' at startup).
    """

    def test_event_position_accepts_sub_bps_aggregate_payload(self) -> None:
        # Exact payload shape from the live Kalshi /portfolio/positions
        # response that crashed Talos when this class first landed.
        ep = EventPosition.model_validate({
            "event_ticker": "KXTRUMPSAY-26APR27",
            "event_exposure_dollars": "20.168040",    # 201680.4 bps — sub-bps
            "fees_paid_dollars": "0.058000",          # 580 bps — exact
            "realized_pnl_dollars": "2.636040",       # 26360.4 bps — sub-bps
            "total_cost_dollars": "97.532000",        # 975320 bps — exact
            "total_cost_shares_fp": "191.59",
        })
        # Half-even rounds: 201680.4 → 201680 ; 26360.4 → 26360 ; 580 / 975320 exact.
        assert ep.event_exposure_bps == 201_680
        assert ep.realized_pnl_bps == 26_360
        assert ep.fees_paid_bps == 580
        assert ep.total_cost_bps == 975_320
        # Legacy cents path rounds the same value to 2017 cents (half-even of 20.168040).
        assert ep.event_exposure == 2_017

    def test_position_accepts_sub_bps_aggregate_payload(self) -> None:
        p = Position.model_validate({
            "ticker": "KXTRUMPSAY-26APR27-YES",
            "position_fp": "100.00",
            "total_traded_dollars": "15.432140",       # 154321.4 bps — sub-bps
            "market_exposure_dollars": "0.500050",     # 5000.5 bps — sub-bps
            "realized_pnl_dollars": "1.234560",        # 12345.6 bps — sub-bps
            "fees_paid_dollars": "0.012340",           # 123.4 bps — sub-bps
        })
        assert p.total_traded_bps == 154_321
        assert p.market_exposure_bps == 5_000      # 5000.5 → 5000 (half-even to even)
        assert p.realized_pnl_bps == 12_346        # 12345.6 → 12346 (half-even)
        assert p.fees_paid_bps == 123

    def test_balance_accepts_sub_bps_payload(self) -> None:
        b = Balance.model_validate({
            "balance": 0, "portfolio_value": 0,
            "balance_dollars": "1234.567890",        # 12345678.9 bps — sub-bps
            "portfolio_value_dollars": "250.000050",  # 2500000.5 bps — sub-bps
        })
        assert b.balance_bps == 12_345_679
        assert b.portfolio_value_bps == 2_500_000
