"""Tests for GameManager and URL parsing."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from talos.game_manager import GameManager, parse_kalshi_url
from talos.market_feed import MarketFeed
from talos.models.market import Event, Market, Series
from talos.rest_client import KalshiRESTClient
from talos.scanner import ArbitrageScanner


class TestParseKalshiUrl:
    def test_parses_full_url(self) -> None:
        url = "https://kalshi.com/markets/kxncaawbgame/college-basketball-womens-game/kxncaawbgame-26mar04stanmia"
        assert parse_kalshi_url(url) == "KXNCAAWBGAME-26MAR04STANMIA"

    def test_parses_url_with_trailing_slash(self) -> None:
        url = "https://kalshi.com/markets/kxncaawbgame/college-basketball-womens-game/kxncaawbgame-26mar04stanmia/"
        assert parse_kalshi_url(url) == "KXNCAAWBGAME-26MAR04STANMIA"

    def test_parses_bare_ticker(self) -> None:
        assert parse_kalshi_url("kxncaawbgame-26mar04stanmia") == "KXNCAAWBGAME-26MAR04STANMIA"

    def test_accepts_uppercase_ticker(self) -> None:
        assert parse_kalshi_url("KXNCAAWBGAME-26MAR04STANMIA") == "KXNCAAWBGAME-26MAR04STANMIA"

    def test_rejects_empty_string(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            parse_kalshi_url("")

    def test_rejects_non_kalshi_url(self) -> None:
        with pytest.raises(ValueError, match="Kalshi"):
            parse_kalshi_url("https://example.com/markets/foo")


class TestGameManager:
    @pytest.fixture()
    def mock_rest(self) -> KalshiRESTClient:
        rest = MagicMock(spec=KalshiRESTClient)
        rest.get_event = AsyncMock()
        rest.get_series = AsyncMock(
            return_value=Series(
                series_ticker="SER-1",
                title="Test Series",
                category="sports",
                fee_type="quadratic_with_maker_fees",
                fee_multiplier=0.0175,
            )
        )
        return rest

    @pytest.fixture()
    def mock_feed(self) -> MarketFeed:
        feed = MagicMock(spec=MarketFeed)
        feed.subscribe = AsyncMock()
        feed.subscribe_bulk = AsyncMock()
        feed.unsubscribe = AsyncMock()
        return feed

    @pytest.fixture()
    def mock_scanner(self) -> ArbitrageScanner:
        scanner = MagicMock(spec=ArbitrageScanner)
        return scanner

    @pytest.fixture()
    def manager(
        self,
        mock_rest: KalshiRESTClient,
        mock_feed: MarketFeed,
        mock_scanner: ArbitrageScanner,
    ) -> GameManager:
        return GameManager(rest=mock_rest, feed=mock_feed, scanner=mock_scanner)

    def _make_event(self, event_ticker: str, tickers: list[str]) -> Event:
        markets = [
            Market(ticker=t, event_ticker=event_ticker, title=f"Team {i}", status="active")
            for i, t in enumerate(tickers)
        ]
        return Event(
            event_ticker=event_ticker,
            series_ticker="SER-1",
            title="Game",
            category="sports",
            status="open",
            markets=markets,
        )

    async def test_add_game_fetches_event(
        self,
        manager: GameManager,
        mock_rest: KalshiRESTClient,
    ) -> None:
        event = self._make_event("EVT-1", ["TICK-A", "TICK-B"])
        mock_rest.get_event.return_value = event  # type: ignore[union-attr]
        await manager.add_game("EVT-1")
        mock_rest.get_event.assert_called_once_with("EVT-1", with_nested_markets=True)  # type: ignore[union-attr]

    async def test_add_game_registers_pair(
        self,
        manager: GameManager,
        mock_rest: KalshiRESTClient,
        mock_scanner: ArbitrageScanner,
    ) -> None:
        event = self._make_event("EVT-1", ["TICK-A", "TICK-B"])
        mock_rest.get_event.return_value = event  # type: ignore[union-attr]
        await manager.add_game("EVT-1")
        mock_scanner.add_pair.assert_called_once_with(  # type: ignore[union-attr]
            "EVT-1",
            "TICK-A",
            "TICK-B",
            fee_type="quadratic_with_maker_fees",
            fee_rate=0.0175,
            close_time=None,
            expected_expiration_time=None,
        )

    async def test_add_game_subscribes_both_tickers(
        self,
        manager: GameManager,
        mock_rest: KalshiRESTClient,
        mock_feed: MarketFeed,
    ) -> None:
        event = self._make_event("EVT-1", ["TICK-A", "TICK-B"])
        mock_rest.get_event.return_value = event  # type: ignore[union-attr]
        await manager.add_game("EVT-1")
        assert mock_feed.subscribe.call_count == 2  # type: ignore[union-attr]
        mock_feed.subscribe.assert_any_call("TICK-A")  # type: ignore[union-attr]
        mock_feed.subscribe.assert_any_call("TICK-B")  # type: ignore[union-attr]

    async def test_add_game_rejects_non_binary_event(
        self,
        manager: GameManager,
        mock_rest: KalshiRESTClient,
    ) -> None:
        event = self._make_event("EVT-1", ["A", "B", "C"])
        mock_rest.get_event.return_value = event  # type: ignore[union-attr]
        with pytest.raises(ValueError, match="exactly 2"):
            await manager.add_game("EVT-1")

    async def test_add_game_returns_pair(
        self,
        manager: GameManager,
        mock_rest: KalshiRESTClient,
    ) -> None:
        event = self._make_event("EVT-1", ["TICK-A", "TICK-B"])
        mock_rest.get_event.return_value = event  # type: ignore[union-attr]
        pair = await manager.add_game("EVT-1")
        assert pair.event_ticker == "EVT-1"
        assert pair.ticker_a == "TICK-A"
        assert pair.ticker_b == "TICK-B"

    async def test_add_game_from_url(
        self,
        manager: GameManager,
        mock_rest: KalshiRESTClient,
    ) -> None:
        event = self._make_event("KXNCAAWBGAME-26MAR04STANMIA", ["STAN", "MIA"])
        mock_rest.get_event.return_value = event  # type: ignore[union-attr]
        url = "https://kalshi.com/markets/kxncaawbgame/college-basketball-womens-game/kxncaawbgame-26mar04stanmia"
        pair = await manager.add_game(url)
        assert pair.event_ticker == "KXNCAAWBGAME-26MAR04STANMIA"

    async def test_add_games_multiple(
        self,
        manager: GameManager,
        mock_rest: KalshiRESTClient,
        mock_feed: MarketFeed,
    ) -> None:
        mock_rest.get_event.side_effect = [  # type: ignore[union-attr]
            self._make_event("EVT-1", ["A1", "B1"]),
            self._make_event("EVT-2", ["A2", "B2"]),
        ]
        pairs = await manager.add_games(["EVT-1", "EVT-2"])
        assert len(pairs) == 2
        # Individual subscribes should be skipped
        mock_feed.subscribe.assert_not_called()  # type: ignore[union-attr]
        # Single bulk subscribe with all 4 tickers
        mock_feed.subscribe_bulk.assert_called_once()  # type: ignore[union-attr]
        bulk_tickers = mock_feed.subscribe_bulk.call_args[0][0]  # type: ignore[union-attr]
        assert set(bulk_tickers) == {"A1", "B1", "A2", "B2"}

    async def test_remove_game(
        self,
        manager: GameManager,
        mock_rest: KalshiRESTClient,
        mock_feed: MarketFeed,
        mock_scanner: ArbitrageScanner,
    ) -> None:
        event = self._make_event("EVT-1", ["TICK-A", "TICK-B"])
        mock_rest.get_event.return_value = event  # type: ignore[union-attr]
        await manager.add_game("EVT-1")
        await manager.remove_game("EVT-1")
        mock_scanner.remove_pair.assert_called_once_with("EVT-1")  # type: ignore[union-attr]
        assert mock_feed.unsubscribe.call_count == 2  # type: ignore[union-attr]

    async def test_duplicate_add_game_is_noop(
        self,
        manager: GameManager,
        mock_rest: KalshiRESTClient,
    ) -> None:
        event = self._make_event("EVT-1", ["TICK-A", "TICK-B"])
        mock_rest.get_event.return_value = event  # type: ignore[union-attr]
        pair1 = await manager.add_game("EVT-1")
        pair2 = await manager.add_game("EVT-1")
        assert pair1 == pair2
        mock_rest.get_event.assert_called_once()  # type: ignore[union-attr]

    async def test_active_games(
        self,
        manager: GameManager,
        mock_rest: KalshiRESTClient,
    ) -> None:
        event = self._make_event("EVT-1", ["TICK-A", "TICK-B"])
        mock_rest.get_event.return_value = event  # type: ignore[union-attr]
        await manager.add_game("EVT-1")
        assert len(manager.active_games) == 1
        assert manager.active_games[0].event_ticker == "EVT-1"

    async def test_clear_all_games(
        self,
        manager: GameManager,
        mock_rest: KalshiRESTClient,
        mock_feed: MarketFeed,
        mock_scanner: ArbitrageScanner,
    ) -> None:
        mock_rest.get_event.side_effect = [  # type: ignore[union-attr]
            self._make_event("EVT-1", ["A1", "B1"]),
            self._make_event("EVT-2", ["A2", "B2"]),
        ]
        await manager.add_game("EVT-1")
        await manager.add_game("EVT-2")
        assert len(manager.active_games) == 2

        await manager.clear_all_games()
        assert len(manager.active_games) == 0
        assert mock_scanner.remove_pair.call_count == 2  # type: ignore[union-attr]
        assert mock_feed.unsubscribe.call_count == 4  # type: ignore[union-attr]

    async def test_on_change_fires_on_add(
        self,
        manager: GameManager,
        mock_rest: KalshiRESTClient,
    ) -> None:
        callback = Mock()
        manager.on_change = callback
        event = self._make_event("EVT-1", ["TICK-A", "TICK-B"])
        mock_rest.get_event.return_value = event  # type: ignore[union-attr]
        await manager.add_game("EVT-1")
        callback.assert_called_once()

    async def test_on_change_fires_on_remove(
        self,
        manager: GameManager,
        mock_rest: KalshiRESTClient,
    ) -> None:
        event = self._make_event("EVT-1", ["TICK-A", "TICK-B"])
        mock_rest.get_event.return_value = event  # type: ignore[union-attr]
        await manager.add_game("EVT-1")
        callback = Mock()
        manager.on_change = callback
        await manager.remove_game("EVT-1")
        callback.assert_called_once()

    async def test_on_change_fires_once_on_clear(
        self,
        manager: GameManager,
        mock_rest: KalshiRESTClient,
    ) -> None:
        mock_rest.get_event.side_effect = [  # type: ignore[union-attr]
            self._make_event("EVT-1", ["A1", "B1"]),
            self._make_event("EVT-2", ["A2", "B2"]),
        ]
        await manager.add_game("EVT-1")
        await manager.add_game("EVT-2")
        callback = Mock()
        manager.on_change = callback
        await manager.clear_all_games()
        callback.assert_called_once()

    async def test_on_change_not_fired_on_duplicate_add(
        self,
        manager: GameManager,
        mock_rest: KalshiRESTClient,
    ) -> None:
        event = self._make_event("EVT-1", ["TICK-A", "TICK-B"])
        mock_rest.get_event.return_value = event  # type: ignore[union-attr]
        await manager.add_game("EVT-1")
        callback = Mock()
        manager.on_change = callback
        await manager.add_game("EVT-1")
        callback.assert_not_called()
