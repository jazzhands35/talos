"""Tests for GameManager and URL parsing."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from talos.game_manager import (
    SCAN_SERIES,
    SPORTS_SERIES,
    GameManager,
    MarketPickerNeeded,
    parse_kalshi_url,
)
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
            series_ticker="KXNHLGAME",
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


class TestSportsBlock:
    """Tests for sports_enabled toggle and YES/NO pair handling."""

    def _make_mock_deps(self) -> tuple[MagicMock, MagicMock, MagicMock]:
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
        feed = MagicMock(spec=MarketFeed)
        feed.subscribe = AsyncMock()
        feed.subscribe_bulk = AsyncMock()
        feed.unsubscribe = AsyncMock()
        scanner = MagicMock(spec=ArbitrageScanner)
        return rest, feed, scanner

    def test_scan_series_alias_matches_sports_series(self) -> None:
        """SCAN_SERIES backward-compatible alias points to SPORTS_SERIES."""
        assert SCAN_SERIES is SPORTS_SERIES

    def test_restore_skips_sports_when_disabled(self) -> None:
        """Sports pairs from cache are skipped when sports_enabled=False."""
        rest, feed, scanner = self._make_mock_deps()
        gm = GameManager(rest, feed, scanner, sports_enabled=False)

        result = gm.restore_game({
            "event_ticker": "KXNHLGAME-26MAR14BOSWSH",
            "ticker_a": "KXNHLGAME-26MAR14BOSWSH-A",
            "ticker_b": "KXNHLGAME-26MAR14BOSWSH-B",
        })
        assert result is None
        assert len(gm.active_games) == 0

    def test_restore_allows_non_sports(self) -> None:
        """Non-sports pairs restore fine when sports disabled."""
        rest, feed, scanner = self._make_mock_deps()
        gm = GameManager(rest, feed, scanner, sports_enabled=False)

        result = gm.restore_game({
            "event_ticker": "SOME-NONSPORT-MKT",
            "ticker_a": "SOME-NONSPORT-MKT",
            "ticker_b": "SOME-NONSPORT-MKT",
            "side_a": "yes",
            "side_b": "no",
            "kalshi_event_ticker": "SOME-NONSPORT-EVT",
        })
        assert result is not None
        assert result.side_a == "yes"
        assert result.side_b == "no"
        assert result.kalshi_event_ticker == "SOME-NONSPORT-EVT"

    def test_restore_allows_sports_when_enabled(self) -> None:
        """Sports pairs restore normally when sports_enabled=True (default)."""
        rest, feed, scanner = self._make_mock_deps()
        gm = GameManager(rest, feed, scanner)  # default sports_enabled=True

        result = gm.restore_game({
            "event_ticker": "KXNHLGAME-26MAR14BOSWSH",
            "ticker_a": "KXNHLGAME-26MAR14BOSWSH-A",
            "ticker_b": "KXNHLGAME-26MAR14BOSWSH-B",
        })
        assert result is not None
        assert result.event_ticker == "KXNHLGAME-26MAR14BOSWSH"

    def test_restore_reads_new_fields_with_defaults(self) -> None:
        """Old cache entries without side_a/side_b/kalshi_event_ticker get defaults."""
        rest, feed, scanner = self._make_mock_deps()
        gm = GameManager(rest, feed, scanner)

        result = gm.restore_game({
            "event_ticker": "EVT-OLD",
            "ticker_a": "TICK-A",
            "ticker_b": "TICK-B",
        })
        assert result is not None
        assert result.side_a == "no"
        assert result.side_b == "no"
        assert result.kalshi_event_ticker == ""

    async def test_add_game_blocks_sports_when_disabled(self) -> None:
        """add_game raises ValueError for sports series when disabled."""
        rest, feed, scanner = self._make_mock_deps()
        gm = GameManager(rest, feed, scanner, sports_enabled=False)

        event = Event(
            event_ticker="KXNHLGAME-26MAR14BOSWSH",
            series_ticker="KXNHLGAME",
            title="Game",
            category="sports",
            status="open",
            markets=[
                Market(ticker="T-A", event_ticker="KXNHLGAME-26MAR14BOSWSH",
                       title="Team A", status="active"),
                Market(ticker="T-B", event_ticker="KXNHLGAME-26MAR14BOSWSH",
                       title="Team B", status="active"),
            ],
        )
        rest.get_event.return_value = event

        with pytest.raises(ValueError, match="Sports markets blocked"):
            await gm.add_game("KXNHLGAME-26MAR14BOSWSH")

    async def test_add_market_as_pair_creates_yes_no(self) -> None:
        """add_market_as_pair creates a same-ticker YES/NO pair."""
        rest, feed, scanner = self._make_mock_deps()
        gm = GameManager(rest, feed, scanner)

        event = Event(
            event_ticker="EVT-1",
            series_ticker="NONSPORT",
            title="Some Event",
            category="politics",
            status="open",
            markets=[],
        )
        market = Market(
            ticker="MKT-1",
            event_ticker="EVT-1",
            title="Will something happen?",
            status="active",
        )

        pair = await gm.add_market_as_pair(event, market)
        assert pair.ticker_a == "MKT-1"
        assert pair.ticker_b == "MKT-1"
        assert pair.side_a == "yes"
        assert pair.side_b == "no"
        assert pair.kalshi_event_ticker == "EVT-1"
        assert pair.is_same_ticker is True
        assert len(gm.active_games) == 1

        # Check labels were built
        labels = gm.leg_labels
        assert "MKT-1" in labels
        assert "YES" in labels["MKT-1"][0]
        assert "NO" in labels["MKT-1"][1]

    async def test_add_game_nonsports_single_market_auto_pairs(self) -> None:
        """Non-sports event with 1 active market auto-creates YES/NO pair."""
        rest, feed, scanner = self._make_mock_deps()
        gm = GameManager(rest, feed, scanner)

        event = Event(
            event_ticker="NONSPORT-EVT",
            series_ticker="NONSPORT",
            title="Some Event",
            category="politics",
            status="open",
            markets=[
                Market(ticker="MKT-1", event_ticker="NONSPORT-EVT",
                       title="Will it happen?", status="active"),
            ],
        )
        rest.get_event.return_value = event

        pair = await gm.add_game("NONSPORT-EVT")
        assert pair.side_a == "yes"
        assert pair.side_b == "no"
        assert pair.ticker_a == pair.ticker_b == "MKT-1"

    async def test_add_game_nonsports_multi_market_raises(self) -> None:
        """Non-sports event with multiple active markets raises for market picker."""
        rest, feed, scanner = self._make_mock_deps()
        gm = GameManager(rest, feed, scanner)

        event = Event(
            event_ticker="NONSPORT-EVT",
            series_ticker="NONSPORT",
            title="Some Event",
            category="politics",
            status="open",
            markets=[
                Market(ticker="MKT-1", event_ticker="NONSPORT-EVT",
                       title="Option A", status="active"),
                Market(ticker="MKT-2", event_ticker="NONSPORT-EVT",
                       title="Option B", status="active"),
                Market(ticker="MKT-3", event_ticker="NONSPORT-EVT",
                       title="Option C", status="active"),
            ],
        )
        rest.get_event.return_value = event

        with pytest.raises(MarketPickerNeeded) as exc_info:
            await gm.add_game("NONSPORT-EVT")
        assert exc_info.value.event is event
        assert len(exc_info.value.markets) == 3


class TestSeriesTicker:
    """Tests for series_ticker field on ArbPair and its population in GameManager."""

    def _make_mock_deps(self) -> tuple[MagicMock, MagicMock, MagicMock]:
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
        feed = MagicMock(spec=MarketFeed)
        feed.subscribe = AsyncMock()
        feed.subscribe_bulk = AsyncMock()
        feed.unsubscribe = AsyncMock()
        scanner = MagicMock(spec=ArbitrageScanner)
        return rest, feed, scanner

    def test_arb_pair_has_series_ticker_field_with_default(self) -> None:
        """ArbPair.series_ticker defaults to empty string."""
        from talos.models.strategy import ArbPair
        pair = ArbPair(event_ticker="EVT-1", ticker_a="A", ticker_b="B")
        assert pair.series_ticker == ""

    def test_arb_pair_series_ticker_can_be_set(self) -> None:
        """ArbPair.series_ticker stores the provided value."""
        from talos.models.strategy import ArbPair
        pair = ArbPair(event_ticker="EVT-1", ticker_a="A", ticker_b="B",
                       series_ticker="KXNHLGAME")
        assert pair.series_ticker == "KXNHLGAME"

    async def test_add_game_sets_series_ticker(self) -> None:
        """add_game() populates series_ticker from event.series_ticker."""
        rest, feed, scanner = self._make_mock_deps()
        gm = GameManager(rest, feed, scanner)

        event = Event(
            event_ticker="KXNHLGAME-26MAR14BOSWSH",
            series_ticker="KXNHLGAME",
            title="BOS vs WSH",
            category="sports",
            status="open",
            markets=[
                Market(ticker="T-A", event_ticker="KXNHLGAME-26MAR14BOSWSH",
                       title="Boston", status="active"),
                Market(ticker="T-B", event_ticker="KXNHLGAME-26MAR14BOSWSH",
                       title="Washington", status="active"),
            ],
        )
        rest.get_event.return_value = event

        pair = await gm.add_game("KXNHLGAME-26MAR14BOSWSH")
        assert pair.series_ticker == "KXNHLGAME"

    async def test_add_market_as_pair_sets_series_ticker(self) -> None:
        """add_market_as_pair() populates series_ticker from event.series_ticker."""
        rest, feed, scanner = self._make_mock_deps()
        gm = GameManager(rest, feed, scanner)

        event = Event(
            event_ticker="NONSPORT-EVT",
            series_ticker="KXNONSPORT",
            title="Some Event",
            category="politics",
            status="open",
            markets=[],
        )
        market = Market(
            ticker="MKT-1",
            event_ticker="NONSPORT-EVT",
            title="Will it happen?",
            status="active",
        )

        pair = await gm.add_market_as_pair(event, market)
        assert pair.series_ticker == "KXNONSPORT"

    def test_restore_game_reads_series_ticker(self) -> None:
        """restore_game() reads series_ticker from persisted data."""
        rest, feed, scanner = self._make_mock_deps()
        gm = GameManager(rest, feed, scanner)

        result = gm.restore_game({
            "event_ticker": "NONSPORT-MKT",
            "ticker_a": "NONSPORT-MKT",
            "ticker_b": "NONSPORT-MKT",
            "side_a": "yes",
            "side_b": "no",
            "kalshi_event_ticker": "NONSPORT-EVT",
            "series_ticker": "KXNONSPORT",
        })
        assert result is not None
        assert result.series_ticker == "KXNONSPORT"

    def test_restore_game_series_ticker_defaults_to_empty(self) -> None:
        """restore_game() defaults series_ticker to '' for old cache entries."""
        rest, feed, scanner = self._make_mock_deps()
        gm = GameManager(rest, feed, scanner)

        result = gm.restore_game({
            "event_ticker": "EVT-OLD",
            "ticker_a": "TICK-A",
            "ticker_b": "TICK-B",
        })
        assert result is not None
        assert result.series_ticker == ""

    async def test_refresh_volumes_uses_series_ticker(self) -> None:
        """refresh_volumes() uses pair.series_ticker when set, not event_ticker prefix."""
        rest, feed, scanner = self._make_mock_deps()
        gm = GameManager(rest, feed, scanner)

        # Simulate a YES/NO pair where event_ticker is a market ticker (no "-" split works)
        # series_ticker is set explicitly to the correct value
        result = gm.restore_game({
            "event_ticker": "KXNONSPORT-MKT",
            "ticker_a": "KXNONSPORT-MKT",
            "ticker_b": "KXNONSPORT-MKT",
            "side_a": "yes",
            "side_b": "no",
            "kalshi_event_ticker": "KXNONSPORT-EVT",
            "series_ticker": "KXNONSPORT",
        })
        assert result is not None

        rest.get_events = AsyncMock(return_value=[])
        await gm.refresh_volumes()

        # Should call get_events with series_ticker="KXNONSPORT" (from pair.series_ticker)
        rest.get_events.assert_called_once_with(
            series_ticker="KXNONSPORT",
            status="open",
            with_nested_markets=True,
            limit=200,
        )

    async def test_refresh_volumes_falls_back_to_event_ticker_split(self) -> None:
        """refresh_volumes() falls back to splitting event_ticker when series_ticker is ''."""
        rest, feed, scanner = self._make_mock_deps()
        gm = GameManager(rest, feed, scanner)

        # Old cache entry without series_ticker
        result = gm.restore_game({
            "event_ticker": "KXNHLGAME-26MAR14BOSWSH",
            "ticker_a": "KXNHLGAME-26MAR14BOSWSH-A",
            "ticker_b": "KXNHLGAME-26MAR14BOSWSH-B",
        })
        assert result is not None

        rest.get_events = AsyncMock(return_value=[])
        await gm.refresh_volumes()

        rest.get_events.assert_called_once_with(
            series_ticker="KXNHLGAME",
            status="open",
            with_nested_markets=True,
            limit=200,
        )
