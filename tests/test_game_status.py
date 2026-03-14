"""Tests for game_status — models, protocol, and provider parsing."""

from __future__ import annotations

from datetime import UTC, datetime

from talos.game_status import (
    EspnProvider,
    ExternalGame,
    GameStatus,
    OddsApiProvider,
    PandaScoreProvider,
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
