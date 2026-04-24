# Tennis Scanner Companion Tool — Design

## Problem

Talos requires manually entering event tickers to set up arbitrage games. For tennis, there's no easy way to discover all active event tickers on Kalshi. The user needs a tool that scans Kalshi and outputs tennis tickers ready for copy/paste into Talos.

## Scope

**In scope:**
- Standalone CLI tool (`tools/tennis_scanner.py`)
- Discovers all active tennis events via Kalshi's search/discovery API
- Classifies events as "match winner" vs "spread/other"
- Toggle between match-only (default) and all bet types
- Outputs event tickers + labels for copy/paste

**Out of scope:**
- Integration into Talos TUI or engine
- Auto-adding games to Talos
- Persistent state or caching
- Other sports (tennis only for now, but architecture supports extension)

## Discovery Flow

```
1. GET /search/filters_by_sports
   → Find "Tennis" in sport list
   → Extract associated series tickers

2. For each tennis series ticker:
   GET /events?series_ticker=X&status=open&with_nested_markets=true

3. Classify each event:
   - 2 markets + mutually_exclusive=true → "Match Winner"
   - Otherwise → "Spread / Other"

4. Print grouped output
```

**Fallback:** If `/search/filters_by_sports` doesn't yield usable series tickers, fetch all open events and filter by `category` field + title keywords (ATP, WTA, ITF, Challenge, tennis player name patterns).

## CLI Interface

```bash
python tools/tennis_scanner.py              # match-winner only (default)
python tools/tennis_scanner.py --all        # include spreads and other bet types
```

## Output Format

```
=== Match Winner (12 events) ===
KXATP-26MAR11-SINCAR    Sinner vs Carreño Busta (Mar 11)
KXATP-26MAR11-RUUMED    Ruud vs Medvedev (Mar 11)
...

=== Spread / Other (4 events) ===
KXATPSPREAD-26MAR11     ATP Spread: Sinner -3.5 games
...
```

Each line: `event_ticker` followed by `sub_title` (or `title` if sub_title is empty).

## Architecture

- **Single file:** `tools/tennis_scanner.py` — no package, just a script
- **Reuses Talos infrastructure:** imports `talos.auth`, `talos.config`, `talos.rest_client`
- **No new dependencies** — httpx, structlog already available
- **Auth:** Same `.env` credentials as Talos

## New API Surface

The rest client needs one new method:

```python
async def get_sports_filters(self) -> dict:
    """GET /search/filters_by_sports — discover sport categories and series."""
    return await self._request("GET", "/search/filters_by_sports")
```

This is a public, unauthenticated endpoint but using the authenticated client is simplest.

## Classification Logic

Match-winner detection:
1. Event has exactly 2 nested markets
2. Event `mutually_exclusive` is True (if field available)
3. Fallback: market titles suggest head-to-head (e.g., player name patterns)

Everything else = spread/other.
