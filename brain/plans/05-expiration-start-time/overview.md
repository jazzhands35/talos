# Expiration-Based Start Time Fallback — Overview

## Context

Talos monitors 20+ sports leagues but only has external start-time providers (ESPN, Odds API, PandaScore, API-Tennis) for ~60% of them. Unmapped leagues (CBA, KBL, AFL, NRL, Cricket T20, NCAA Lacrosse, Boxing) show "—" in the Date/Game columns, and exit-only never auto-triggers because `GameStatus.scheduled_start` is `None`.

Kalshi's Market API returns `expected_expiration_time` — the estimated market resolution time, set once at market creation. For sports markets, this is approximately **game start + game duration**. Subtracting a sport-specific offset gives an estimated start time. Research verified offsets across 11 sport/leagues — see `brain/expected-expiration-research.md`.

## Scope

**In scope:**
- Parse `expected_expiration_time` from Market API into the Market model
- Thread through ArbPair so it's available at game-add time
- Compute `estimated_start = expected_expiration - offset` as a fallback in GameStatusResolver
- Display estimated start in Date/Game columns for unmapped leagues
- Use estimated start for exit-only auto-trigger timing

**Out of scope:**
- Replacing external providers for mapped leagues (ESPN/API-Tennis remain primary)
- Dynamic offset learning — use fixed offsets from research
- Updating `expected_expiration_time` after initial fetch (Kalshi never updates it)

## Constraints

- **Fallback only:** Only fills `scheduled_start` when external providers return `state="unknown"`. Never overrides a matched external provider.
- **P14 (Parse at Boundary):** `expected_expiration_time` is parsed into the Market model at the Pydantic layer.
- **P7 (Kalshi is SoT):** The field comes directly from Kalshi — it IS authoritative data for when Kalshi expects the market to expire.
- **P22 (End-to-End):** The estimated start must be visible in the UI, not just used internally.

### Alternatives Considered

1. **Add more external providers per-league** — High effort, each league needs a separate API integration. Not scalable for 30+ leagues.
2. **Parse date from event ticker** — Already done in Event Details screen as a last resort. Only gives date, not time. Not useful for exit-only timing.
3. **Use `expected_expiration_time` offset** — Chosen. Single Kalshi field, universal across all sports, verified offsets. Approximate but sufficient for exit-only triggers (30-min window absorbs the imprecision).

### Applicable Skills

- `kalshi-api-research` — verify `expected_expiration_time` field behavior before implementation
- `safety-audit` — after changes, since exit-only timing affects order cancellation
- `superpowers:test-driven-development` — TDD throughout

## Status: COMPLETE (2026-03-17)

## Phases

1. [[plans/05-expiration-start-time/phase-1-market-model]] — DONE — Parse `expected_expiration_time` into Market model
2. [[plans/05-expiration-start-time/phase-2-threading]] — DONE — Thread through ArbPair and GameManager
3. [[plans/05-expiration-start-time/phase-3-offset-lookup]] — DONE — Sport-specific offset table and start time computation
4. [[plans/05-expiration-start-time/phase-4-resolver-fallback]] — DONE — Integrate as GameStatusResolver fallback
5. [[plans/05-expiration-start-time/phase-5-display]] — DONE — UI display for estimated start times

## Verification

```bash
.venv/Scripts/python -m pytest -v
.venv/Scripts/python -m ruff check src/ tests/
.venv/Scripts/python -m pyright
```

Runtime: add a game from an unmapped league (e.g., CBA, AFL) and verify the Date/Game columns show an estimated time instead of "—". Verify exit-only triggers ~30 min before the estimated start.
