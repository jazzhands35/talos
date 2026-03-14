"""Game status models, protocol, and external API providers.

Providers fetch live game data from ESPN, The Odds API, and PandaScore,
normalizing it into ExternalGame objects for downstream matching.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

import httpx
import structlog
from pydantic import BaseModel

logger = structlog.get_logger()


# ── Models ─────────────────────────────────────────────────────────


class GameStatus(BaseModel):
    """Cached game state for a Kalshi event."""

    state: str  # "pre" | "live" | "post" | "unknown"
    scheduled_start: datetime | None = None
    detail: str = ""


class ExternalGame(BaseModel):
    """Normalized game from an external API."""

    home_team: str
    away_team: str
    home_abbr: str | None = None
    away_abbr: str | None = None
    scheduled_start: datetime
    state: str  # "pre" | "live" | "post"
    detail: str = ""


# ── Protocol ───────────────────────────────────────────────────────


@runtime_checkable
class GameStatusProvider(Protocol):
    """Interface for external game data providers."""

    async def fetch_games(
        self, sport: str, league: str, game_date: str
    ) -> list[ExternalGame]: ...


# ── ESPN Provider ──────────────────────────────────────────────────


_ESPN_STATE_MAP = {
    "pre": "pre",
    "in": "live",
    "post": "post",
}


class EspnProvider:
    """Fetches game data from the ESPN Scoreboard API (no auth required)."""

    BASE_URL = "https://site.api.espn.com/apis/site/v2/sports"

    @staticmethod
    def _parse_response(data: dict) -> list[ExternalGame]:
        games: list[ExternalGame] = []
        for event in data.get("events", []):
            try:
                competition = event["competitions"][0]
                status = competition["status"]
                raw_state = status["type"]["state"]
                state = _ESPN_STATE_MAP.get(raw_state, "unknown")

                detail = ""
                if state == "live":
                    period = status.get("period", 0)
                    clock = status.get("displayClock", "")
                    detail = f"P{period} {clock}"

                competitors = competition["competitors"]
                home_team = ""
                away_team = ""
                home_abbr = None
                away_abbr = None
                for comp in competitors:
                    team = comp["team"]
                    if comp["homeAway"] == "home":
                        home_team = team["displayName"]
                        home_abbr = team.get("abbreviation")
                    else:
                        away_team = team["displayName"]
                        away_abbr = team.get("abbreviation")

                scheduled_start = datetime.fromisoformat(
                    event["date"].replace("Z", "+00:00")
                )

                games.append(
                    ExternalGame(
                        home_team=home_team,
                        away_team=away_team,
                        home_abbr=home_abbr,
                        away_abbr=away_abbr,
                        scheduled_start=scheduled_start,
                        state=state,
                        detail=detail,
                    )
                )
            except (KeyError, IndexError, ValueError):
                logger.warning("espn_parse_event_failed", event=event)
        return games

    async def fetch_games(
        self, sport: str, league: str, game_date: str
    ) -> list[ExternalGame]:
        url = f"{self.BASE_URL}/{sport}/{league}/scoreboard"
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(url, params={"dates": game_date})
                resp.raise_for_status()
                return self._parse_response(resp.json())
        except httpx.HTTPError:
            logger.warning("espn_fetch_failed", sport=sport, league=league)
            return []


# ── OddsAPI Provider ───────────────────────────────────────────────


class OddsApiProvider:
    """Fetches game data from The Odds API (requires API key)."""

    BASE_URL = "https://api.the-odds-api.com/v4/sports"

    @staticmethod
    def _parse_response(data: list) -> list[ExternalGame]:
        games: list[ExternalGame] = []
        for item in data:
            try:
                commence = datetime.fromisoformat(
                    item["commence_time"].replace("Z", "+00:00")
                )
                completed = item.get("completed", False)
                scores = item.get("scores")

                now = datetime.now(UTC)
                if completed:
                    state = "post"
                elif commence <= now and scores:
                    state = "live"
                else:
                    state = "pre"

                detail = ""
                if scores and (state == "live" or state == "post"):
                    score_parts = [s["score"] for s in scores]
                    detail = "-".join(score_parts)

                games.append(
                    ExternalGame(
                        home_team=item["home_team"],
                        away_team=item["away_team"],
                        scheduled_start=commence,
                        state=state,
                        detail=detail,
                    )
                )
            except (KeyError, ValueError):
                logger.warning("odds_api_parse_event_failed", item=item)
        return games

    async def fetch_games(
        self, sport: str, league: str, game_date: str  # noqa: ARG002
    ) -> list[ExternalGame]:
        # sport and game_date unused — Odds API uses league directly, daysFrom=1
        api_key = os.environ.get("ODDS_API_KEY", "")
        if not api_key:
            logger.warning("odds_api_key_missing")
            return []

        url = f"{self.BASE_URL}/{league}/scores/"
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    url, params={"apiKey": api_key, "daysFrom": "1"}
                )
                resp.raise_for_status()
                return self._parse_response(resp.json())
        except httpx.HTTPError:
            logger.warning("odds_api_fetch_failed", league=league)
            return []


# ── PandaScore Provider ────────────────────────────────────────────

_PANDA_STATE_MAP = {
    "not_started": "pre",
    "running": "live",
    "finished": "post",
    "canceled": "post",
    "postponed": "post",
}


class PandaScoreProvider:
    """Fetches game data from PandaScore API (requires bearer token)."""

    BASE_URL = "https://api.pandascore.co"

    @staticmethod
    def _parse_response(data: list) -> list[ExternalGame]:
        games: list[ExternalGame] = []
        for item in data:
            try:
                raw_status = item.get("status", "")
                state = _PANDA_STATE_MAP.get(raw_status, "unknown")

                # Prefer begin_at over scheduled_at
                time_str = item.get("begin_at") or item.get("scheduled_at", "")
                scheduled_start = datetime.fromisoformat(
                    time_str.replace("Z", "+00:00")
                )

                # Team info from opponents array
                opponents = item.get("opponents", [])
                home_team = ""
                away_team = ""
                home_abbr = None
                away_abbr = None
                if len(opponents) >= 2:
                    home_team = opponents[0]["opponent"]["name"]
                    away_team = opponents[1]["opponent"]["name"]
                    home_abbr = opponents[0]["opponent"].get("acronym")
                    away_abbr = opponents[1]["opponent"].get("acronym")

                # Detail for live games: current_game/total_games
                detail = ""
                if state == "live":
                    total_games = item.get("number_of_games", 0)
                    match_games = item.get("games", [])
                    current_game = len(match_games)
                    detail = f"{current_game}/{total_games}"

                games.append(
                    ExternalGame(
                        home_team=home_team,
                        away_team=away_team,
                        home_abbr=home_abbr,
                        away_abbr=away_abbr,
                        scheduled_start=scheduled_start,
                        state=state,
                        detail=detail,
                    )
                )
            except (KeyError, ValueError):
                logger.warning("pandascore_parse_event_failed", item=item)
        return games

    async def fetch_games(
        self, sport: str, league: str, game_date: str  # noqa: ARG002
    ) -> list[ExternalGame]:
        # league unused — PandaScore uses sport slug for URL path
        token = os.environ.get("PANDASCORE_TOKEN", "")
        if not token:
            logger.warning("pandascore_token_missing")
            return []

        # game_date is "YYYYMMDD", convert to "YYYY-MM-DD" for API
        api_date = f"{game_date[:4]}-{game_date[4:6]}-{game_date[6:8]}"

        url = f"{self.BASE_URL}/{sport}/matches"
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    url,
                    params={
                        "filter[scheduled_at]": api_date,
                        "sort": "scheduled_at",
                        "per_page": "50",
                    },
                    headers={"Authorization": f"Bearer {token}"},
                )
                resp.raise_for_status()
                return self._parse_response(resp.json())
        except httpx.HTTPError:
            logger.warning("pandascore_fetch_failed", sport=sport)
            return []
