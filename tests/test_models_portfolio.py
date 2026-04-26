"""Tests for portfolio Pydantic models."""

import pytest

from talos.models.portfolio import Balance, EventPosition, ExchangeStatus, Position, Settlement


class TestBalance:
    def test_parse_balance_dollars_format(self) -> None:
        """Future-proof: if Kalshi migrates /portfolio/balance to _dollars strings."""
        data = {
            "balance_dollars": "5000.00",
            "portfolio_value_dollars": "7500.00",
        }
        b = Balance.model_validate(data)
        assert b.balance_bps == 50_000_000
        assert b.portfolio_value_bps == 75_000_000

    def test_parse_balance_integer_cents_wire_shape(self) -> None:
        """Actual Kalshi /portfolio/balance wire shape as of 2026-04-24:
        integer cents under ``balance`` / ``portfolio_value`` — unmigrated
        from the _dollars format the rest of portfolio/ uses. This is the
        regression guard for the bug that surfaced post-13a-2c (the
        validator silently produced 0/0 because it only matched _dollars).
        """
        data = {
            "balance": 434031,
            "portfolio_value": 35992,
            "updated_ts": 1777034248,
        }
        b = Balance.model_validate(data)
        # 434031 cents = $4,340.31 = 43,403,100 bps
        assert b.balance_bps == 43_403_100
        assert b.portfolio_value_bps == 3_599_200


class TestPosition:
    def test_parse_position_fp_dollars_format(self) -> None:
        """Post March 12: _fp/_dollars string fields → bps/fp100. resting_orders_count
        stays integer on the wire (no _fp variant — verified 2026-04-25)."""
        data = {
            "ticker": "KXBTC-26MAR-T50000",
            "position_fp": "10",
            "total_traded_dollars": "0.25",
            "market_exposure_dollars": "6.50",
            "resting_orders_count": 3,
        }
        p = Position.model_validate(data)
        assert p.position_fp100 == 1000
        assert p.total_traded_bps == 2500
        assert p.market_exposure_bps == 65_000
        assert p.resting_orders_count == 3

    def test_negative_position(self) -> None:
        data = {
            "ticker": "KXBTC-26MAR-T50000",
            "position_fp": "-5",
        }
        p = Position.model_validate(data)
        assert p.position_fp100 == -500


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
        assert s.revenue_bps == 50_000  # cents*100
        assert s.fee_cost_bps == 770
        assert s.yes_count_fp100 == 1000
        assert s.no_count_fp100 == 0
        assert s.yes_total_cost_bps == 45_000
        assert s.no_total_cost_bps == 0

    def test_parse_settlement_value_dollars(self) -> None:
        """Per-contract payout from value_dollars."""
        data = {
            "ticker": "MKT-YES",
            "value_dollars": "1.00",
        }
        s = Settlement.model_validate(data)
        assert s.value_bps == 10_000

    def test_settlement_minimal(self) -> None:
        """Only ticker is required."""
        s = Settlement.model_validate({"ticker": "MKT-1"})
        assert s.ticker == "MKT-1"
        assert s.revenue_bps == 0
        assert s.fee_cost_bps == 0


class TestEventPosition:
    def test_parse_event_position_minimal(self) -> None:
        """Only event_ticker required — backward compat."""
        ep = EventPosition.model_validate({"event_ticker": "EVT-1"})
        assert ep.event_ticker == "EVT-1"
        assert ep.total_cost_bps == 0
        assert ep.realized_pnl_bps == 0

    def test_parse_event_position_dollars_fp(self) -> None:
        """Full enriched response from Kalshi. event_positions[] entries do NOT
        carry a resting-orders count — verified 2026-04-25 against production."""
        data = {
            "event_ticker": "EVT-1",
            "total_cost_dollars": "12.50",
            "total_cost_shares_fp": "25",
            "event_exposure_dollars": "10.00",
            "realized_pnl_dollars": "3.50",
            "fees_paid_dollars": "0.44",
        }
        ep = EventPosition.model_validate(data)
        assert ep.total_cost_bps == 125_000
        assert ep.total_cost_shares_fp100 == 2500
        assert ep.event_exposure_bps == 100_000
        assert ep.realized_pnl_bps == 35_000
        assert ep.fees_paid_bps == 4400


class TestExchangeStatus:
    def test_parse_status_json(self) -> None:
        data = {
            "trading_active": True,
            "exchange_active": True,
        }
        es = ExchangeStatus.model_validate(data)
        assert es.trading_active is True


class TestBalanceBpsFields:
    """Task 3b-Portfolio: Balance bps fields populate from _dollars wire."""

    def test_whole_cents(self) -> None:
        """Wire '5000.00' → balance_bps==50_000_000."""
        data = {
            "balance_dollars": "5000.00",
            "portfolio_value_dollars": "7500.00",
        }
        b = Balance.model_validate(data)
        assert b.balance_bps == 50_000_000
        assert b.portfolio_value_bps == 75_000_000

    def test_subcent_balance_retained_in_bps(self) -> None:
        """Sub-cent balance — bps retains exact precision via aggregate-rounded parser."""
        data = {
            "balance_dollars": "0.0488",
            "portfolio_value_dollars": "0.9512",
        }
        b = Balance.model_validate(data)
        assert b.balance_bps == 488
        assert b.portfolio_value_bps == 9512

    def test_none_wire_field_yields_zero(self) -> None:
        data = {
            "balance_dollars": None,
            "portfolio_value_dollars": None,
        }
        b = Balance.model_validate(data)
        assert b.balance_bps == 0
        assert b.portfolio_value_bps == 0


class TestPositionBpsFields:
    """Task 3b-Portfolio: Position bps/fp100 population from wire."""

    def test_whole_values(self) -> None:
        data = {
            "ticker": "KXBTC-26MAR-T50000",
            "position_fp": "10",
            "total_traded_dollars": "0.25",
            "market_exposure_dollars": "6.50",
            "resting_orders_count": 3,
            "realized_pnl_dollars": "1.00",
            "fees_paid_dollars": "0.44",
        }
        p = Position.model_validate(data)
        assert p.position_fp100 == 1000
        assert p.total_traded_bps == 2500
        assert p.market_exposure_bps == 65_000
        assert p.resting_orders_count == 3
        assert p.realized_pnl_bps == 10_000
        assert p.fees_paid_bps == 4400

    def test_subcent_costs_retained_in_bps(self) -> None:
        """Sub-cent bps retention via aggregate-rounded parser."""
        data = {
            "ticker": "MARJ-MKT",
            "total_traded_dollars": "0.0488",
            "market_exposure_dollars": "0.0150",
            "realized_pnl_dollars": "0.0025",
            "fees_paid_dollars": "0.0075",
        }
        p = Position.model_validate(data)
        assert p.total_traded_bps == 488
        assert p.market_exposure_bps == 150
        assert p.realized_pnl_bps == 25
        assert p.fees_paid_bps == 75

    def test_fractional_position_retained_in_fp100(self) -> None:
        """position_fp='1.89' → position_fp100==189 (exact)."""
        data = {
            "ticker": "MARJ-MKT",
            "position_fp": "1.89",
        }
        p = Position.model_validate(data)
        assert p.position_fp100 == 189

    def test_negative_fractional_position(self) -> None:
        """Negative fractional position retained exactly in fp100."""
        data = {
            "ticker": "MARJ-MKT",
            "position_fp": "-5.50",
        }
        p = Position.model_validate(data)
        assert p.position_fp100 == -550

    def test_zero_defaults_when_wire_fields_absent(self) -> None:
        p = Position.model_validate({"ticker": "MKT-1"})
        assert p.position_fp100 == 0
        assert p.total_traded_bps == 0
        assert p.market_exposure_bps == 0
        assert p.resting_orders_count == 0
        assert p.realized_pnl_bps == 0
        assert p.fees_paid_bps == 0


class TestEventPositionBpsFields:
    """Task 3b-Portfolio: EventPosition bps/fp100 wire population."""

    def test_whole_values(self) -> None:
        data = {
            "event_ticker": "EVT-1",
            "total_cost_dollars": "12.50",
            "total_cost_shares_fp": "25",
            "event_exposure_dollars": "10.00",
            "realized_pnl_dollars": "3.50",
            "fees_paid_dollars": "0.44",
        }
        ep = EventPosition.model_validate(data)
        assert ep.total_cost_bps == 125_000
        assert ep.total_cost_shares_fp100 == 2500
        assert ep.event_exposure_bps == 100_000
        assert ep.realized_pnl_bps == 35_000
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
        assert ep.total_cost_bps == 488
        assert ep.event_exposure_bps == 150
        assert ep.fees_paid_bps == 75
        assert ep.realized_pnl_bps == 25

    def test_fractional_total_cost_shares_retained_in_fp100(self) -> None:
        """total_cost_shares_fp='1.89' → total_cost_shares_fp100==189."""
        data = {
            "event_ticker": "MARJ-EVT",
            "total_cost_shares_fp": "1.89",
        }
        ep = EventPosition.model_validate(data)
        assert ep.total_cost_shares_fp100 == 189

    def test_zero_defaults_when_wire_fields_absent(self) -> None:
        ep = EventPosition.model_validate({"event_ticker": "EVT-1"})
        assert ep.total_cost_bps == 0
        assert ep.total_cost_shares_fp100 == 0
        assert ep.event_exposure_bps == 0
        assert ep.realized_pnl_bps == 0
        assert ep.fees_paid_bps == 0


class TestSettlementBpsFields:
    """Task 3b-Portfolio: Settlement bps/fp100 + mixed-wire special cases."""

    def test_whole_values(self) -> None:
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
        assert s.revenue_bps == 50_000  # cents * 100
        assert s.fee_cost_bps == 770
        assert s.yes_count_fp100 == 1000
        assert s.no_count_fp100 == 0
        assert s.yes_total_cost_bps == 45_000
        assert s.no_total_cost_bps == 0
        assert s.value_bps == 10_000

    def test_revenue_special_case_cents_times_100(self) -> None:
        """revenue is int cents on the wire; revenue_bps == revenue * 100."""
        s = Settlement.model_validate({"ticker": "MKT-1", "revenue": 53})
        assert s.revenue_bps == 5300

    def test_revenue_zero_default(self) -> None:
        s = Settlement.model_validate({"ticker": "MKT-1"})
        assert s.revenue_bps == 0

    def test_fee_cost_from_fee_cost_dollars_path(self) -> None:
        """New FP-variant wire (fee_cost_dollars) populates bps."""
        data = {
            "ticker": "MKT-1",
            "fee_cost_dollars": "0.0770",
        }
        s = Settlement.model_validate(data)
        assert s.fee_cost_bps == 770

    def test_fee_cost_from_legacy_string_path(self) -> None:
        """Legacy string-fee_cost wire populates bps identically."""
        data = {
            "ticker": "MKT-1",
            "fee_cost": "0.0770",
        }
        s = Settlement.model_validate(data)
        assert s.fee_cost_bps == 770

    def test_subcent_value_retained_in_bps(self) -> None:
        """Sub-cent per-contract payout — bps retains exact precision."""
        data = {"ticker": "MKT-1", "value_dollars": "0.0488"}
        s = Settlement.model_validate(data)
        assert s.value_bps == 488

    def test_fractional_yes_count_retained_in_fp100(self) -> None:
        """yes_count_fp='1.89' → yes_count_fp100==189 (exact)."""
        data = {
            "ticker": "MARJ-MKT",
            "yes_count_fp": "1.89",
            "no_count_fp": "0.50",
        }
        s = Settlement.model_validate(data)
        assert s.yes_count_fp100 == 189
        assert s.no_count_fp100 == 50

    def test_zero_defaults_when_wire_fields_absent(self) -> None:
        s = Settlement.model_validate({"ticker": "MKT-1"})
        assert s.revenue_bps == 0
        assert s.fee_cost_bps == 0
        assert s.yes_count_fp100 == 0
        assert s.no_count_fp100 == 0
        assert s.yes_total_cost_bps == 0
        assert s.no_total_cost_bps == 0
        assert s.value_bps is None


class TestPortfolioFieldInvariants:
    """Pin the wire→bps contract for whole-cent values."""

    @pytest.mark.parametrize(
        "wire_dollars,bps",
        [
            ("0.01", 100),
            ("500.00", 5_000_000),
            ("7500.50", 75_005_000),
        ],
    )
    def test_balance_bps_from_wire(self, wire_dollars: str, bps: int) -> None:
        b = Balance.model_validate({
            "balance_dollars": wire_dollars,
            "portfolio_value_dollars": wire_dollars,
        })
        assert b.balance_bps == bps

    @pytest.mark.parametrize(
        "wire_dollars,bps",
        [
            ("0.25", 2_500),
            ("6.50", 65_000),
            ("100.00", 1_000_000),
        ],
    )
    def test_position_total_traded_bps_from_wire(
        self, wire_dollars: str, bps: int
    ) -> None:
        p = Position.model_validate({
            "ticker": "MKT-1",
            "total_traded_dollars": wire_dollars,
        })
        assert p.total_traded_bps == bps

    @pytest.mark.parametrize(
        "wire_dollars,bps",
        [
            ("12.50", 125_000),
            ("0.44", 4_400),
            ("100.00", 1_000_000),
        ],
    )
    def test_event_position_total_cost_bps_from_wire(
        self, wire_dollars: str, bps: int
    ) -> None:
        ep = EventPosition.model_validate({
            "event_ticker": "EVT-1",
            "total_cost_dollars": wire_dollars,
        })
        assert ep.total_cost_bps == bps

    @pytest.mark.parametrize(
        "revenue_cents",
        [1, 53, 500, 10_000],
    )
    def test_settlement_revenue_bps_equals_cents_times_100(
        self, revenue_cents: int
    ) -> None:
        """revenue is already cents on the wire — revenue_bps is cents * 100."""
        s = Settlement.model_validate({"ticker": "MKT-1", "revenue": revenue_cents})
        assert s.revenue_bps == revenue_cents * 100


class TestPortfolioAggregateSubBpsPrecision:
    """Kalshi's /portfolio endpoints emit aggregate money fields with 6-decimal
    precision; aggregate-rounded parser keeps these loading cleanly.
    """

    def test_event_position_accepts_sub_bps_aggregate_payload(self) -> None:
        ep = EventPosition.model_validate({
            "event_ticker": "KXTRUMPSAY-26APR27",
            "event_exposure_dollars": "20.168040",
            "fees_paid_dollars": "0.058000",
            "realized_pnl_dollars": "2.636040",
            "total_cost_dollars": "97.532000",
            "total_cost_shares_fp": "191.59",
        })
        # Half-even rounds: 201680.4 → 201680 ; 26360.4 → 26360.
        assert ep.event_exposure_bps == 201_680
        assert ep.realized_pnl_bps == 26_360
        assert ep.fees_paid_bps == 580
        assert ep.total_cost_bps == 975_320

    def test_position_accepts_sub_bps_aggregate_payload(self) -> None:
        p = Position.model_validate({
            "ticker": "KXTRUMPSAY-26APR27-YES",
            "position_fp": "100.00",
            "total_traded_dollars": "15.432140",
            "market_exposure_dollars": "0.500050",
            "realized_pnl_dollars": "1.234560",
            "fees_paid_dollars": "0.012340",
        })
        assert p.total_traded_bps == 154_321
        assert p.market_exposure_bps == 5_000  # half-even
        assert p.realized_pnl_bps == 12_346
        assert p.fees_paid_bps == 123

    def test_balance_accepts_sub_bps_payload(self) -> None:
        b = Balance.model_validate({
            "balance_dollars": "1234.567890",
            "portfolio_value_dollars": "250.000050",
        })
        assert b.balance_bps == 12_345_679
        assert b.portfolio_value_bps == 2_500_000
