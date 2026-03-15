"""Game status models, protocol, and external API providers.

Providers fetch live game data from ESPN, The Odds API, and PandaScore,
normalizing it into ExternalGame objects for downstream matching.
"""

from __future__ import annotations

import os
import re
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
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
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
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
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

        # game_date is "YYYYMMDD" — build ISO range for the full day
        iso_date = f"{game_date[:4]}-{game_date[4:6]}-{game_date[6:8]}"
        range_start = f"{iso_date}T00:00:00Z"
        range_end = f"{iso_date}T23:59:59Z"

        url = f"{self.BASE_URL}/{sport}/matches"
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
                resp = await client.get(
                    url,
                    params={
                        "range[scheduled_at]": f"{range_start},{range_end}",
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


# ── Source Map & Resolver ─────────────────────────────────────────

SOURCE_MAP: dict[str, tuple[str, str, str]] = {
    # ESPN — major US leagues
    "KXNHLGAME": ("espn", "hockey", "nhl"),
    "KXNBAGAME": ("espn", "basketball", "nba"),
    "KXMLBGAME": ("espn", "baseball", "mlb"),
    "KXNFLGAME": ("espn", "football", "nfl"),
    "KXWNBAGAME": ("espn", "basketball", "wnba"),
    "KXCFBGAME": ("espn", "football", "college-football"),
    "KXCBBGAME": ("espn", "basketball", "mens-college-basketball"),
    "KXMLSGAME": ("espn", "soccer", "usa.1"),
    "KXEPLGAME": ("espn", "soccer", "eng.1"),
    # The Odds API — minor leagues
    "KXAHLGAME": ("odds-api", "icehockey_ahl", "icehockey_ahl"),
    # Tennis — tournament keys rotate; map active ones here
    # "KXATPMATCH": ("odds-api", "tennis_atp_indian_wells", "tennis_atp_indian_wells"),
    # "KXWTAMATCH": ("odds-api", "tennis_wta_indian_wells", "tennis_wta_indian_wells"),
    # ATP/WTA Challenger not covered by any free API yet
    # PandaScore — esports
    "KXLOLGAME": ("pandascore", "lol", "league-of-legends"),
    "KXCS2GAME": ("pandascore", "csgo", "cs2"),
    "KXVALGAME": ("pandascore", "valorant", "valorant"),
    "KXDOTA2GAME": ("pandascore", "dota2", "dota-2"),
    "KXCODGAME": ("pandascore", "codmw", "cod-mw"),
}

_MONTH_MAP: dict[str, str] = {
    "JAN": "01",
    "FEB": "02",
    "MAR": "03",
    "APR": "04",
    "MAY": "05",
    "JUN": "06",
    "JUL": "07",
    "AUG": "08",
    "SEP": "09",
    "OCT": "10",
    "NOV": "11",
    "DEC": "12",
}


def _extract_date_from_ticker(event_ticker: str) -> str | None:
    """Parse 'KXNHL-26MAR14-BOS-NYR' -> '20260314'."""
    parts = event_ticker.split("-")
    if len(parts) < 2:
        return None
    segment = parts[1]
    m = re.match(r"(\d{2})([A-Z]{3})(\d{1,2})", segment)
    if not m:
        return None
    year_suffix, month_abbr, day = m.group(1), m.group(2), m.group(3)
    month_num = _MONTH_MAP.get(month_abbr)
    if month_num is None:
        return None
    return f"20{year_suffix}{month_num}{int(day):02d}"


class GameStatusResolver:
    """Resolves Kalshi event tickers to live game status via external providers."""

    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        self._providers: dict[str, GameStatusProvider] = {
            "espn": EspnProvider(),
            "odds-api": OddsApiProvider(),
            "pandascore": PandaScoreProvider(),
        }
        self._http = http
        # Cache: event_ticker -> (status, team_codes, source_key)
        self._cache: dict[str, tuple[GameStatus, tuple[str, str] | None, str]] = {}

    @staticmethod
    def extract_team_codes(event_ticker: str) -> tuple[str, str] | None:
        """Extract team codes from ticker: 'KXNHL-26MAR14-BOS-NYR' -> ('BOS', 'NYR')."""
        parts = event_ticker.split("-")
        if len(parts) < 4:
            return None
        code_a, code_b = parts[-2], parts[-1]
        pattern = re.compile(r"^[A-Z]{2,4}$")
        if pattern.match(code_a) and pattern.match(code_b):
            return (code_a, code_b)
        return None

    @staticmethod
    def extract_from_subtitle(sub_title: str) -> tuple[str, str] | None:
        """Extract team names from subtitle: 'WAKE at VT (Mar 10)' -> ('WAKE', 'VT')."""
        # Strip date suffix in parens
        stripped = re.sub(r"\s*\([^)]*\)\s*$", "", sub_title).strip()
        for sep in (" at ", " vs ", " vs. "):
            if sep in stripped:
                team_a, team_b = stripped.split(sep, 1)
                return (team_a.strip().upper(), team_b.strip().upper())
        return None

    @staticmethod
    def match_game(
        team_codes: tuple[str, str], games: list[ExternalGame]
    ) -> ExternalGame | None:
        """Match team codes to an ExternalGame, first by abbreviation then by substring."""
        code_set = {team_codes[0].upper(), team_codes[1].upper()}

        # Strategy 1: abbreviation match
        for game in games:
            abbrs = set()
            if game.home_abbr:
                abbrs.add(game.home_abbr.upper())
            if game.away_abbr:
                abbrs.add(game.away_abbr.upper())
            if code_set == abbrs:
                return game

        # Strategy 2: substring fallback
        for game in games:
            combined = f"{game.home_team} {game.away_team}".upper()
            if all(code in combined for code in code_set):
                return game

        return None

    def _prepare_entry(
        self, event_ticker: str, sub_title: str = ""
    ) -> tuple[str, str, str, tuple[str, str] | None, str] | None:
        """Extract source info and team codes for an event ticker.

        Returns (prefix, sport, league, team_codes, game_date) or None.
        """
        prefix = event_ticker.split("-")[0]
        source = SOURCE_MAP.get(prefix)
        if source is None:
            logger.warning(
                "unmapped_series_ticker", prefix=prefix, ticker=event_ticker
            )
            self._cache[event_ticker] = (GameStatus(state="unknown"), None, "")
            return None

        _, sport, league = source
        team_codes = self.extract_team_codes(event_ticker)
        if team_codes is None and sub_title:
            team_codes = self.extract_from_subtitle(sub_title)

        game_date = _extract_date_from_ticker(event_ticker)
        if game_date is None:
            game_date = datetime.now(UTC).strftime("%Y%m%d")

        return (prefix, sport, league, team_codes, game_date)

    async def resolve(
        self, event_ticker: str, sub_title: str = ""
    ) -> GameStatus:
        """Resolve a single Kalshi event ticker to a GameStatus."""
        result = await self.resolve_batch([(event_ticker, sub_title)])
        return result.get(event_ticker, GameStatus(state="unknown"))

    async def resolve_batch(
        self, items: list[tuple[str, str]]
    ) -> dict[str, GameStatus]:
        """Resolve multiple event tickers, batching API calls by source.

        Each unique (provider, sport, league, date) combination makes
        exactly one API call, regardless of how many games share that source.
        """
        # Group events by their API source key
        # source_key = (prefix, sport, league, game_date)
        groups: dict[tuple[str, str, str, str], list[tuple[str, tuple[str, str] | None]]] = {}
        results: dict[str, GameStatus] = {}

        for event_ticker, sub_title in items:
            entry = self._prepare_entry(event_ticker, sub_title)
            if entry is None:
                results[event_ticker] = GameStatus(state="unknown")
                continue
            prefix, sport, league, team_codes, game_date = entry
            source_key = (prefix, sport, league, game_date)
            groups.setdefault(source_key, []).append((event_ticker, team_codes))

        # One API call per unique source
        for (prefix, sport, league, game_date), event_list in groups.items():
            source = SOURCE_MAP.get(prefix)
            if source is None:
                continue
            provider_name = source[0]
            provider = self._providers.get(provider_name)
            if provider is None:
                continue

            try:
                games = await provider.fetch_games(sport, league, game_date)
            except Exception:
                logger.warning("batch_fetch_failed", prefix=prefix)
                for event_ticker, team_codes in event_list:
                    if event_ticker not in self._cache:
                        status = GameStatus(state="unknown")
                        self._cache[event_ticker] = (status, team_codes, prefix)
                        results[event_ticker] = status
                continue

            # Match each event against the fetched games
            for event_ticker, team_codes in event_list:
                if not games or team_codes is None:
                    status = GameStatus(state="unknown")
                else:
                    matched = self.match_game(team_codes, games)
                    if matched is None:
                        status = GameStatus(state="unknown")
                    else:
                        status = GameStatus(
                            state=matched.state,
                            scheduled_start=matched.scheduled_start,
                            detail=matched.detail,
                        )
                self._cache[event_ticker] = (status, team_codes, prefix)
                results[event_ticker] = status

        return results

    async def refresh_all(self) -> None:
        """Re-fetch all cached entries, batched by source."""
        # Group cached entries by source
        groups: dict[tuple[str, str, str, str], list[tuple[str, tuple[str, str]]]] = {}
        for event_ticker, (_, team_codes, prefix) in list(self._cache.items()):
            source = SOURCE_MAP.get(prefix)
            if source is None or team_codes is None:
                continue
            _, sport, league = source
            game_date = _extract_date_from_ticker(event_ticker)
            if game_date is None:
                game_date = datetime.now(UTC).strftime("%Y%m%d")
            key = (prefix, sport, league, game_date)
            groups.setdefault(key, []).append((event_ticker, team_codes))

        # One API call per unique source
        for (prefix, sport, league, game_date), event_list in groups.items():
            source = SOURCE_MAP.get(prefix)
            if source is None:
                continue
            provider_name = source[0]
            provider = self._providers.get(provider_name)
            if provider is None:
                continue

            try:
                games = await provider.fetch_games(sport, league, game_date)
            except Exception:
                logger.warning("refresh_fetch_failed", prefix=prefix)
                continue

            if not games:
                continue  # Keep stale cached values

            for event_ticker, team_codes in event_list:
                matched = self.match_game(team_codes, games)
                if matched is not None:
                    new_status = GameStatus(
                        state=matched.state,
                        scheduled_start=matched.scheduled_start,
                        detail=matched.detail,
                    )
                    self._cache[event_ticker] = (new_status, team_codes, prefix)
                # If no match, keep stale cached value

    def get(self, event_ticker: str) -> GameStatus | None:
        """Read cached status for an event ticker."""
        entry = self._cache.get(event_ticker)
        if entry is None:
            return None
        return entry[0]

    def remove(self, event_ticker: str) -> None:
        """Remove an event ticker from cache."""
        self._cache.pop(event_ticker, None)
