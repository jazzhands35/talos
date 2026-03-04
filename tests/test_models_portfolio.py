"""Tests for portfolio Pydantic models."""

from talos.models.portfolio import Balance, ExchangeStatus, Position, Settlement


class TestBalance:
    def test_parse_balance_json(self) -> None:
        data = {
            "balance": 500000,
            "portfolio_value": 750000,
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
    def test_parse_settlement_json(self) -> None:
        data = {
            "ticker": "KXBTC-26MAR-T50000",
            "settlement_price": 100,
            "payout": 1000,
            "settled_time": "2026-03-26T12:00:00Z",
        }
        s = Settlement.model_validate(data)
        assert s.settlement_price == 100
        assert s.payout == 1000


class TestExchangeStatus:
    def test_parse_status_json(self) -> None:
        data = {
            "trading_active": True,
            "exchange_active": True,
        }
        es = ExchangeStatus.model_validate(data)
        assert es.trading_active is True
