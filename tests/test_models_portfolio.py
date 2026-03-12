"""Tests for portfolio Pydantic models."""

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
