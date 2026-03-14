"""Tests for game_status — models, protocol, and provider parsing."""

from __future__ import annotations

from datetime import UTC, datetime
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
    def test_standard_ticker(self) -> None:
        result = GameStatusResolver.extract_team_codes("KXNHL-26MAR14-BOS-NYR")
        assert result == ("BOS", "NYR")

    def test_short_ticker(self) -> None:
        result = GameStatusResolver.extract_team_codes("KXNHL-26MAR14")
        assert result is None

    def test_non_team_suffix(self) -> None:
        result = GameStatusResolver.extract_team_codes("KXBTC-26MAR-T50000")
        assert result is None

    def test_three_letter_codes(self) -> None:
        result = GameStatusResolver.extract_team_codes("KXNBA-26MAR14-LAL-GSW")
        assert result == ("LAL", "GSW")

    def test_two_letter_codes(self) -> None:
        result = GameStatusResolver.extract_team_codes("KXCBB-26MAR14-VT-UK")
        assert result == ("VT", "UK")


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
        result = _extract_date_from_ticker("KXNHL-26MAR14-BOS-NYR")
        assert result == "20260314"

    def test_single_digit_day(self) -> None:
        result = _extract_date_from_ticker("KXNBA-26MAR5-LAL-GSW")
        assert result == "20260305"

    def test_no_date(self) -> None:
        result = _extract_date_from_ticker("KXBTC")
        assert result is None

    def test_non_date(self) -> None:
        result = _extract_date_from_ticker("KXBTC-T50000")
        assert result is None


# ── Resolver Integration ─────────────────────────────────────────


class TestResolverIntegration:
    @pytest.mark.asyncio
    async def test_resolve_espn_game(self) -> None:
        t = datetime(2026, 3, 14, 19, 0, tzinfo=UTC)
        mock_provider = AsyncMock()
        mock_provider.fetch_games.return_value = [
            ExternalGame(
                home_team="Boston Bruins",
                away_team="New York Rangers",
                home_abbr="BOS",
                away_abbr="NYR",
                scheduled_start=t,
                state="pre",
            ),
        ]
        resolver = GameStatusResolver()
        resolver._providers["espn"] = mock_provider

        status = await resolver.resolve("KXNHL-26MAR14-BOS-NYR")
        assert status.state == "pre"
        assert status.scheduled_start == t

    @pytest.mark.asyncio
    async def test_resolve_unmapped_series(self) -> None:
        resolver = GameStatusResolver()
        status = await resolver.resolve("KXFOO-26MAR14-AAA-BBB")
        assert status.state == "unknown"

    @pytest.mark.asyncio
    async def test_resolve_no_match(self) -> None:
        mock_provider = AsyncMock()
        mock_provider.fetch_games.return_value = []
        resolver = GameStatusResolver()
        resolver._providers["espn"] = mock_provider

        status = await resolver.resolve("KXNHL-26MAR14-BOS-NYR")
        assert status.state == "unknown"

    @pytest.mark.asyncio
    async def test_get_cached(self) -> None:
        t = datetime(2026, 3, 14, 19, 0, tzinfo=UTC)
        mock_provider = AsyncMock()
        mock_provider.fetch_games.return_value = [
            ExternalGame(
                home_team="Boston Bruins",
                away_team="New York Rangers",
                home_abbr="BOS",
                away_abbr="NYR",
                scheduled_start=t,
                state="pre",
            ),
        ]
        resolver = GameStatusResolver()
        resolver._providers["espn"] = mock_provider

        await resolver.resolve("KXNHL-26MAR14-BOS-NYR")
        cached = resolver.get("KXNHL-26MAR14-BOS-NYR")
        assert cached is not None
        assert cached.state == "pre"

    @pytest.mark.asyncio
    async def test_refresh_all_updates_cache(self) -> None:
        t = datetime(2026, 3, 14, 19, 0, tzinfo=UTC)
        pre_game = ExternalGame(
            home_team="Boston Bruins",
            away_team="New York Rangers",
            home_abbr="BOS",
            away_abbr="NYR",
            scheduled_start=t,
            state="pre",
        )
        live_game = ExternalGame(
            home_team="Boston Bruins",
            away_team="New York Rangers",
            home_abbr="BOS",
            away_abbr="NYR",
            scheduled_start=t,
            state="live",
            detail="P1 15:00",
        )
        mock_provider = AsyncMock()
        mock_provider.fetch_games.return_value = [pre_game]
        resolver = GameStatusResolver()
        resolver._providers["espn"] = mock_provider

        await resolver.resolve("KXNHL-26MAR14-BOS-NYR")
        assert resolver.get("KXNHL-26MAR14-BOS-NYR") is not None
        assert resolver.get("KXNHL-26MAR14-BOS-NYR").state == "pre"  # type: ignore[union-attr]

        # Now refresh returns live game
        mock_provider.fetch_games.return_value = [live_game]
        await resolver.refresh_all()
        cached = resolver.get("KXNHL-26MAR14-BOS-NYR")
        assert cached is not None
        assert cached.state == "live"
        assert cached.detail == "P1 15:00"

    @pytest.mark.asyncio
    async def test_refresh_keeps_stale_on_failure(self) -> None:
        t = datetime(2026, 3, 14, 19, 0, tzinfo=UTC)
        mock_provider = AsyncMock()
        mock_provider.fetch_games.return_value = [
            ExternalGame(
                home_team="Boston Bruins",
                away_team="New York Rangers",
                home_abbr="BOS",
                away_abbr="NYR",
                scheduled_start=t,
                state="pre",
            ),
        ]
        resolver = GameStatusResolver()
        resolver._providers["espn"] = mock_provider

        await resolver.resolve("KXNHL-26MAR14-BOS-NYR")

        # Refresh returns empty list -> keep stale
        mock_provider.fetch_games.return_value = []
        await resolver.refresh_all()
        cached = resolver.get("KXNHL-26MAR14-BOS-NYR")
        assert cached is not None
        assert cached.state == "pre"

    @pytest.mark.asyncio
    async def test_remove(self) -> None:
        t = datetime(2026, 3, 14, 19, 0, tzinfo=UTC)
        mock_provider = AsyncMock()
        mock_provider.fetch_games.return_value = [
            ExternalGame(
                home_team="Boston Bruins",
                away_team="New York Rangers",
                home_abbr="BOS",
                away_abbr="NYR",
                scheduled_start=t,
                state="pre",
            ),
        ]
        resolver = GameStatusResolver()
        resolver._providers["espn"] = mock_provider

        await resolver.resolve("KXNHL-26MAR14-BOS-NYR")
        assert resolver.get("KXNHL-26MAR14-BOS-NYR") is not None

        resolver.remove("KXNHL-26MAR14-BOS-NYR")
        assert resolver.get("KXNHL-26MAR14-BOS-NYR") is None


# ── UI Formatter Tests ───────────────────────────────────────────


from datetime import timedelta
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
