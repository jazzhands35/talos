# Game Status Provider — Design Spec

**Date:** 2026-03-14
**Status:** Draft

## Problem

Talos displays a "Closes" column (market close time from Kalshi) which is useless for trading decisions. What traders need is to know when the underlying sporting event actually starts — and whether it has genuinely kicked off (not just its scheduled time). Kalshi provides no explicit "game start" field.

Coverage must include major US leagues, minor leagues (AHL), and esports — no single external API covers all of these.

## Solution

A multi-source game status system that:

1. Maps Kalshi series tickers to external sports data APIs
2. Fetches game schedules and live status from the appropriate source
3. Matches Kalshi events to external games by team abbreviations + date
4. Displays two new columns in the OpportunitiesTable: **Date** and **Status** (replacing "Closes")

All times are converted to **Pacific Time** before display or further processing.

## Data Model

All models are Pydantic `BaseModel` subclasses, consistent with codebase convention.

### GameStatus

Stored in a separate lookup dict on `GameStatusResolver`, keyed by `event_ticker`. Populated at add-time, refreshed hourly. The UI reads from the resolver's cache, not from `ArbPair` or `Opportunity` directly.

```python
class GameStatus(BaseModel):
    state: str                        # "pre" | "live" | "post" | "unknown"
    scheduled_start: datetime | None  # from external API, in UTC
    detail: str = ""                  # e.g. "Q2 4:32", "P1 12:00", "FINAL"
```

### ExternalGame

Intermediate representation returned by each provider. Normalized across all sources.

```python
class ExternalGame(BaseModel):
    home_team: str            # "Boston Bruins"
    away_team: str            # "New York Rangers"
    home_abbr: str | None = None  # "BOS" — ESPN has this, others may not
    away_abbr: str | None = None  # "NYR"
    scheduled_start: datetime # UTC
    state: str                # "pre" | "live" | "post"
    detail: str = ""          # "Q2 4:32", "P1 12:00", ""
```

### UI Display

Two columns replace the single "Closes" column:

| Column | Width | Example | Description |
|--------|-------|---------|-------------|
| Date   | 6     | `03/14` | Game date in Pacific Time (MM/DD) |
| Status | 9     | `2:30 PM` / `in 12m` / `LIVE P1` / `FINAL` / `—` | Game status in Pacific Time |

Display rules (all times Pacific):

| State | Condition | Display | Color |
|-------|-----------|---------|-------|
| `pre` | > 15 min to start | `2:30 PM` | white |
| `pre` | <= 15 min to start | `in 12m` | yellow |
| `live` | — | `LIVE P1` (or `LIVE` if no detail) | green |
| `post` | — | `FINAL` | dim/grey |
| `unknown` | — | `—` | dim |

## Provider Architecture

### Protocol

```python
class GameStatusProvider(Protocol):
    async def fetch_games(
        self, sport: str, league: str, game_date: date
    ) -> list[ExternalGame]: ...
```

Each provider fetches ALL games for a sport/league/date combo and returns normalized `ExternalGame` objects.

### ESPN Provider

- **Endpoint:** `https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard?dates={YYYYMMDD}`
- **Auth:** None
- **Coverage:** NFL, NBA, MLB, NHL, WNBA, MLS, college football/basketball/hockey, international soccer (EPL, La Liga, etc.)
- **Status mapping:**
  - `status.type.state == "pre"` → `"pre"`
  - `status.type.state == "in"` → `"live"` (game has actually started)
  - `status.type.state == "post"` → `"post"`
- **Detail:** `status.displayClock` + period info
- **Team abbrs:** Available in `competitor.team.abbreviation`
- **Limitations:** No AHL, no esports. Unofficial/undocumented API — can change without notice.

### The Odds API Provider

- **Endpoint:** `https://api.the-odds-api.com/v4/sports/{sport_key}/scores/?apiKey={key}&daysFrom=1`
- **Auth:** API key via `ODDS_API_KEY` env var (free tier: 500 requests/month)
- **Coverage:** AHL, minor league baseball, Finnish/Swedish hockey leagues, 200+ leagues total
- **Status mapping:**
  - `completed == false` and `commence_time > now` → `"pre"`
  - `completed == false` and `commence_time <= now` and scores present → `"live"`
  - `completed == true` → `"post"`
- **Detail:** Score data if available, otherwise empty
- **Team abbrs:** Not provided — match on full team names
- **Limitations:** No explicit "in-progress" flag. `commence_time` is scheduled, not actual. Infer live from commence_time + scores present. This is a **heuristic** — a delayed game could show "live" before it actually starts. Acceptable for PoC; future work can cross-reference multiple sources for confirmation.

### PandaScore Provider

- **Endpoint:** `https://api.pandascore.co/{videogame}/matches?filter[scheduled_at]={date}&sort=scheduled_at`
- **Auth:** Bearer token via `PANDASCORE_TOKEN` env var (free tier)
- **Coverage:** 13 esports titles — LoL, CS2, Valorant, Dota2, Overwatch, R6, Rocket League, etc.
- **Status mapping:**
  - `status == "not_started"` → `"pre"`
  - `status == "running"` → `"live"` (`begin_at` field = actual start time)
  - `status == "finished"` → `"post"`
  - `status == "canceled"` / `"postponed"` → `"post"` (with detail)
- **Detail:** Game number if in series (e.g. "Game 2 of 3")
- **Team abbrs:** Available as `opponent.acronym`
- **Limitations:** Free tier rate limits. Esports only.

## Source Mapping

The resolver extracts the series prefix from `event_ticker` by splitting on `-` and taking the first segment (e.g., `KXNHL-26MAR14-BOS-NYR` → `KXNHL`). This prefix is used to look up the source mapping.

A module-level constant maps Kalshi series ticker prefixes to (provider, sport, league):

```python
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
```

Unmapped series tickers produce `GameStatus(state="unknown")`.

This mapping will grow over time. When a Kalshi series is encountered that isn't mapped, log a warning so the operator knows to add it.

## Team Matching

### From Kalshi Event Ticker

Kalshi event tickers encode team information, but the format varies. The resolver uses a multi-strategy extraction approach:

**Strategy 1 — Suffix team codes:** Split on `-`, take the last two segments if they look like team abbreviations (2-4 uppercase letters). Examples:
- `KXNHL-26MAR14-BOS-NYR` → teams: BOS, NYR
- `KXNBA-26MAR14-LAL-GSW` → teams: LAL, GSW

**Strategy 2 — Event sub_title parsing:** If the ticker doesn't yield valid team codes, parse the `Event.sub_title` field (e.g., `"WAKE at VT (Mar 10)"`) to extract team names/abbreviations. This is the primary strategy for esports and any non-standard ticker formats.

**Strategy 3 — No extraction possible:** Return `GameStatus(state="unknown")`.

The extraction strategy is determined at add-time when the full `Event` object is available. The resolver stores the extracted team identifiers alongside the cached `GameStatus` so subsequent refreshes don't need the `Event` object.

### Matching Algorithm

1. **Abbreviation match (preferred):** Compare extracted team codes against `home_abbr`/`away_abbr` from the external API. Both must match (order-independent).
2. **Substring fallback:** If abbreviations aren't available (The Odds API), check if the team codes appear as substrings in full team names. E.g., "BOS" in "Boston Bruins".
3. **No match:** Return `GameStatus(state="unknown")`.

### Esports

Esports event tickers may not follow the `TEAM1-TEAM2` pattern. For esports, the resolver uses Strategy 2 (sub_title parsing) and matches against PandaScore's `opponent.acronym` field. The exact sub_title format for Kalshi esports events needs to be inspected during implementation to finalize parsing — if the format is too unpredictable, we fall back to `state="unknown"` and iterate.

## Integration

### Fetch Timing

- **On add:** When `GameManager.add_game()` adds a pair, immediately fetch game status
- **Hourly refresh:** A simple `asyncio` task in the engine calls `resolver.refresh_all()` every 60 minutes
- **Future:** Increase polling frequency as game start time approaches (not in this PoC)
- **UX note:** With hourly refresh, a game could jump from showing "2:30 PM" directly to "LIVE" if the refresh lands after game start. Acceptable for PoC.

### Module Structure

Flat layout, consistent with existing codebase (no new subdirectories):

```
src/talos/game_status.py       # GameStatus, ExternalGame, GameStatusProvider protocol,
                                # EspnProvider, OddsApiProvider, PandaScoreProvider,
                                # GameStatusResolver (mapping + matching + caching)
                                # SOURCE_MAP config
tests/test_game_status.py      # Unit tests for all of the above
```

All providers are small (one HTTP call + response parsing each), so a single module is appropriate. If the module grows beyond ~400 lines, split into `game_status.py` (models + resolver) and `game_providers.py` (provider implementations).

### Wiring

- `GameStatusResolver` is instantiated in the engine, creates its own `httpx.AsyncClient` (following the existing `KalshiRESTClient` pattern)
- `GameManager.add_game()` calls `resolver.resolve(event)` → resolver caches `GameStatus` keyed by `event_ticker`
- Engine hourly task calls `resolver.refresh_all()` to re-fetch all cached entries
- `OpportunitiesTable` calls `resolver.get(event_ticker)` → returns `GameStatus | None` to render Date and Status columns

### Data Flow

```
GameManager.add_game(event)
  └── resolver.resolve(event)
       ├── extract series prefix from event.event_ticker → SOURCE_MAP lookup
       ├── extract team codes from event_ticker or event.sub_title
       ├── provider.fetch_games(sport, league, date)
       ├── match game by team codes
       └── cache: {event_ticker: (GameStatus, team_codes, source_key)}

Engine._hourly_refresh()
  └── resolver.refresh_all()
       └── for each cached entry: re-fetch from provider, update GameStatus

OpportunitiesTable.refresh_from_scanner()
  └── for each opportunity:
       status = resolver.get(opp.event_ticker)  # read from cache
       render Date column from status.scheduled_start (Pacific)
       render Status column from status.state + status.detail (Pacific)
```

### API Keys

| Variable | Source | Required |
|----------|--------|----------|
| `ODDS_API_KEY` | The Odds API | Only if trading AHL/minor leagues |
| `PANDASCORE_TOKEN` | PandaScore | Only if trading esports |

ESPN requires no authentication. Missing keys cause the corresponding provider to return `state="unknown"` with a log warning — not a crash.

### Error Handling

All provider failures are swallowed and logged — never crash the app:

- **Missing API key:** Provider returns empty list, log warning once at startup
- **HTTP timeout / connection error:** Provider returns empty list, log warning with event details
- **Malformed JSON / unexpected schema:** Provider returns empty list, log error with response snippet
- **No match found:** Resolver returns `GameStatus(state="unknown")`, no log (normal for unmapped series)
- **Rate limit hit:** Provider returns empty list, log warning; resolver keeps stale cached value

The resolver always falls back to its last cached `GameStatus` if a refresh fails. If no cached value exists, returns `GameStatus(state="unknown")`.

## Testing Strategy

- **Unit tests per provider:** Mock HTTP responses, verify `ExternalGame` parsing
- **Unit tests for resolver:** Mock providers, verify team matching logic
- **Unit tests for UI formatting:** Verify display rules (color, text) for each state
- **Integration smoke test:** Hit real ESPN endpoint (no auth needed) to verify response shape hasn't changed

## Out of Scope (Future Work)

- Adaptive polling frequency (more frequent as game approaches)
- WebSocket-based live updates
- Automatic series ticker discovery (currently manual mapping)
- Behavior changes based on game status (e.g., auto-cancel orders when game starts)
- Historical game time tracking / analytics
