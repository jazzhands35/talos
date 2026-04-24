"""Game status models, protocol, and external API providers.

Providers fetch live game data from ESPN, The Odds API, and PandaScore,
normalizing it into ExternalGame objects for downstream matching.
"""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime, timedelta
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

    # Cache active tennis keys — refreshed hourly
    _tennis_keys: list[str] = []
    _tennis_keys_ts: float = 0

    @classmethod
    async def get_active_tennis_keys(cls) -> list[str]:
        """Discover active tennis tournament keys from /sports endpoint.

        This endpoint is free — doesn't count against usage quota.
        Cached for 1 hour to avoid excessive calls.
        """
        import time
        if cls._tennis_keys and (time.monotonic() - cls._tennis_keys_ts) < 3600:
            return cls._tennis_keys

        api_key = os.environ.get("ODDS_API_KEY", "")
        if not api_key:
            return []
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
                resp = await client.get(
                    f"{cls.BASE_URL}/",
                    params={"apiKey": api_key},
                )
                resp.raise_for_status()
                sports = resp.json()
                cls._tennis_keys = [
                    s["key"] for s in sports
                    if "tennis" in s.get("key", "") and s.get("active")
                ]
                cls._tennis_keys_ts = time.monotonic()
                logger.info("odds_api_tennis_keys", keys=cls._tennis_keys)
                return cls._tennis_keys
        except Exception:
            logger.warning("odds_api_tennis_discovery_failed", exc_info=True)
            return cls._tennis_keys  # return stale cache

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

    @staticmethod
    def _parse_events_response(data: list) -> list[ExternalGame]:
        """Parse the /events endpoint (free, no quota) — has commence_time but no scores."""
        games: list[ExternalGame] = []
        for item in data:
            try:
                commence = datetime.fromisoformat(
                    item["commence_time"].replace("Z", "+00:00")
                )
                now = datetime.now(UTC)
                state = "pre" if commence > now else "live"
                games.append(
                    ExternalGame(
                        home_team=item["home_team"],
                        away_team=item["away_team"],
                        scheduled_start=commence,
                        state=state,
                    )
                )
            except (KeyError, ValueError):
                pass
        return games

    async def fetch_games(
        self, sport: str, league: str, game_date: str  # noqa: ARG002
    ) -> list[ExternalGame]:
        # sport and game_date unused — Odds API uses league directly, daysFrom=1
        api_key = os.environ.get("ODDS_API_KEY", "")
        if not api_key:
            logger.warning("odds_api_key_missing")
            return []

        # For tennis, use the /events endpoint (free, no quota cost)
        if "tennis" in league:
            url = f"{self.BASE_URL}/{league}/events/"
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
                    resp = await client.get(url, params={"apiKey": api_key})
                    resp.raise_for_status()
                    return self._parse_events_response(resp.json())
            except httpx.HTTPError:
                logger.warning("odds_api_fetch_failed", league=league)
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


# ── API-Tennis Provider (Challengers) ────────────────────────────

_API_TENNIS_BASE = "https://api.api-tennis.com/tennis/"

# event_type_key values
_CHALLENGER_MEN = "281"
_CHALLENGER_WOMEN = "275"


class ApiTennisProvider:
    """Fetches tennis match data from api-tennis.com (challengers + all tours)."""

    @staticmethod
    def _parse_response(data: dict) -> list[ExternalGame]:
        games: list[ExternalGame] = []
        for item in data.get("result", []):
            if not isinstance(item, dict):
                continue
            try:
                date_str = item.get("event_date", "")
                time_str = item.get("event_time", "00:00")
                # Parse date + time into datetime (API returns in configured timezone)
                dt = datetime.fromisoformat(f"{date_str}T{time_str}:00+00:00")

                is_live = item.get("event_live", "0") == "1"
                status_raw = item.get("event_status", "")
                if status_raw == "Finished":
                    state = "post"
                elif is_live or status_raw.startswith("Set"):
                    state = "live"
                else:
                    state = "pre"

                p1 = item.get("event_first_player", "")
                p2 = item.get("event_second_player", "")

                games.append(
                    ExternalGame(
                        home_team=p1,
                        away_team=p2,
                        scheduled_start=dt,
                        state=state,
                        detail=status_raw if state == "live" else "",
                    )
                )
            except (ValueError, KeyError):
                pass
        return games

    async def fetch_games(
        self, sport: str, league: str, game_date: str  # noqa: ARG002
    ) -> list[ExternalGame]:
        api_key = os.environ.get("API_TENNIS_KEY", "")
        if not api_key:
            logger.warning("api_tennis_key_missing")
            return []

        # game_date is "YYYYMMDD", convert to "YYYY-MM-DD"
        date_fmt = f"{game_date[:4]}-{game_date[4:6]}-{game_date[6:8]}"

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
                resp = await client.get(
                    _API_TENNIS_BASE,
                    params={
                        "method": "get_fixtures",
                        "date_start": date_fmt,
                        "date_stop": date_fmt,
                        "event_type_key": league,  # e.g., "281" for challenger men
                        "APIkey": api_key,
                        "timezone": "Etc/UTC",
                    },
                )
                resp.raise_for_status()
                return self._parse_response(resp.json())
        except Exception:
            logger.warning("api_tennis_fetch_failed", league=league, exc_info=True)
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
    # ESPN — European soccer
    "KXLALIGAGAME": ("espn", "soccer", "esp.1"),
    "KXBUNDESLIGAGAME": ("espn", "soccer", "ger.1"),
    "KXSERIEAGAME": ("espn", "soccer", "ita.1"),
    "KXLIGUE1GAME": ("espn", "soccer", "fra.1"),
    "KXUCLGAME": ("espn", "soccer", "uefa.champions"),
    "KXLIGAMXGAME": ("espn", "soccer", "mex.1"),
    # ESPN — MMA
    "KXUFCFIGHT": ("espn", "mma", "ufc"),
    # The Odds API — minor/international leagues
    "KXAHLGAME": ("odds-api", "icehockey_ahl", "icehockey_ahl"),
    "KXSHLGAME": ("odds-api", "icehockey_sweden_hockey_league", "icehockey_sweden_hockey_league"),
    # Tennis — API-Tennis covers all tours (event_type_key: 265=ATP, 266=WTA, 281=Ch.M, 272=Ch.W)
    "KXATPMATCH": ("api-tennis", "tennis", "265"),
    "KXWTAMATCH": ("api-tennis", "tennis", "266"),
    "KXATPCHALLENGERMATCH": ("api-tennis", "tennis", "281"),
    "KXWTACHALLENGERMATCH": ("api-tennis", "tennis", "272"),
    "KXATPDOUBLES": ("api-tennis", "tennis", "267"),
    # PandaScore — esports
    "KXLOLGAME": ("pandascore", "lol", "league-of-legends"),
    "KXCS2GAME": ("pandascore", "csgo", "cs2"),
    "KXVALGAME": ("pandascore", "valorant", "valorant"),
    "KXDOTA2GAME": ("pandascore", "dota2", "dota-2"),
    "KXCODGAME": ("pandascore", "codmw", "cod-mw"),
}

# Tennis series resolved dynamically — tournament keys rotate
# All tennis now in SOURCE_MAP via API-Tennis — no dynamic resolution needed
TENNIS_SERIES: set[str] = set()

# Map Kalshi tennis prefix hints to Odds API key patterns
_TENNIS_KEY_HINTS = {
    "KXATPMATCH": "tennis_atp_",
    "KXWTAMATCH": "tennis_wta_",
    "KXATPCHALLENGERMATCH": "tennis_atp_",  # challengers mixed with main draw
    "KXWTACHALLENGERMATCH": "tennis_wta_",
    "KXATPDOUBLES": "tennis_atp_",
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


# ── Expiration-based start time estimation ────────────────────────

ESTIMATED_DETAIL = "~est"

# Sport-specific offsets: expected_expiration_time = game_start + offset.
# Research verified across 11 leagues — see brain/expected-expiration-research.md.
_EXPIRATION_OFFSETS: dict[str, timedelta] = {
    "KXUFCFIGHT": timedelta(hours=5),
    "KXBOXING": timedelta(hours=5),
}
_DEFAULT_OFFSET = timedelta(hours=3)

# Listed start times from external providers are earlier than actual tip-off/puck-drop.
_START_DELAYS: dict[str, timedelta] = {
    "KXNBAGAME": timedelta(minutes=10),
    "KXNHLGAME": timedelta(minutes=7),
}


def _apply_start_delay(
    scheduled_start: datetime | None, series_prefix: str
) -> datetime | None:
    """Shift a listed start time by a league-specific delay to get actual start."""
    if scheduled_start is None:
        return None
    delay = _START_DELAYS.get(series_prefix)
    if delay is None:
        return scheduled_start
    return scheduled_start + delay


def estimate_start_time(
    expected_expiration: str | None, series_prefix: str
) -> datetime | None:
    """Derive estimated game start from Kalshi's expected_expiration_time.

    Returns None if the input is missing or a known placeholder (midnight UTC).
    """
    if not expected_expiration:
        return None
    try:
        expiration = datetime.fromisoformat(expected_expiration.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    # Midnight UTC is a Boxing placeholder — not a real expiration
    if expiration.hour == 0 and expiration.minute == 0 and expiration.second == 0:
        return None
    offset = _EXPIRATION_OFFSETS.get(series_prefix, _DEFAULT_OFFSET)
    return expiration - offset


class GameStatusResolver:
    """Resolves Kalshi event tickers to live game status via external providers."""

    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        self._providers: dict[str, GameStatusProvider] = {
            "espn": EspnProvider(),
            "odds-api": OddsApiProvider(),
            "pandascore": PandaScoreProvider(),
            "api-tennis": ApiTennisProvider(),
        }
        self._http = http
        # Cache: event_ticker -> (status, team_codes, source_key)
        self._cache: dict[str, tuple[GameStatus, tuple[str, str] | None, str]] = {}
        # Expiration-based start time fallback: event_ticker -> (expected_expiration, prefix)
        self._expirations: dict[str, tuple[str, str]] = {}

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

    def set_expiration(
        self, event_ticker: str, expected_expiration_time: str | None
    ) -> None:
        """Store expected_expiration_time for expiration-based start fallback."""
        if expected_expiration_time:
            prefix = event_ticker.split("-")[0]
            self._expirations[event_ticker] = (expected_expiration_time, prefix)

    def _expiration_fallback(self, event_ticker: str) -> GameStatus:
        """Try expiration-based start estimate; return unknown if unavailable."""
        exp_data = self._expirations.get(event_ticker)
        if exp_data is not None:
            estimated = estimate_start_time(exp_data[0], exp_data[1])
            if estimated is not None:
                return GameStatus(
                    state="pre", scheduled_start=estimated, detail=ESTIMATED_DETAIL
                )
        return GameStatus(state="unknown")

    def _resolve_match(
        self,
        event_ticker: str,
        team_codes: tuple[str, str] | None,
        games: list[ExternalGame],
    ) -> GameStatus:
        """Match team codes against games; fall back to expiration estimate."""
        matched = (
            self.match_game(team_codes, games)
            if games and team_codes else None
        )
        if matched is not None:
            prefix = event_ticker.split("-")[0]
            return GameStatus(
                state=matched.state,
                scheduled_start=_apply_start_delay(matched.scheduled_start, prefix),
                detail=matched.detail,
            )
        return self._expiration_fallback(event_ticker)

    def _prepare_entry(
        self, event_ticker: str, sub_title: str = ""
    ) -> tuple[str, str, str, tuple[str, str] | None, str] | None:
        """Extract source info and team codes for an event ticker.

        Returns (prefix, sport, league, team_codes, game_date) or None.
        """
        prefix = event_ticker.split("-")[0]
        source = SOURCE_MAP.get(prefix)
        if source is None and prefix not in TENNIS_SERIES:
            status = self._expiration_fallback(event_ticker)
            if status.state == "unknown":
                logger.warning(
                    "unmapped_series_ticker", prefix=prefix, ticker=event_ticker
                )
            self._cache[event_ticker] = (status, None, "")
            return None

        if source is not None:
            _, sport, league = source
        else:
            # Tennis — use placeholder, resolved dynamically in resolve_batch
            sport = "tennis"
            league = "tennis_dynamic"
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
                # _prepare_entry may have cached a fallback (e.g. expiration estimate)
                cached = self._cache.get(event_ticker)
                results[event_ticker] = cached[0] if cached else GameStatus(state="unknown")
                continue
            prefix, sport, league, team_codes, game_date = entry
            source_key = (prefix, sport, league, game_date)
            groups.setdefault(source_key, []).append((event_ticker, team_codes))

        # One API call per unique source
        for (prefix, sport, league, game_date), event_list in groups.items():
            # Dynamic tennis resolution
            if league == "tennis_dynamic":
                provider = self._providers.get("odds-api")
                if provider is None:
                    continue
                tennis_keys = await OddsApiProvider.get_active_tennis_keys()
                hint = _TENNIS_KEY_HINTS.get(prefix, "tennis_")
                matching_keys = [k for k in tennis_keys if k.startswith(hint)]
                if not matching_keys:
                    matching_keys = tennis_keys  # fallback: search all
                all_games: list[ExternalGame] = []
                for tkey in matching_keys:
                    try:
                        tgames = await provider.fetch_games(sport, tkey, game_date)
                        all_games.extend(tgames)
                    except Exception:
                        pass
                for event_ticker, team_codes in event_list:
                    status = self._resolve_match(event_ticker, team_codes, all_games)
                    self._cache[event_ticker] = (status, team_codes, prefix)
                    results[event_ticker] = status
                continue

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
                        status = self._expiration_fallback(event_ticker)
                        self._cache[event_ticker] = (status, team_codes, prefix)
                        results[event_ticker] = status
                continue

            # Match each event against the fetched games
            for event_ticker, team_codes in event_list:
                status = self._resolve_match(event_ticker, team_codes, games)
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
                        scheduled_start=_apply_start_delay(
                            matched.scheduled_start, prefix
                        ),
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
        self._expirations.pop(event_ticker, None)
