# Game Status Provider Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the "Closes" column with Date + Status columns showing live game state from ESPN, The Odds API, and PandaScore.

**Architecture:** Single module `game_status.py` containing Pydantic models (`GameStatus`, `ExternalGame`), three provider classes (ESPN, OddsApi, PandaScore), and a `GameStatusResolver` that maps Kalshi series tickers to providers, matches games by team codes, and caches results. The resolver is instantiated in `__main__.py`, passed to the engine, and read by the UI every 0.5s.

**Tech Stack:** Python 3.12+, httpx (async HTTP), Pydantic v2, structlog, pytest + pytest-asyncio

**Spec:** `docs/plans/2026-03-14-game-status-design.md`

---

## Task 1: Models and Provider Protocol

**Files:**
- Create: `src/talos/game_status.py`
- Create: `tests/test_game_status.py`

- [ ] **Step 1: Write failing tests for GameStatus and ExternalGame models**

```python
# tests/test_game_status.py
"""Tests for game status provider system."""

from datetime import UTC, datetime

from talos.game_status import ExternalGame, GameStatus


class TestGameStatus:
    def test_defaults(self) -> None:
        gs = GameStatus(state="unknown")
        assert gs.state == "unknown"
        assert gs.scheduled_start is None
        assert gs.detail == ""

    def test_with_all_fields(self) -> None:
        dt = datetime(2026, 3, 14, 20, 0, tzinfo=UTC)
        gs = GameStatus(state="pre", scheduled_start=dt, detail="7:00 PM")
        assert gs.state == "pre"
        assert gs.scheduled_start == dt


class TestExternalGame:
    def test_minimal(self) -> None:
        dt = datetime(2026, 3, 14, 20, 0, tzinfo=UTC)
        g = ExternalGame(
            home_team="Boston Bruins",
            away_team="New York Rangers",
            scheduled_start=dt,
            state="pre",
        )
        assert g.home_abbr is None
        assert g.detail == ""

    def test_with_abbreviations(self) -> None:
        dt = datetime(2026, 3, 14, 20, 0, tzinfo=UTC)
        g = ExternalGame(
            home_team="Boston Bruins",
            away_team="New York Rangers",
            home_abbr="BOS",
            away_abbr="NYR",
            scheduled_start=dt,
            state="live",
            detail="P2 12:34",
        )
        assert g.home_abbr == "BOS"
        assert g.state == "live"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_game_status.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'talos.game_status'`

- [ ] **Step 3: Create game_status.py with models and provider protocol**

```python
# src/talos/game_status.py
"""Multi-source game status provider for live sporting event detection."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pydantic import BaseModel


class GameStatus(BaseModel):
    """Cached game state for a Kalshi event."""

    state: str  # "pre" | "live" | "post" | "unknown"
    scheduled_start: datetime | None = None
    detail: str = ""


class ExternalGame(BaseModel):
    """Normalized game from an external sports data API."""

    home_team: str
    away_team: str
    home_abbr: str | None = None
    away_abbr: str | None = None
    scheduled_start: datetime
    state: str  # "pre" | "live" | "post"
    detail: str = ""


class GameStatusProvider(Protocol):
    """Protocol for external sports data sources."""

    async def fetch_games(
        self, sport: str, league: str, game_date: str
    ) -> list[ExternalGame]: ...
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_game_status.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add src/talos/game_status.py tests/test_game_status.py
git commit -m "feat(game-status): add GameStatus, ExternalGame models and provider protocol"
```

---

## Task 2: ESPN Provider

**Files:**
- Modify: `src/talos/game_status.py`
- Modify: `tests/test_game_status.py`

- [ ] **Step 1: Write failing test for ESPN response parsing**

Add to `tests/test_game_status.py`:

```python
import pytest

from talos.game_status import EspnProvider

# Minimal ESPN scoreboard response for one game
ESPN_RESPONSE = {
    "events": [
        {
            "date": "2026-03-14T20:00Z",
            "competitions": [
                {
                    "status": {
                        "clock": 0.0,
                        "displayClock": "0:00",
                        "period": 0,
                        "type": {
                            "id": "1",
                            "name": "STATUS_SCHEDULED",
                            "state": "pre",
                            "completed": False,
                            "description": "Scheduled",
                            "detail": "Sat, March 14th at 4:00 PM EDT",
                            "shortDetail": "3/14 - 4:00 PM EDT",
                        },
                    },
                    "competitors": [
                        {
                            "homeAway": "home",
                            "team": {
                                "displayName": "Boston Bruins",
                                "abbreviation": "BOS",
                            },
                        },
                        {
                            "homeAway": "away",
                            "team": {
                                "displayName": "New York Rangers",
                                "abbreviation": "NYR",
                            },
                        },
                    ],
                }
            ],
        }
    ]
}

ESPN_LIVE_RESPONSE = {
    "events": [
        {
            "date": "2026-03-14T20:00Z",
            "competitions": [
                {
                    "status": {
                        "clock": 732.0,
                        "displayClock": "12:12",
                        "period": 2,
                        "type": {
                            "id": "2",
                            "name": "STATUS_IN_PROGRESS",
                            "state": "in",
                            "completed": False,
                            "description": "In Progress",
                            "detail": "2nd Period - 12:12",
                            "shortDetail": "2nd - 12:12",
                        },
                    },
                    "competitors": [
                        {
                            "homeAway": "home",
                            "team": {
                                "displayName": "Boston Bruins",
                                "abbreviation": "BOS",
                            },
                        },
                        {
                            "homeAway": "away",
                            "team": {
                                "displayName": "New York Rangers",
                                "abbreviation": "NYR",
                            },
                        },
                    ],
                }
            ],
        }
    ]
}


class TestEspnProvider:
    def test_parse_scheduled_game(self) -> None:
        games = EspnProvider._parse_response(ESPN_RESPONSE)
        assert len(games) == 1
        g = games[0]
        assert g.home_team == "Boston Bruins"
        assert g.away_team == "New York Rangers"
        assert g.home_abbr == "BOS"
        assert g.away_abbr == "NYR"
        assert g.state == "pre"
        assert g.detail == ""

    def test_parse_live_game(self) -> None:
        games = EspnProvider._parse_response(ESPN_LIVE_RESPONSE)
        assert len(games) == 1
        g = games[0]
        assert g.state == "live"
        assert "P2" in g.detail or "12:12" in g.detail

    def test_parse_empty_events(self) -> None:
        games = EspnProvider._parse_response({"events": []})
        assert games == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_game_status.py::TestEspnProvider -v`
Expected: FAIL — `cannot import name 'EspnProvider'`

- [ ] **Step 3: Implement EspnProvider**

Add to `src/talos/game_status.py`:

```python
from datetime import UTC

import httpx
import structlog

logger = structlog.get_logger()

_ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"


class EspnProvider:
    """Fetches live game status from ESPN's unofficial scoreboard API."""

    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        self._http = http or httpx.AsyncClient()

    async def fetch_games(
        self, sport: str, league: str, game_date: str
    ) -> list[ExternalGame]:
        url = f"{_ESPN_BASE}/{sport}/{league}/scoreboard"
        try:
            resp = await self._http.get(url, params={"dates": game_date})
            resp.raise_for_status()
            return self._parse_response(resp.json())
        except Exception:
            logger.warning("espn_fetch_failed", sport=sport, league=league, exc_info=True)
            return []

    @staticmethod
    def _parse_response(data: dict) -> list[ExternalGame]:
        games: list[ExternalGame] = []
        for event in data.get("events", []):
            date_str = event.get("date", "")
            try:
                scheduled = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                continue

            for comp in event.get("competitions", []):
                status = comp.get("status", {})
                state_raw = status.get("type", {}).get("state", "pre")
                state_map = {"pre": "pre", "in": "live", "post": "post"}
                state = state_map.get(state_raw, "pre")

                # Build detail string for live games
                detail = ""
                if state == "live":
                    period = status.get("period", 0)
                    clock = status.get("displayClock", "")
                    if period and clock:
                        detail = f"P{period} {clock}"
                    elif period:
                        detail = f"P{period}"

                home_team = away_team = ""
                home_abbr = away_abbr = None
                for team_entry in comp.get("competitors", []):
                    team = team_entry.get("team", {})
                    name = team.get("displayName", "")
                    abbr = team.get("abbreviation")
                    if team_entry.get("homeAway") == "home":
                        home_team = name
                        home_abbr = abbr
                    else:
                        away_team = name
                        away_abbr = abbr

                if home_team and away_team:
                    games.append(
                        ExternalGame(
                            home_team=home_team,
                            away_team=away_team,
                            home_abbr=home_abbr,
                            away_abbr=away_abbr,
                            scheduled_start=scheduled,
                            state=state,
                            detail=detail,
                        )
                    )
        return games
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_game_status.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/talos/game_status.py tests/test_game_status.py
git commit -m "feat(game-status): add ESPN provider with response parsing"
```

---

## Task 3: The Odds API Provider

**Files:**
- Modify: `src/talos/game_status.py`
- Modify: `tests/test_game_status.py`

- [ ] **Step 1: Write failing tests for Odds API response parsing**

Add to `tests/test_game_status.py`:

```python
from talos.game_status import OddsApiProvider

ODDS_PRE_RESPONSE = [
    {
        "id": "abc123",
        "sport_key": "icehockey_ahl",
        "home_team": "Hershey Bears",
        "away_team": "Charlotte Checkers",
        "commence_time": "2026-03-14T23:00:00Z",
        "completed": False,
        "scores": None,
    }
]

ODDS_LIVE_RESPONSE = [
    {
        "id": "abc123",
        "sport_key": "icehockey_ahl",
        "home_team": "Hershey Bears",
        "away_team": "Charlotte Checkers",
        "commence_time": "2026-03-14T20:00:00Z",
        "completed": False,
        "scores": [
            {"name": "Hershey Bears", "score": "2"},
            {"name": "Charlotte Checkers", "score": "1"},
        ],
    }
]

ODDS_FINAL_RESPONSE = [
    {
        "id": "abc123",
        "sport_key": "icehockey_ahl",
        "home_team": "Hershey Bears",
        "away_team": "Charlotte Checkers",
        "commence_time": "2026-03-14T20:00:00Z",
        "completed": True,
        "scores": [
            {"name": "Hershey Bears", "score": "4"},
            {"name": "Charlotte Checkers", "score": "2"},
        ],
    }
]


class TestOddsApiProvider:
    def test_parse_pre_game(self) -> None:
        games = OddsApiProvider._parse_response(ODDS_PRE_RESPONSE)
        assert len(games) == 1
        assert games[0].state == "pre"
        assert games[0].home_team == "Hershey Bears"
        assert games[0].home_abbr is None  # Odds API has no abbreviations

    def test_parse_live_game(self) -> None:
        games = OddsApiProvider._parse_response(ODDS_LIVE_RESPONSE)
        assert len(games) == 1
        assert games[0].state == "live"
        assert "2-1" in games[0].detail

    def test_parse_final_game(self) -> None:
        games = OddsApiProvider._parse_response(ODDS_FINAL_RESPONSE)
        assert len(games) == 1
        assert games[0].state == "post"

    def test_parse_empty(self) -> None:
        assert OddsApiProvider._parse_response([]) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_game_status.py::TestOddsApiProvider -v`
Expected: FAIL — `cannot import name 'OddsApiProvider'`

- [ ] **Step 3: Implement OddsApiProvider**

Add to `src/talos/game_status.py`:

```python
import os

_ODDS_API_BASE = "https://api.the-odds-api.com/v4/sports"


class OddsApiProvider:
    """Fetches live game status from The Odds API (AHL, minor leagues)."""

    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        self._http = http or httpx.AsyncClient()
        self._api_key = os.environ.get("ODDS_API_KEY", "")

    async def fetch_games(
        self, sport: str, league: str, game_date: str
    ) -> list[ExternalGame]:
        if not self._api_key:
            logger.warning("odds_api_key_missing")
            return []
        url = f"{_ODDS_API_BASE}/{league}/scores/"
        try:
            resp = await self._http.get(
                url, params={"apiKey": self._api_key, "daysFrom": "1"}
            )
            resp.raise_for_status()
            return self._parse_response(resp.json())
        except Exception:
            logger.warning("odds_api_fetch_failed", league=league, exc_info=True)
            return []

    @staticmethod
    def _parse_response(data: list) -> list[ExternalGame]:
        games: list[ExternalGame] = []
        for item in data:
            commence = item.get("commence_time", "")
            try:
                scheduled = datetime.fromisoformat(commence.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                continue

            completed = item.get("completed", False)
            scores = item.get("scores")
            has_scores = scores is not None and len(scores) > 0
            now = datetime.now(UTC)

            if completed:
                state = "post"
            elif scheduled <= now and has_scores:
                state = "live"
            else:
                state = "pre"

            # Build score detail for live/post
            detail = ""
            if has_scores:
                parts = [s.get("score", "0") for s in scores]
                detail = "-".join(parts)

            games.append(
                ExternalGame(
                    home_team=item.get("home_team", ""),
                    away_team=item.get("away_team", ""),
                    scheduled_start=scheduled,
                    state=state,
                    detail=detail,
                )
            )
        return games
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_game_status.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/talos/game_status.py tests/test_game_status.py
git commit -m "feat(game-status): add The Odds API provider for AHL and minor leagues"
```

---

## Task 4: PandaScore Provider

**Files:**
- Modify: `src/talos/game_status.py`
- Modify: `tests/test_game_status.py`

- [ ] **Step 1: Write failing tests for PandaScore response parsing**

Add to `tests/test_game_status.py`:

```python
from talos.game_status import PandaScoreProvider

PANDA_RESPONSE = [
    {
        "id": 999,
        "name": "T1 vs Gen.G",
        "scheduled_at": "2026-03-14T10:00:00Z",
        "begin_at": None,
        "status": "not_started",
        "opponents": [
            {"opponent": {"name": "T1", "acronym": "T1"}},
            {"opponent": {"name": "Gen.G", "acronym": "GEN"}},
        ],
        "number_of_games": 3,
        "games": [],
    },
    {
        "id": 1000,
        "name": "Cloud9 vs NRG",
        "scheduled_at": "2026-03-14T14:00:00Z",
        "begin_at": "2026-03-14T14:05:00Z",
        "status": "running",
        "opponents": [
            {"opponent": {"name": "Cloud9", "acronym": "C9"}},
            {"opponent": {"name": "NRG", "acronym": "NRG"}},
        ],
        "number_of_games": 3,
        "games": [{"status": "finished"}, {"status": "running"}],
    },
]


class TestPandaScoreProvider:
    def test_parse_not_started(self) -> None:
        games = PandaScoreProvider._parse_response(PANDA_RESPONSE)
        g = games[0]
        assert g.state == "pre"
        assert g.home_abbr == "T1"
        assert g.away_abbr == "GEN"

    def test_parse_running(self) -> None:
        games = PandaScoreProvider._parse_response(PANDA_RESPONSE)
        g = games[1]
        assert g.state == "live"
        assert g.home_abbr == "C9"
        assert g.away_abbr == "NRG"
        assert "2/3" in g.detail  # game 2 of 3

    def test_begin_at_used_when_available(self) -> None:
        """When begin_at is set, use it as scheduled_start (actual start)."""
        games = PandaScoreProvider._parse_response(PANDA_RESPONSE)
        running = games[1]
        assert running.scheduled_start.minute == 5  # from begin_at, not scheduled_at

    def test_parse_empty(self) -> None:
        assert PandaScoreProvider._parse_response([]) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_game_status.py::TestPandaScoreProvider -v`
Expected: FAIL — `cannot import name 'PandaScoreProvider'`

- [ ] **Step 3: Implement PandaScoreProvider**

Add to `src/talos/game_status.py`:

```python
_PANDA_BASE = "https://api.pandascore.co"


class PandaScoreProvider:
    """Fetches live esports match status from PandaScore."""

    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        self._http = http or httpx.AsyncClient()
        self._token = os.environ.get("PANDASCORE_TOKEN", "")

    async def fetch_games(
        self, sport: str, league: str, game_date: str
    ) -> list[ExternalGame]:
        if not self._token:
            logger.warning("pandascore_token_missing")
            return []
        # game_date is "YYYYMMDD", PandaScore wants "YYYY-MM-DD"
        date_fmt = f"{game_date[:4]}-{game_date[4:6]}-{game_date[6:8]}"
        url = f"{_PANDA_BASE}/{sport}/matches"
        try:
            resp = await self._http.get(
                url,
                params={
                    "filter[scheduled_at]": date_fmt,
                    "sort": "scheduled_at",
                    "per_page": "50",
                },
                headers={"Authorization": f"Bearer {self._token}"},
            )
            resp.raise_for_status()
            return self._parse_response(resp.json())
        except Exception:
            logger.warning("pandascore_fetch_failed", sport=sport, exc_info=True)
            return []

    @staticmethod
    def _parse_response(data: list) -> list[ExternalGame]:
        games: list[ExternalGame] = []
        for match in data:
            # Prefer begin_at (actual start) over scheduled_at
            time_str = match.get("begin_at") or match.get("scheduled_at", "")
            try:
                scheduled = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                continue

            status_raw = match.get("status", "not_started")
            status_map = {
                "not_started": "pre",
                "running": "live",
                "finished": "post",
                "canceled": "post",
                "postponed": "post",
            }
            state = status_map.get(status_raw, "pre")

            # Extract team abbreviations from opponents
            opponents = match.get("opponents", [])
            home_team = opponents[0]["opponent"]["name"] if len(opponents) > 0 else ""
            away_team = opponents[1]["opponent"]["name"] if len(opponents) > 1 else ""
            home_abbr = (
                opponents[0]["opponent"].get("acronym") if len(opponents) > 0 else None
            )
            away_abbr = (
                opponents[1]["opponent"].get("acronym") if len(opponents) > 1 else None
            )

            # Detail: game progress in series (e.g., "2/3")
            detail = ""
            if state == "live":
                num_games = match.get("number_of_games", 0)
                finished_games = sum(
                    1 for g in match.get("games", []) if g.get("status") == "finished"
                )
                current_game = finished_games + 1
                if num_games > 1:
                    detail = f"{current_game}/{num_games}"

            games.append(
                ExternalGame(
                    home_team=home_team,
                    away_team=away_team,
                    home_abbr=home_abbr,
                    away_abbr=away_abbr,
                    scheduled_start=scheduled,
                    state=state,
                    detail=detail,
                )
            )
        return games
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_game_status.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/talos/game_status.py tests/test_game_status.py
git commit -m "feat(game-status): add PandaScore provider for esports"
```

---

## Task 5: GameStatusResolver — Source Mapping and Team Matching

**Files:**
- Modify: `src/talos/game_status.py`
- Modify: `tests/test_game_status.py`

- [ ] **Step 1: Write failing tests for team code extraction and matching**

Add to `tests/test_game_status.py`:

```python
from talos.game_status import GameStatusResolver


class TestTeamExtraction:
    """Test extracting team codes from Kalshi event tickers."""

    def test_standard_ticker(self) -> None:
        codes = GameStatusResolver.extract_team_codes("KXNHL-26MAR14-BOS-NYR")
        assert codes == ("BOS", "NYR")

    def test_short_ticker(self) -> None:
        """Tickers with < 4 segments should return None."""
        codes = GameStatusResolver.extract_team_codes("KXNHL-26MAR14")
        assert codes is None

    def test_non_team_suffix(self) -> None:
        """Segments that don't look like team codes (too long, numeric) return None."""
        codes = GameStatusResolver.extract_team_codes("KXBTC-26MAR-T50000")
        assert codes is None

    def test_three_letter_codes(self) -> None:
        codes = GameStatusResolver.extract_team_codes("KXNBA-26MAR14-LAL-GSW")
        assert codes == ("LAL", "GSW")

    def test_two_letter_codes(self) -> None:
        codes = GameStatusResolver.extract_team_codes("KXCBB-26MAR14-VT-UK")
        assert codes == ("VT", "UK")


class TestSubtitleExtraction:
    """Test extracting team names from Kalshi event sub_title."""

    def test_at_separator(self) -> None:
        codes = GameStatusResolver.extract_from_subtitle("WAKE at VT (Mar 10)")
        assert codes == ("WAKE", "VT")

    def test_vs_separator(self) -> None:
        codes = GameStatusResolver.extract_from_subtitle("T1 vs Gen.G")
        assert codes == ("T1", "GEN.G")

    def test_no_separator(self) -> None:
        codes = GameStatusResolver.extract_from_subtitle("Something else")
        assert codes is None


class TestGameMatching:
    """Test matching extracted team codes against ExternalGame list."""

    def test_abbr_match(self) -> None:
        dt = datetime(2026, 3, 14, 20, 0, tzinfo=UTC)
        games = [
            ExternalGame(
                home_team="Boston Bruins", away_team="New York Rangers",
                home_abbr="BOS", away_abbr="NYR",
                scheduled_start=dt, state="pre",
            ),
            ExternalGame(
                home_team="Tampa Bay Lightning", away_team="Florida Panthers",
                home_abbr="TB", away_abbr="FLA",
                scheduled_start=dt, state="pre",
            ),
        ]
        result = GameStatusResolver.match_game(("BOS", "NYR"), games)
        assert result is not None
        assert result.home_team == "Boston Bruins"

    def test_abbr_match_order_independent(self) -> None:
        dt = datetime(2026, 3, 14, 20, 0, tzinfo=UTC)
        games = [
            ExternalGame(
                home_team="Boston Bruins", away_team="New York Rangers",
                home_abbr="BOS", away_abbr="NYR",
                scheduled_start=dt, state="pre",
            ),
        ]
        # Reversed order should still match
        result = GameStatusResolver.match_game(("NYR", "BOS"), games)
        assert result is not None

    def test_substring_fallback(self) -> None:
        """When no abbreviations, fall back to substring match on names."""
        dt = datetime(2026, 3, 14, 20, 0, tzinfo=UTC)
        games = [
            ExternalGame(
                home_team="Hershey Bears", away_team="Charlotte Checkers",
                scheduled_start=dt, state="pre",
            ),
        ]
        result = GameStatusResolver.match_game(("HER", "CHA"), games)
        assert result is not None
        assert result.home_team == "Hershey Bears"

    def test_no_match(self) -> None:
        dt = datetime(2026, 3, 14, 20, 0, tzinfo=UTC)
        games = [
            ExternalGame(
                home_team="Boston Bruins", away_team="New York Rangers",
                home_abbr="BOS", away_abbr="NYR",
                scheduled_start=dt, state="pre",
            ),
        ]
        result = GameStatusResolver.match_game(("LAL", "GSW"), games)
        assert result is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_game_status.py::TestTeamExtraction -v`
Expected: FAIL — `has no attribute 'extract_team_codes'`

- [ ] **Step 3: Implement GameStatusResolver with source mapping, extraction, and matching**

Add to `src/talos/game_status.py`:

```python
import re

# Series ticker prefix → (provider_name, sport, league)
SOURCE_MAP: dict[str, tuple[str, str, str]] = {
    # ESPN
    "KXNHL":  ("espn",       "hockey",      "nhl"),
    "KXNBA":  ("espn",       "basketball",  "nba"),
    "KXMLB":  ("espn",       "baseball",    "mlb"),
    "KXNFL":  ("espn",       "football",    "nfl"),
    "KXWNBA": ("espn",       "basketball",  "wnba"),
    "KXCFB":  ("espn",       "football",    "college-football"),
    "KXCBB":  ("espn",       "basketball",  "mens-college-basketball"),
    "KXMLS":  ("espn",       "soccer",      "usa.1"),
    "KXEPL":  ("espn",       "soccer",      "eng.1"),
    # The Odds API
    "KXAHL":  ("odds-api",   "icehockey_ahl", "icehockey_ahl"),
    # PandaScore
    "KXLOL":  ("pandascore", "lol",         "league-of-legends"),
    "KXCS2":  ("pandascore", "csgo",        "cs2"),
    "KXVAL":  ("pandascore", "valorant",    "valorant"),
    "KXDOTA": ("pandascore", "dota2",       "dota-2"),
}

# Regex for team abbreviation: 2-4 uppercase letters
_TEAM_CODE_RE = re.compile(r"^[A-Z]{2,4}$")


class GameStatusResolver:
    """Maps Kalshi events to external game status via multi-source lookup."""

    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        client = http or httpx.AsyncClient()
        self._providers: dict[str, GameStatusProvider] = {
            "espn": EspnProvider(client),
            "odds-api": OddsApiProvider(client),
            "pandascore": PandaScoreProvider(client),
        }
        # Cache: event_ticker -> (GameStatus, team_codes, source_key)
        self._cache: dict[str, tuple[GameStatus, tuple[str, str] | None, str]] = {}

    @staticmethod
    def extract_team_codes(event_ticker: str) -> tuple[str, str] | None:
        """Extract team abbreviation pair from event ticker suffix.

        E.g. 'KXNHL-26MAR14-BOS-NYR' -> ('BOS', 'NYR').
        Returns None if last two segments don't look like team codes.
        """
        parts = event_ticker.split("-")
        if len(parts) < 4:
            return None
        code_a, code_b = parts[-2], parts[-1]
        if _TEAM_CODE_RE.match(code_a) and _TEAM_CODE_RE.match(code_b):
            return (code_a, code_b)
        return None

    @staticmethod
    def extract_from_subtitle(sub_title: str) -> tuple[str, str] | None:
        """Extract team names from Kalshi event sub_title.

        E.g. 'WAKE at VT (Mar 10)' -> ('WAKE', 'VT').
        """
        # Strip date suffix in parens
        text = sub_title
        if "(" in text:
            text = text[: text.rfind("(")].strip()
        # Try separators
        for sep in (" at ", " vs ", " vs. "):
            if sep in text:
                parts = text.split(sep, 1)
                return (parts[0].strip().upper(), parts[1].strip().upper())
        return None

    @staticmethod
    def match_game(
        team_codes: tuple[str, str], games: list[ExternalGame]
    ) -> ExternalGame | None:
        """Find the game matching the team codes (order-independent)."""
        code_set = {c.upper() for c in team_codes}
        # Strategy 1: abbreviation match
        for g in games:
            if g.home_abbr and g.away_abbr:
                game_set = {g.home_abbr.upper(), g.away_abbr.upper()}
                if code_set == game_set:
                    return g
        # Strategy 2: substring match on full names
        for g in games:
            name_upper = f"{g.home_team} {g.away_team}".upper()
            if all(code in name_upper for code in code_set):
                return g
        return None

    async def resolve(
        self,
        event_ticker: str,
        sub_title: str = "",
    ) -> GameStatus:
        """Resolve game status for a Kalshi event. Caches the result."""
        series_prefix = event_ticker.split("-")[0]
        source = SOURCE_MAP.get(series_prefix)
        if source is None:
            logger.warning("unmapped_series_ticker", prefix=series_prefix, event=event_ticker)
            status = GameStatus(state="unknown")
            self._cache[event_ticker] = (status, None, "")
            return status

        provider_name, sport, league = source
        provider = self._providers.get(provider_name)
        if provider is None:
            status = GameStatus(state="unknown")
            self._cache[event_ticker] = (status, None, "")
            return status

        # Extract team codes
        team_codes = self.extract_team_codes(event_ticker)
        if team_codes is None and sub_title:
            team_codes = self.extract_from_subtitle(sub_title)

        # Determine game date from ticker (e.g. "KXNHL-26MAR14" -> "20260314")
        game_date = _extract_date_from_ticker(event_ticker)
        if game_date is None:
            game_date = datetime.now(UTC).strftime("%Y%m%d")

        games = await provider.fetch_games(sport, league, game_date)

        if team_codes is not None:
            matched = self.match_game(team_codes, games)
        else:
            matched = None

        if matched is None:
            status = GameStatus(state="unknown")
        else:
            status = GameStatus(
                state=matched.state,
                scheduled_start=matched.scheduled_start,
                detail=matched.detail,
            )

        source_key = f"{provider_name}:{sport}:{league}"
        self._cache[event_ticker] = (status, team_codes, source_key)
        return status

    async def refresh_all(self) -> None:
        """Re-fetch status for all cached events."""
        for event_ticker, (_, team_codes, source_key) in list(self._cache.items()):
            if not source_key:
                continue
            parts = source_key.split(":", 2)
            if len(parts) != 3:
                continue
            provider_name, sport, league = parts
            provider = self._providers.get(provider_name)
            if provider is None:
                continue

            game_date = _extract_date_from_ticker(event_ticker)
            if game_date is None:
                game_date = datetime.now(UTC).strftime("%Y%m%d")

            games = await provider.fetch_games(sport, league, game_date)
            if team_codes is not None:
                matched = self.match_game(team_codes, games)
            else:
                matched = None

            if matched is not None:
                status = GameStatus(
                    state=matched.state,
                    scheduled_start=matched.scheduled_start,
                    detail=matched.detail,
                )
                self._cache[event_ticker] = (status, team_codes, source_key)
            # If refresh fails, keep stale cached value (don't overwrite)

    def get(self, event_ticker: str) -> GameStatus | None:
        """Read cached game status. Returns None if not cached."""
        entry = self._cache.get(event_ticker)
        return entry[0] if entry else None

    def remove(self, event_ticker: str) -> None:
        """Remove a cached entry (when game is removed from monitoring)."""
        self._cache.pop(event_ticker, None)


def _extract_date_from_ticker(event_ticker: str) -> str | None:
    """Try to extract a date from the event ticker.

    Kalshi tickers like 'KXNHL-26MAR14-BOS-NYR' encode the date as
    YY + 3-letter month + DD in the second segment.
    Returns 'YYYYMMDD' string or None.
    """
    parts = event_ticker.split("-")
    if len(parts) < 2:
        return None
    segment = parts[1]
    months = {
        "JAN": "01", "FEB": "02", "MAR": "03", "APR": "04",
        "MAY": "05", "JUN": "06", "JUL": "07", "AUG": "08",
        "SEP": "09", "OCT": "10", "NOV": "11", "DEC": "12",
    }
    # Pattern: 2-digit year + 3-letter month + 1-2 digit day
    m = re.match(r"(\d{2})([A-Z]{3})(\d{1,2})$", segment)
    if m:
        yy, mon, dd = m.group(1), m.group(2), m.group(3)
        mm = months.get(mon)
        if mm:
            return f"20{yy}{mm}{dd.zfill(2)}"
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_game_status.py -v`
Expected: PASS

- [ ] **Step 5: Write test for date extraction from ticker**

Add to `tests/test_game_status.py`:

```python
from talos.game_status import _extract_date_from_ticker


class TestDateExtraction:
    def test_standard_ticker(self) -> None:
        assert _extract_date_from_ticker("KXNHL-26MAR14-BOS-NYR") == "20260314"

    def test_single_digit_day(self) -> None:
        assert _extract_date_from_ticker("KXNBA-26MAR5-LAL-GSW") == "20260305"

    def test_no_date_segment(self) -> None:
        assert _extract_date_from_ticker("KXBTC") is None

    def test_non_date_segment(self) -> None:
        assert _extract_date_from_ticker("KXBTC-T50000") is None
```

- [ ] **Step 6: Write integration tests for resolve() and refresh_all()**

Add to `tests/test_game_status.py`:

```python
from unittest.mock import AsyncMock


class TestResolverIntegration:
    """End-to-end tests for resolve() with mocked providers."""

    @pytest.mark.asyncio
    async def test_resolve_espn_game(self) -> None:
        resolver = GameStatusResolver()
        # Mock ESPN provider
        mock_provider = AsyncMock()
        dt = datetime(2026, 3, 14, 20, 0, tzinfo=UTC)
        mock_provider.fetch_games.return_value = [
            ExternalGame(
                home_team="Boston Bruins", away_team="New York Rangers",
                home_abbr="BOS", away_abbr="NYR",
                scheduled_start=dt, state="pre",
            )
        ]
        resolver._providers["espn"] = mock_provider

        status = await resolver.resolve("KXNHL-26MAR14-BOS-NYR")
        assert status.state == "pre"
        assert status.scheduled_start == dt
        mock_provider.fetch_games.assert_called_once()

    @pytest.mark.asyncio
    async def test_resolve_unmapped_series(self) -> None:
        resolver = GameStatusResolver()
        status = await resolver.resolve("KXFOO-26MAR14-AAA-BBB")
        assert status.state == "unknown"

    @pytest.mark.asyncio
    async def test_resolve_no_match(self) -> None:
        resolver = GameStatusResolver()
        mock_provider = AsyncMock()
        mock_provider.fetch_games.return_value = []
        resolver._providers["espn"] = mock_provider

        status = await resolver.resolve("KXNHL-26MAR14-BOS-NYR")
        assert status.state == "unknown"

    @pytest.mark.asyncio
    async def test_get_cached(self) -> None:
        resolver = GameStatusResolver()
        mock_provider = AsyncMock()
        dt = datetime(2026, 3, 14, 20, 0, tzinfo=UTC)
        mock_provider.fetch_games.return_value = [
            ExternalGame(
                home_team="Boston Bruins", away_team="New York Rangers",
                home_abbr="BOS", away_abbr="NYR",
                scheduled_start=dt, state="live", detail="P2 12:00",
            )
        ]
        resolver._providers["espn"] = mock_provider

        await resolver.resolve("KXNHL-26MAR14-BOS-NYR")
        cached = resolver.get("KXNHL-26MAR14-BOS-NYR")
        assert cached is not None
        assert cached.state == "live"

    @pytest.mark.asyncio
    async def test_refresh_all_updates_cache(self) -> None:
        resolver = GameStatusResolver()
        mock_provider = AsyncMock()
        dt = datetime(2026, 3, 14, 20, 0, tzinfo=UTC)
        # First call: pre-game
        mock_provider.fetch_games.return_value = [
            ExternalGame(
                home_team="Boston Bruins", away_team="New York Rangers",
                home_abbr="BOS", away_abbr="NYR",
                scheduled_start=dt, state="pre",
            )
        ]
        resolver._providers["espn"] = mock_provider
        await resolver.resolve("KXNHL-26MAR14-BOS-NYR")
        assert resolver.get("KXNHL-26MAR14-BOS-NYR").state == "pre"

        # Second call via refresh: now live
        mock_provider.fetch_games.return_value = [
            ExternalGame(
                home_team="Boston Bruins", away_team="New York Rangers",
                home_abbr="BOS", away_abbr="NYR",
                scheduled_start=dt, state="live", detail="P1 15:00",
            )
        ]
        await resolver.refresh_all()
        assert resolver.get("KXNHL-26MAR14-BOS-NYR").state == "live"

    @pytest.mark.asyncio
    async def test_refresh_keeps_stale_on_failure(self) -> None:
        resolver = GameStatusResolver()
        mock_provider = AsyncMock()
        dt = datetime(2026, 3, 14, 20, 0, tzinfo=UTC)
        mock_provider.fetch_games.return_value = [
            ExternalGame(
                home_team="Boston Bruins", away_team="New York Rangers",
                home_abbr="BOS", away_abbr="NYR",
                scheduled_start=dt, state="pre",
            )
        ]
        resolver._providers["espn"] = mock_provider
        await resolver.resolve("KXNHL-26MAR14-BOS-NYR")

        # Refresh returns empty (API failure) — cache should keep stale value
        mock_provider.fetch_games.return_value = []
        await resolver.refresh_all()
        assert resolver.get("KXNHL-26MAR14-BOS-NYR").state == "pre"

    def test_remove(self) -> None:
        resolver = GameStatusResolver()
        resolver._cache["KXNHL-26MAR14-BOS-NYR"] = (
            GameStatus(state="pre"), ("BOS", "NYR"), "espn:hockey:nhl"
        )
        resolver.remove("KXNHL-26MAR14-BOS-NYR")
        assert resolver.get("KXNHL-26MAR14-BOS-NYR") is None
```

- [ ] **Step 7: Run all tests**

Run: `.venv/Scripts/python -m pytest tests/test_game_status.py -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add src/talos/game_status.py tests/test_game_status.py
git commit -m "feat(game-status): add GameStatusResolver with source mapping, team matching, and caching"
```

---

## Task 6: UI Columns — Replace "Closes" with "Date" + "Status"

**Files:**
- Modify: `src/talos/ui/widgets.py`
- Modify: `tests/test_game_status.py` (add formatter tests)

The existing `_fmt_closes` function (widgets.py:75-99) and the "Closes" column (widgets.py:199) will be replaced. The table's `refresh_from_scanner` method (widgets.py:237) will read from a resolver reference instead of `opp.close_time`.

- [ ] **Step 1: Write failing tests for the new formatting functions**

Add to `tests/test_game_status.py`:

```python
from datetime import timedelta
from zoneinfo import ZoneInfo

from talos.ui.widgets import _fmt_game_date, _fmt_game_status

PT = ZoneInfo("America/Los_Angeles")


class TestFmtGameDate:
    def test_with_datetime(self) -> None:
        dt = datetime(2026, 3, 14, 20, 0, tzinfo=UTC)
        result = _fmt_game_date(dt)
        # In Pacific time, 20:00 UTC on Mar 14 = 13:00 PT (still Mar 14)
        assert "03/14" in str(result)

    def test_none(self) -> None:
        result = _fmt_game_date(None)
        assert "—" in str(result)


class TestFmtGameStatus:
    def test_unknown(self) -> None:
        gs = GameStatus(state="unknown")
        result = _fmt_game_status(gs)
        assert "—" in str(result)

    def test_pre_far_out(self) -> None:
        future = datetime.now(UTC) + timedelta(hours=3)
        gs = GameStatus(state="pre", scheduled_start=future)
        result = str(_fmt_game_status(gs))
        # Should show time like "1:30 PM" (Pacific), not countdown
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_game_status.py::TestFmtGameDate -v`
Expected: FAIL — `cannot import name '_fmt_game_date'`

- [ ] **Step 3: Implement formatting functions in widgets.py**

Add new functions to `src/talos/ui/widgets.py` (near the existing `_fmt_closes`):

```python
from zoneinfo import ZoneInfo

from talos.game_status import GameStatus

_PT = ZoneInfo("America/Los_Angeles")


def _fmt_game_date(scheduled_start: datetime | None) -> RichText:
    """Format game date as MM/DD in Pacific Time."""
    if scheduled_start is None:
        return DIM_DASH
    pt = scheduled_start.astimezone(_PT)
    return RichText(pt.strftime("%m/%d"), justify="right")


def _fmt_game_status(status: GameStatus | None) -> RichText:
    """Format game status for the Status column."""
    if status is None or status.state == "unknown":
        return DIM_DASH
    if status.state == "post":
        return RichText("FINAL", style="dim", justify="right")
    if status.state == "live":
        label = f"LIVE {status.detail}".strip()
        return RichText(label, style=GREEN, justify="right")
    # state == "pre"
    if status.scheduled_start is None:
        return DIM_DASH
    now = datetime.now(UTC)
    delta = status.scheduled_start - now
    minutes_left = int(delta.total_seconds() / 60)
    if minutes_left <= 15:
        return RichText(f"in {minutes_left}m", style=YELLOW, justify="right")
    pt = status.scheduled_start.astimezone(_PT)
    time_str = pt.strftime("%I:%M %p").lstrip("0")
    return RichText(time_str, justify="right")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_game_status.py::TestFmtGameDate tests/test_game_status.py::TestFmtGameStatus -v`
Expected: PASS

- [ ] **Step 5: Replace "Closes" column with "Date" + "Status" in OpportunitiesTable**

In `src/talos/ui/widgets.py`, modify the `OpportunitiesTable` class:

1. In `__init__`, add a `_resolver` field (typed as `Any` to avoid circular import, or use TYPE_CHECKING):

```python
def __init__(self, **kwargs: Any) -> None:
    super().__init__(**kwargs)
    self._positions: dict[str, EventPositionSummary] = {}
    self._labels: dict[str, str] = {}
    self._resolver: Any = None  # GameStatusResolver, set by app
```

2. Add a setter method:

```python
def set_resolver(self, resolver: Any) -> None:
    """Set the game status resolver for Date/Status columns."""
    self._resolver = resolver
```

3. In `on_mount`, replace the `Closes` column line with:

```python
    self.add_column(RichText("Date", justify=r), width=6)
    self.add_column(RichText("Game", justify=r), width=9)
```

4. In `refresh_from_scanner`, replace the `closes` line with:

```python
    game_status = self._resolver.get(opp.event_ticker) if self._resolver else None
    game_date = _fmt_game_date(
        game_status.scheduled_start if game_status else None
    )
    game_col = _fmt_game_status(game_status)
```

And update the `row_data` tuple — replace the single `closes` element with `game_date, game_col`:

```python
                row_data = (
                    display_name,
                    _fmt_cents(opp.no_a),
                    _fmt_cents(opp.no_b),
                    edge_str,
                    game_date,   # was: closes
                    game_col,    # new column
                    pos_a,
                    pos_b,
                    q_a,
                    cpm_a,
                    eta_a,
                    q_b,
                    cpm_b,
                    eta_b,
                    status,
                    pnl,
                    net_odds,
                )
```

- [ ] **Step 6: Run all tests**

Run: `.venv/Scripts/python -m pytest tests/test_game_status.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/talos/ui/widgets.py tests/test_game_status.py
git commit -m "feat(game-status): replace Closes column with Date + Game Status columns"
```

---

## Task 7: Wiring — Connect Resolver to Engine, GameManager, and App

**Files:**
- Modify: `src/talos/__main__.py` — instantiate resolver, pass to engine
- Modify: `src/talos/engine.py` — store resolver, add hourly refresh, resolve on add_game
- Modify: `src/talos/ui/app.py` — pass resolver to OpportunitiesTable

- [ ] **Step 1: Add resolver to TradingEngine**

In `src/talos/engine.py`:

1. Add import at top:

```python
from talos.game_status import GameStatusResolver
```

2. Add `game_status_resolver` parameter to `__init__`:

```python
def __init__(
    self,
    *,
    scanner: ArbitrageScanner,
    game_manager: GameManager,
    rest_client: KalshiRESTClient,
    market_feed: MarketFeed,
    tracker: TopOfMarketTracker,
    adjuster: BidAdjuster,
    initial_games: list[str] | None = None,
    proposal_queue: ProposalQueue | None = None,
    automation_config: AutomationConfig | None = None,
    portfolio_feed: PortfolioFeed | None = None,
    ticker_feed: TickerFeed | None = None,
    lifecycle_feed: LifecycleFeed | None = None,
    position_feed: PositionFeed | None = None,
    game_status_resolver: GameStatusResolver | None = None,
) -> None:
    # ... existing init code ...
    self._game_status_resolver = game_status_resolver
```

3. Add property:

```python
@property
def game_status_resolver(self) -> GameStatusResolver | None:
    return self._game_status_resolver
```

4. In the `add_games` method (which calls `game_manager.add_game`), after games are added, resolve status for each. Find the method that calls `self._game_manager.add_game()` or `add_games()` and add status resolution after:

```python
async def add_games(self, urls: list[str]) -> None:
    # ... existing code that calls game_manager.add_games ...
    # After pairs are added, resolve game status
    if self._game_status_resolver is not None:
        for pair in self._game_manager.active_games:
            event = None  # sub_title unavailable here — resolve with ticker only
            await self._game_status_resolver.resolve(pair.event_ticker)
```

5. Add hourly refresh method:

```python
async def refresh_game_status(self) -> None:
    """Hourly: re-fetch game status for all active events."""
    if self._game_status_resolver is not None:
        await self._game_status_resolver.refresh_all()
```

- [ ] **Step 2: Wire resolver in __main__.py**

In `src/talos/__main__.py`:

1. Add import:

```python
from talos.game_status import GameStatusResolver
```

2. After `game_mgr` creation, instantiate resolver:

```python
game_status_resolver = GameStatusResolver()
```

3. Pass to engine:

```python
engine = TradingEngine(
    # ... existing args ...
    game_status_resolver=game_status_resolver,
)
```

- [ ] **Step 3: Wire resolver to OpportunitiesTable in app.py**

In `src/talos/ui/app.py`:

1. In `on_mount`, after `self.set_interval(0.5, self.refresh_opportunities)`, pass the resolver to the table:

```python
if self._engine is not None and self._engine.game_status_resolver is not None:
    table = self.query_one(OpportunitiesTable)
    table.set_resolver(self._engine.game_status_resolver)
```

2. Add hourly refresh timer in `on_mount`:

```python
if self._engine is not None:
    # ... existing intervals ...
    self.set_interval(3600.0, self._refresh_game_status)
```

3. Add the refresh method:

```python
@work(thread=False)
async def _refresh_game_status(self) -> None:
    if self._engine is not None:
        await self._engine.refresh_game_status()
```

- [ ] **Step 4: Resolve game status at add-time with sub_title**

The `GameManager.add_game()` has access to the full `Event` object (with `sub_title`). The cleanest place to call `resolver.resolve()` is right after the game is added. Modify `engine.py`'s `add_games` method to pass `sub_title`:

In `GameManager`, expose the event data so the engine can pass sub_title to the resolver. The simplest approach: store sub_titles alongside labels (GameManager already stores labels from sub_title at line 128-136).

To make raw `sub_title` available, add a `_subtitles: dict[str, str]` dict to `GameManager` alongside `_labels`. In `add_game()`, store `self._subtitles[event.event_ticker] = event.sub_title` before the label transformation happens. Add a `subtitles` property mirroring `labels`.

In the engine's `add_games` flow, after `game_manager.add_games(urls)` returns, iterate and resolve:

```python
if self._game_status_resolver is not None:
    for pair in pairs:
        sub_title = self._game_manager.subtitles.get(pair.event_ticker, "")
        await self._game_status_resolver.resolve(pair.event_ticker, sub_title)
```

- [ ] **Step 5: Wire resolver.remove() on game removal**

In `src/talos/engine.py`, find the methods that remove games and add cleanup:

- In the method that calls `game_manager.remove_game(event_ticker)`, add:
  ```python
  if self._game_status_resolver is not None:
      self._game_status_resolver.remove(event_ticker)
  ```
- In the method that calls `game_manager.clear_all_games()`, add:
  ```python
  if self._game_status_resolver is not None:
      for pair in self._game_manager.active_games:
          self._game_status_resolver.remove(pair.event_ticker)
  ```
  (Call this **before** `clear_all_games()` since active_games will be empty after.)

- [ ] **Step 6: Clean up — remove unused _fmt_closes**

In `src/talos/ui/widgets.py`:
- Verify no tests reference `_fmt_closes` before removing
- Remove the `_fmt_closes` function (lines 75-99) — it's no longer used
- The `close_time` field on `ArbPair` and `Opportunity` stays (other code may use it) — only the UI column is removed

- [ ] **Step 6: Run full test suite**

Run: `.venv/Scripts/python -m pytest -v`
Expected: PASS (existing tests + new game_status tests)

- [ ] **Step 7: Commit**

```bash
git add src/talos/engine.py src/talos/__main__.py src/talos/ui/app.py src/talos/ui/widgets.py
git commit -m "feat(game-status): wire resolver into engine, app, and table"
```

---

## Task 8: Smoke Test with Real ESPN Data

**Files:** None modified — manual verification only

- [ ] **Step 1: Test ESPN endpoint manually**

Run from project root:

```bash
.venv/Scripts/python -c "
import asyncio, httpx

async def main():
    async with httpx.AsyncClient() as http:
        r = await http.get(
            'https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard'
        )
        data = r.json()
        for event in data.get('events', [])[:3]:
            comp = event['competitions'][0]
            status = comp['status']['type']
            teams = [c['team']['abbreviation'] for c in comp['competitors']]
            print(f'{teams[0]} vs {teams[1]}: {status[\"state\"]} ({status[\"description\"]})')

asyncio.run(main())
"
```

Expected: Prints today's NHL games with their status (pre/in/post)

- [ ] **Step 2: Test full resolver with a mock event ticker**

```bash
.venv/Scripts/python -c "
import asyncio
from talos.game_status import GameStatusResolver

async def main():
    resolver = GameStatusResolver()
    # Use a real game date — adjust ticker to match today's games
    status = await resolver.resolve('KXNHL-26MAR14-BOS-NYR')
    print(f'State: {status.state}')
    print(f'Start: {status.scheduled_start}')
    print(f'Detail: {status.detail}')

asyncio.run(main())
"
```

Expected: Prints the game status (state/start/detail) or "unknown" if no matching game today

- [ ] **Step 3: Launch Talos and verify columns render**

Run: `.venv/Scripts/python -m talos`

Verify the table shows "Date" and "Game" columns where "Closes" used to be. For any added games, the columns should show date and status (or "—" if no match).

- [ ] **Step 4: Final commit if any adjustments needed**

```bash
git add -u
git commit -m "fix(game-status): adjustments from smoke testing"
```
