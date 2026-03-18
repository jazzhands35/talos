"""Tests for game_status — models, protocol, and provider parsing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from talos.game_status import (
    EspnProvider,
    ExternalGame,
    GameStatus,
    GameStatusResolver,
    OddsApiProvider,
    PandaScoreProvider,
    _extract_date_from_ticker,
    estimate_start_time,
)

# ── GameStatus Model ───────────────────────────────────────────────


class TestGameStatus:
    def test_defaults(self) -> None:
        gs = GameStatus(state="unknown")
        assert gs.state == "unknown"
        assert gs.scheduled_start is None
        assert gs.detail == ""

    def test_all_fields(self) -> None:
        t = datetime(2026, 3, 14, 19, 0, tzinfo=UTC)
        gs = GameStatus(state="live", scheduled_start=t, detail="P2 12:12")
        assert gs.state == "live"
        assert gs.scheduled_start == t
        assert gs.detail == "P2 12:12"

    def test_state_values(self) -> None:
        for s in ("pre", "live", "post", "unknown"):
            gs = GameStatus(state=s)
            assert gs.state == s


# ── ExternalGame Model ─────────────────────────────────────────────


class TestExternalGame:
    def test_minimal(self) -> None:
        t = datetime(2026, 3, 14, 19, 0, tzinfo=UTC)
        eg = ExternalGame(
            home_team="Lakers",
            away_team="Celtics",
            scheduled_start=t,
            state="pre",
        )
        assert eg.home_team == "Lakers"
        assert eg.away_team == "Celtics"
        assert eg.home_abbr is None
        assert eg.away_abbr is None
        assert eg.state == "pre"
        assert eg.detail == ""

    def test_full(self) -> None:
        t = datetime(2026, 3, 14, 19, 0, tzinfo=UTC)
        eg = ExternalGame(
            home_team="Lakers",
            away_team="Celtics",
            home_abbr="LAL",
            away_abbr="BOS",
            scheduled_start=t,
            state="live",
            detail="P2 12:12",
        )
        assert eg.home_abbr == "LAL"
        assert eg.away_abbr == "BOS"
        assert eg.detail == "P2 12:12"

    def test_post_state(self) -> None:
        t = datetime(2026, 3, 14, 19, 0, tzinfo=UTC)
        eg = ExternalGame(
            home_team="Lakers",
            away_team="Celtics",
            scheduled_start=t,
            state="post",
            detail="Final",
        )
        assert eg.state == "post"
        assert eg.detail == "Final"


# ── ESPN Provider ──────────────────────────────────────────────────


class TestEspnProvider:
    def test_parse_scheduled_game(self) -> None:
        data = {
            "events": [
                {
                    "date": "2026-03-14T19:00:00Z",
                    "competitions": [
                        {
                            "status": {
                                "type": {"state": "pre"},
                                "displayClock": "0:00",
                                "period": 0,
                            },
                            "competitors": [
                                {
                                    "homeAway": "home",
                                    "team": {
                                        "displayName": "Los Angeles Lakers",
                                        "abbreviation": "LAL",
                                    },
                                },
                                {
                                    "homeAway": "away",
                                    "team": {
                                        "displayName": "Boston Celtics",
                                        "abbreviation": "BOS",
                                    },
                                },
                            ]
                        }
                    ],
                }
            ]
        }
        games = EspnProvider._parse_response(data)
        assert len(games) == 1
        g = games[0]
        assert g.state == "pre"
        assert g.home_team == "Los Angeles Lakers"
        assert g.away_team == "Boston Celtics"
        assert g.home_abbr == "LAL"
        assert g.away_abbr == "BOS"
        assert g.detail == ""

    def test_parse_live_game(self) -> None:
        data = {
            "events": [
                {
                    "date": "2026-03-14T19:00:00Z",
                    "competitions": [
                        {
                            "status": {
                                "type": {"state": "in"},
                                "displayClock": "12:12",
                                "period": 2,
                            },
                            "competitors": [
                                {
                                    "homeAway": "home",
                                    "team": {
                                        "displayName": "Lakers",
                                        "abbreviation": "LAL",
                                    },
                                },
                                {
                                    "homeAway": "away",
                                    "team": {
                                        "displayName": "Celtics",
                                        "abbreviation": "BOS",
                                    },
                                },
                            ]
                        }
                    ],
                }
            ]
        }
        games = EspnProvider._parse_response(data)
        assert len(games) == 1
        g = games[0]
        assert g.state == "live"
        assert g.detail == "P2 12:12"

    def test_parse_post_game(self) -> None:
        data = {
            "events": [
                {
                    "date": "2026-03-14T19:00:00Z",
                    "competitions": [
                        {
                            "status": {
                                "type": {"state": "post"},
                                "displayClock": "0:00",
                                "period": 4,
                            },
                            "competitors": [
                                {
                                    "homeAway": "home",
                                    "team": {
                                        "displayName": "Lakers",
                                        "abbreviation": "LAL",
                                    },
                                },
                                {
                                    "homeAway": "away",
                                    "team": {
                                        "displayName": "Celtics",
                                        "abbreviation": "BOS",
                                    },
                                },
                            ]
                        }
                    ],
                }
            ]
        }
        games = EspnProvider._parse_response(data)
        assert len(games) == 1
        assert games[0].state == "post"

    def test_parse_empty_response(self) -> None:
        games = EspnProvider._parse_response({"events": []})
        assert games == []


# ── OddsAPI Provider ───────────────────────────────────────────────


class TestOddsApiProvider:
    def test_parse_pre_game(self) -> None:
        data = [
            {
                "home_team": "Lakers",
                "away_team": "Celtics",
                "commence_time": "2026-03-14T19:00:00Z",
                "completed": False,
                "scores": None,
            }
        ]
        games = OddsApiProvider._parse_response(data)
        assert len(games) == 1
        g = games[0]
        assert g.state == "pre"
        assert g.home_team == "Lakers"
        assert g.away_team == "Celtics"
        assert g.home_abbr is None
        assert g.away_abbr is None
        assert g.detail == ""

    def test_parse_live_game(self) -> None:
        data = [
            {
                "home_team": "Lakers",
                "away_team": "Celtics",
                "commence_time": "2026-03-14T19:00:00Z",
                "completed": False,
                "scores": [
                    {"name": "Lakers", "score": "102"},
                    {"name": "Celtics", "score": "98"},
                ],
            }
        ]
        games = OddsApiProvider._parse_response(data)
        assert len(games) == 1
        g = games[0]
        assert g.state == "live"
        assert g.detail == "102-98"

    def test_parse_final_game(self) -> None:
        data = [
            {
                "home_team": "Lakers",
                "away_team": "Celtics",
                "commence_time": "2026-03-14T19:00:00Z",
                "completed": True,
                "scores": [
                    {"name": "Lakers", "score": "110"},
                    {"name": "Celtics", "score": "105"},
                ],
            }
        ]
        games = OddsApiProvider._parse_response(data)
        assert len(games) == 1
        g = games[0]
        assert g.state == "post"
        assert g.detail == "110-105"

    def test_parse_empty_response(self) -> None:
        games = OddsApiProvider._parse_response([])
        assert games == []


# ── PandaScore Provider ────────────────────────────────────────────


class TestPandaScoreProvider:
    def test_parse_not_started(self) -> None:
        data = [
            {
                "status": "not_started",
                "scheduled_at": "2026-03-14T19:00:00Z",
                "begin_at": None,
                "number_of_games": 3,
                "games": [],
                "opponents": [
                    {"opponent": {"name": "Team Alpha", "acronym": "ALP"}},
                    {"opponent": {"name": "Team Beta", "acronym": "BET"}},
                ],
            }
        ]
        games = PandaScoreProvider._parse_response(data)
        assert len(games) == 1
        g = games[0]
        assert g.state == "pre"
        assert g.home_team == "Team Alpha"
        assert g.away_team == "Team Beta"
        assert g.home_abbr == "ALP"
        assert g.away_abbr == "BET"
        assert g.detail == ""

    def test_parse_running(self) -> None:
        data = [
            {
                "status": "running",
                "scheduled_at": "2026-03-14T19:00:00Z",
                "begin_at": "2026-03-14T19:05:00Z",
                "number_of_games": 3,
                "games": [
                    {"status": "finished"},
                    {"status": "running"},
                ],
                "opponents": [
                    {"opponent": {"name": "Team Alpha", "acronym": "ALP"}},
                    {"opponent": {"name": "Team Beta", "acronym": "BET"}},
                ],
            }
        ]
        games = PandaScoreProvider._parse_response(data)
        assert len(games) == 1
        g = games[0]
        assert g.state == "live"
        assert g.detail == "2/3"

    def test_begin_at_preferred_over_scheduled_at(self) -> None:
        data = [
            {
                "status": "running",
                "scheduled_at": "2026-03-14T19:00:00Z",
                "begin_at": "2026-03-14T19:05:00Z",
                "number_of_games": 3,
                "games": [{"status": "running"}],
                "opponents": [
                    {"opponent": {"name": "Team Alpha", "acronym": "ALP"}},
                    {"opponent": {"name": "Team Beta", "acronym": "BET"}},
                ],
            }
        ]
        games = PandaScoreProvider._parse_response(data)
        assert len(games) == 1
        g = games[0]
        assert g.scheduled_start == datetime(2026, 3, 14, 19, 5, tzinfo=UTC)

    def test_parse_empty_response(self) -> None:
        games = PandaScoreProvider._parse_response([])
        assert games == []


# ── Team Extraction ───────────────────────────────────────────────


class TestTeamExtraction:
    def test_standard_ticker_with_dashes(self) -> None:
        """Tickers with team codes in separate dash segments."""
        result = GameStatusResolver.extract_team_codes("KXNHLGAME-26MAR14-BOS-NYR")
        assert result == ("BOS", "NYR")

    def test_real_kalshi_ticker(self) -> None:
        """Real Kalshi tickers have teams concatenated — no separate segments."""
        result = GameStatusResolver.extract_team_codes("KXNHLGAME-26MAR14BOSWSH")
        assert result is None  # Only 2 segments, needs subtitle fallback

    def test_short_ticker(self) -> None:
        result = GameStatusResolver.extract_team_codes("KXNHLGAME-26MAR14")
        assert result is None

    def test_non_team_suffix(self) -> None:
        result = GameStatusResolver.extract_team_codes("KXBTC-26MAR-T50000")
        assert result is None


# ── Subtitle Extraction ──────────────────────────────────────────


class TestSubtitleExtraction:
    def test_at_separator(self) -> None:
        result = GameStatusResolver.extract_from_subtitle("WAKE at VT (Mar 10)")
        assert result == ("WAKE", "VT")

    def test_vs_separator(self) -> None:
        result = GameStatusResolver.extract_from_subtitle("T1 vs Gen.G")
        assert result == ("T1", "GEN.G")

    def test_no_separator(self) -> None:
        result = GameStatusResolver.extract_from_subtitle("Something else")
        assert result is None


# ── Game Matching ─────────────────────────────────────────────────


class TestGameMatching:
    def test_abbr_match(self) -> None:
        t = datetime(2026, 3, 14, 19, 0, tzinfo=UTC)
        games = [
            ExternalGame(
                home_team="Los Angeles Lakers",
                away_team="Boston Celtics",
                home_abbr="LAL",
                away_abbr="BOS",
                scheduled_start=t,
                state="pre",
            ),
            ExternalGame(
                home_team="New York Rangers",
                away_team="Montreal Canadiens",
                home_abbr="NYR",
                away_abbr="MTL",
                scheduled_start=t,
                state="pre",
            ),
        ]
        result = GameStatusResolver.match_game(("BOS", "LAL"), games)
        assert result is not None
        assert result.home_team == "Los Angeles Lakers"

    def test_abbr_match_order_independent(self) -> None:
        t = datetime(2026, 3, 14, 19, 0, tzinfo=UTC)
        games = [
            ExternalGame(
                home_team="Los Angeles Lakers",
                away_team="Boston Celtics",
                home_abbr="LAL",
                away_abbr="BOS",
                scheduled_start=t,
                state="pre",
            ),
        ]
        result = GameStatusResolver.match_game(("LAL", "BOS"), games)
        assert result is not None
        assert result.home_team == "Los Angeles Lakers"

    def test_substring_fallback(self) -> None:
        t = datetime(2026, 3, 14, 19, 0, tzinfo=UTC)
        games = [
            ExternalGame(
                home_team="Los Angeles Lakers",
                away_team="Boston Celtics",
                scheduled_start=t,
                state="pre",
            ),
        ]
        result = GameStatusResolver.match_game(("LAKER", "CELTIC"), games)
        assert result is not None
        assert result.home_team == "Los Angeles Lakers"

    def test_no_match(self) -> None:
        t = datetime(2026, 3, 14, 19, 0, tzinfo=UTC)
        games = [
            ExternalGame(
                home_team="Los Angeles Lakers",
                away_team="Boston Celtics",
                home_abbr="LAL",
                away_abbr="BOS",
                scheduled_start=t,
                state="pre",
            ),
        ]
        result = GameStatusResolver.match_game(("NYR", "MTL"), games)
        assert result is None


# ── Date Extraction ───────────────────────────────────────────────


class TestDateExtraction:
    def test_standard(self) -> None:
        result = _extract_date_from_ticker("KXNHLGAME-26MAR14BOSWSH")
        assert result == "20260314"

    def test_single_digit_day(self) -> None:
        result = _extract_date_from_ticker("KXNBAGAME-26MAR5LALGSW")
        assert result == "20260305"

    def test_ahl_with_time(self) -> None:
        """AHL tickers embed a time after the date."""
        result = _extract_date_from_ticker("KXAHLGAME-26MAR141800BRICHA")
        assert result == "20260314"

    def test_no_date(self) -> None:
        result = _extract_date_from_ticker("KXBTC")
        assert result is None

    def test_non_date(self) -> None:
        result = _extract_date_from_ticker("KXBTC-T50000")
        assert result is None


# ── Resolver Integration ─────────────────────────────────────────


class TestResolverIntegration:
    """Integration tests using real Kalshi ticker format + sub_title."""

    _TICKER = "KXNHLGAME-26MAR14BOSNYR"
    _SUB = "BOS at NYR (Mar 14)"

    def _espn_game(self, state: str = "pre", detail: str = "") -> ExternalGame:
        return ExternalGame(
            home_team="New York Rangers",
            away_team="Boston Bruins",
            home_abbr="NYR",
            away_abbr="BOS",
            scheduled_start=datetime(2026, 3, 14, 19, 0, tzinfo=UTC),
            state=state,
            detail=detail,
        )

    @pytest.mark.asyncio
    async def test_resolve_with_subtitle(self) -> None:
        mock_provider = AsyncMock()
        mock_provider.fetch_games.return_value = [self._espn_game()]
        resolver = GameStatusResolver()
        resolver._providers["espn"] = mock_provider

        status = await resolver.resolve(self._TICKER, self._SUB)
        assert status.state == "pre"
        assert status.scheduled_start is not None

    @pytest.mark.asyncio
    async def test_resolve_unmapped_series(self) -> None:
        resolver = GameStatusResolver()
        status = await resolver.resolve("KXFOO-26MAR14AAA")
        assert status.state == "unknown"

    @pytest.mark.asyncio
    async def test_resolve_no_match(self) -> None:
        mock_provider = AsyncMock()
        mock_provider.fetch_games.return_value = []
        resolver = GameStatusResolver()
        resolver._providers["espn"] = mock_provider

        status = await resolver.resolve(self._TICKER, self._SUB)
        assert status.state == "unknown"

    @pytest.mark.asyncio
    async def test_get_cached(self) -> None:
        mock_provider = AsyncMock()
        mock_provider.fetch_games.return_value = [self._espn_game("live", "P2 12:00")]
        resolver = GameStatusResolver()
        resolver._providers["espn"] = mock_provider

        await resolver.resolve(self._TICKER, self._SUB)
        cached = resolver.get(self._TICKER)
        assert cached is not None
        assert cached.state == "live"

    @pytest.mark.asyncio
    async def test_refresh_all_updates_cache(self) -> None:
        mock_provider = AsyncMock()
        mock_provider.fetch_games.return_value = [self._espn_game()]
        resolver = GameStatusResolver()
        resolver._providers["espn"] = mock_provider

        await resolver.resolve(self._TICKER, self._SUB)
        assert resolver.get(self._TICKER).state == "pre"  # type: ignore[union-attr]

        mock_provider.fetch_games.return_value = [self._espn_game("live", "P1 15:00")]
        await resolver.refresh_all()
        cached = resolver.get(self._TICKER)
        assert cached is not None
        assert cached.state == "live"

    @pytest.mark.asyncio
    async def test_refresh_keeps_stale_on_failure(self) -> None:
        mock_provider = AsyncMock()
        mock_provider.fetch_games.return_value = [self._espn_game()]
        resolver = GameStatusResolver()
        resolver._providers["espn"] = mock_provider

        await resolver.resolve(self._TICKER, self._SUB)

        mock_provider.fetch_games.return_value = []
        await resolver.refresh_all()
        assert resolver.get(self._TICKER).state == "pre"  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_remove(self) -> None:
        mock_provider = AsyncMock()
        mock_provider.fetch_games.return_value = [self._espn_game()]
        resolver = GameStatusResolver()
        resolver._providers["espn"] = mock_provider

        await resolver.resolve(self._TICKER, self._SUB)
        assert resolver.get(self._TICKER) is not None

        resolver.remove("KXNHL-26MAR14-BOS-NYR")
        assert resolver.get("KXNHL-26MAR14-BOS-NYR") is None


# ── UI Formatter Tests ───────────────────────────────────────────


from zoneinfo import ZoneInfo

from talos.ui.widgets import _fmt_game_date, _fmt_game_status

PT = ZoneInfo("America/Los_Angeles")


class TestFmtGameDate:
    def test_with_datetime(self) -> None:
        dt = datetime(2026, 3, 14, 20, 0, tzinfo=UTC)
        result = _fmt_game_date(dt)
        assert "03/14" in str(result)

    def test_none(self) -> None:
        result = _fmt_game_date(None)
        assert "\u2014" in str(result)


class TestFmtGameStatus:
    def test_unknown(self) -> None:
        gs = GameStatus(state="unknown")
        result = _fmt_game_status(gs)
        assert "\u2014" in str(result)

    def test_pre_far_out(self) -> None:
        future = datetime.now(UTC) + timedelta(hours=3)
        gs = GameStatus(state="pre", scheduled_start=future)
        result = str(_fmt_game_status(gs))
        assert "M" in result  # AM or PM

    def test_pre_imminent(self) -> None:
        soon = datetime.now(UTC) + timedelta(minutes=10)
        gs = GameStatus(state="pre", scheduled_start=soon)
        result = str(_fmt_game_status(gs))
        assert "in " in result

    def test_live_with_detail(self) -> None:
        gs = GameStatus(state="live", detail="P2 12:34")
        result = str(_fmt_game_status(gs))
        assert "LIVE" in result
        assert "P2" in result

    def test_live_no_detail(self) -> None:
        gs = GameStatus(state="live")
        result = str(_fmt_game_status(gs))
        assert "LIVE" in result

    def test_post(self) -> None:
        gs = GameStatus(state="post")
        result = str(_fmt_game_status(gs))
        assert "FINAL" in result

    def test_none_status(self) -> None:
        result = _fmt_game_status(None)
        assert "\u2014" in str(result)

    def test_estimated_far_out_has_tilde_prefix(self) -> None:
        future = datetime.now(UTC) + timedelta(hours=3)
        gs = GameStatus(state="pre", scheduled_start=future, detail="~est")
        result = str(_fmt_game_status(gs))
        assert result.startswith("~")
        assert "M" in result  # AM or PM

    def test_estimated_imminent_has_tilde_prefix(self) -> None:
        soon = datetime.now(UTC) + timedelta(minutes=10)
        gs = GameStatus(state="pre", scheduled_start=soon, detail="~est")
        result = str(_fmt_game_status(gs))
        assert "~in " in result

    def test_confirmed_time_no_tilde(self) -> None:
        future = datetime.now(UTC) + timedelta(hours=3)
        gs = GameStatus(state="pre", scheduled_start=future, detail="Q1 5:00")
        result = str(_fmt_game_status(gs))
        assert not result.startswith("~")


# ── Expiration Start Time Estimation ──────────────────────────────


class TestEstimateStartTime:
    """Tests for estimate_start_time() pure function."""

    def test_nba_3h_offset(self) -> None:
        result = estimate_start_time("2026-03-19T04:30:00Z", "KXNBAGAME")
        assert result == datetime(2026, 3, 19, 1, 30, tzinfo=UTC)

    def test_ufc_5h_offset(self) -> None:
        result = estimate_start_time("2026-03-22T02:40:00Z", "KXUFCFIGHT")
        assert result == datetime(2026, 3, 21, 21, 40, tzinfo=UTC)

    def test_boxing_5h_offset(self) -> None:
        result = estimate_start_time("2026-04-26T05:00:00Z", "KXBOXING")
        assert result == datetime(2026, 4, 26, 0, 0, tzinfo=UTC)

    def test_midnight_placeholder_returns_none(self) -> None:
        """Boxing placeholder: midnight UTC = no real expiration."""
        result = estimate_start_time("2026-04-12T00:00:00Z", "KXBOXING")
        assert result is None

    def test_none_input_returns_none(self) -> None:
        result = estimate_start_time(None, "KXNHLGAME")
        assert result is None

    def test_empty_string_returns_none(self) -> None:
        result = estimate_start_time("", "KXNHLGAME")
        assert result is None

    def test_unknown_prefix_uses_default_3h(self) -> None:
        result = estimate_start_time("2026-03-22T10:10:00Z", "KXAFLGAME")
        assert result == datetime(2026, 3, 22, 7, 10, tzinfo=UTC)

    def test_invalid_iso_returns_none(self) -> None:
        result = estimate_start_time("not-a-date", "KXNHLGAME")
        assert result is None


class TestResolverExpirationFallback:
    """Tests for expiration-based start time fallback in GameStatusResolver."""

    @pytest.mark.asyncio
    async def test_unmapped_league_with_expiration_gets_estimated_start(self) -> None:
        """CBA game with expected_expiration_time → state='pre' with estimated start."""
        resolver = GameStatusResolver()
        ticker = "KXCBAGAME-26MAR18SHASHAD"
        resolver.set_expiration(ticker, "2026-03-18T14:35:00Z")
        status = await resolver.resolve(ticker)
        assert status.state == "pre"
        assert status.scheduled_start == datetime(2026, 3, 18, 11, 35, tzinfo=UTC)
        assert status.detail == "~est"

    @pytest.mark.asyncio
    async def test_unmapped_league_without_expiration_stays_unknown(self) -> None:
        """Unmapped league without expiration data → state='unknown'."""
        resolver = GameStatusResolver()
        status = await resolver.resolve("KXFOO-26MAR14AAABBB")
        assert status.state == "unknown"

    @pytest.mark.asyncio
    async def test_mapped_league_uses_provider_when_matched(self) -> None:
        """NHL game uses ESPN when provider matches, not expiration fallback."""
        mock_provider = AsyncMock()
        mock_provider.fetch_games.return_value = [
            ExternalGame(
                home_team="Rangers", away_team="Bruins",
                home_abbr="NYR", away_abbr="BOS",
                scheduled_start=datetime(2026, 3, 14, 19, 0, tzinfo=UTC),
                state="live", detail="P2 5:00",
            )
        ]
        resolver = GameStatusResolver()
        resolver._providers["espn"] = mock_provider
        ticker = "KXNHLGAME-26MAR14BOSNYR"
        resolver.set_expiration(ticker, "2026-03-14T22:00:00Z")
        status = await resolver.resolve(ticker, "BOS at NYR (Mar 14)")
        assert status.state == "live"  # From ESPN, not fallback

    @pytest.mark.asyncio
    async def test_mapped_league_falls_back_when_no_match(self) -> None:
        """AHL game with no provider match uses expiration fallback."""
        mock_provider = AsyncMock()
        mock_provider.fetch_games.return_value = []  # No games returned
        resolver = GameStatusResolver()
        resolver._providers["odds-api"] = mock_provider
        ticker = "KXAHLGAME-26MAR18COASAN"
        resolver.set_expiration(ticker, "2026-03-18T22:00:00Z")
        status = await resolver.resolve(ticker, "COA at SAN (Mar 18)")
        assert status.state == "pre"
        assert status.scheduled_start == datetime(2026, 3, 18, 19, 0, tzinfo=UTC)
        assert status.detail == "~est"

    @pytest.mark.asyncio
    async def test_mapped_league_falls_back_on_fetch_error(self) -> None:
        """Provider throws → expiration fallback activates."""
        mock_provider = AsyncMock()
        mock_provider.fetch_games.side_effect = Exception("API down")
        resolver = GameStatusResolver()
        resolver._providers["odds-api"] = mock_provider
        ticker = "KXAHLGAME-26MAR18COASAN"
        resolver.set_expiration(ticker, "2026-03-18T22:00:00Z")
        status = await resolver.resolve(ticker, "COA at SAN (Mar 18)")
        assert status.state == "pre"
        assert status.detail == "~est"

    @pytest.mark.asyncio
    async def test_expiration_removed_on_game_remove(self) -> None:
        resolver = GameStatusResolver()
        ticker = "KXCBAGAME-26MAR18SHASHAD"
        resolver.set_expiration(ticker, "2026-03-18T14:35:00Z")
        resolver.remove(ticker)
        assert ticker not in resolver._expirations

    @pytest.mark.asyncio
    async def test_midnight_placeholder_falls_through_to_unknown(self) -> None:
        """Boxing midnight placeholder → fallback returns None → state='unknown'."""
        resolver = GameStatusResolver()
        ticker = "KXBOXING-26APR12FURYMAKH"
        resolver.set_expiration(ticker, "2026-04-12T00:00:00Z")
        status = await resolver.resolve(ticker)
        assert status.state == "unknown"

    @pytest.mark.asyncio
    async def test_cached_fallback_returned_by_get(self) -> None:
        resolver = GameStatusResolver()
        ticker = "KXAFLGAME-26MAR22NMKWCE"
        resolver.set_expiration(ticker, "2026-03-22T10:10:00Z")
        await resolver.resolve(ticker)
        cached = resolver.get(ticker)
        assert cached is not None
        assert cached.state == "pre"
        assert cached.detail == "~est"
