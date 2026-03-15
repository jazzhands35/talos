"""Tests for GameManager.scan_events() scanner integration."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from talos.game_manager import SCAN_SERIES, GameManager
from talos.models.market import Event, Market
from talos.scanner import ArbitrageScanner


def _make_market(ticker: str, *, status: str = "open") -> Market:
    return Market(
        ticker=ticker,
        event_ticker="EVT-1",
        title=f"Market {ticker}",
        status=status,
    )


def _make_event(
    event_ticker: str,
    *,
    num_markets: int = 2,
    market_status: str = "active",
) -> Event:
    markets = [
        _make_market(f"{event_ticker}-M{i}", status=market_status)
        for i in range(num_markets)
    ]
    return Event(
        event_ticker=event_ticker,
        series_ticker="KXNBAGAME",
        title=f"Event {event_ticker}",
        category="sports",
        markets=markets,
    )


class TestScanSeries:
    def test_scan_series_contains_known_series(self) -> None:
        assert "KXNBAGAME" in SCAN_SERIES
        assert "KXNHLGAME" in SCAN_SERIES
        assert "KXATPMATCH" in SCAN_SERIES

    def test_scan_series_no_duplicates(self) -> None:
        assert len(SCAN_SERIES) == len(set(SCAN_SERIES))


class TestScanEvents:
    @pytest.fixture()
    def gm(self) -> GameManager:
        rest = MagicMock()
        rest.get_events = AsyncMock(return_value=[])
        feed = AsyncMock()
        scanner = MagicMock(spec=ArbitrageScanner)
        return GameManager(rest=rest, feed=feed, scanner=scanner)

    @pytest.mark.asyncio()
    async def test_scan_returns_two_market_events(self, gm: GameManager) -> None:
        two_mkt = _make_event("EVT-2MKT", num_markets=2)
        three_mkt = _make_event("EVT-3MKT", num_markets=3)

        gm._rest.get_events = AsyncMock(return_value=[two_mkt, three_mkt])

        result = await gm.scan_events()
        tickers = [e.event_ticker for e in result]
        assert "EVT-2MKT" in tickers
        assert "EVT-3MKT" not in tickers

    @pytest.mark.asyncio()
    async def test_scan_excludes_already_monitored(self, gm: GameManager) -> None:
        monitored = _make_event("EVT-MON")
        new_event = _make_event("EVT-NEW")

        # Pre-populate _games to simulate an already-monitored event
        from talos.models.strategy import ArbPair

        gm._games["EVT-MON"] = ArbPair(
            event_ticker="EVT-MON",
            ticker_a="EVT-MON-M0",
            ticker_b="EVT-MON-M1",
        )

        gm._rest.get_events = AsyncMock(return_value=[monitored, new_event])

        result = await gm.scan_events()
        tickers = [e.event_ticker for e in result]
        assert "EVT-MON" not in tickers
        assert "EVT-NEW" in tickers

    @pytest.mark.asyncio()
    async def test_scan_handles_api_failure(self, gm: GameManager) -> None:
        gm._rest.get_events = AsyncMock(side_effect=RuntimeError("API down"))

        result = await gm.scan_events()
        assert result == []

    @pytest.mark.asyncio()
    async def test_scan_excludes_settled_events(self, gm: GameManager) -> None:
        settled = _make_event("EVT-SETTLED", market_status="settled")
        determined = _make_event("EVT-DET", market_status="determined")
        active = _make_event("EVT-ACTIVE", market_status="active")

        gm._rest.get_events = AsyncMock(
            return_value=[settled, determined, active],
        )

        result = await gm.scan_events()
        tickers = [e.event_ticker for e in result]
        assert "EVT-SETTLED" not in tickers
        assert "EVT-DET" not in tickers
        assert "EVT-ACTIVE" in tickers
