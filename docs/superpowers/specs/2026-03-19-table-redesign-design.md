# Table Redesign: Two-Row Layout with Trust Signals

**Date:** 2026-03-19
**Status:** Approved

## Problem

The main OpportunitiesTable has 20 columns crammed into a single row per event. This causes:
- Important info drowns in noise — hard to spot what needs attention
- A/B column duplication (NO-A/NO-B, V-A/V-B, Pos-A/Pos-B, Q-A/Q-B, CPM-A/CPM-B, ETA-A/ETA-B) — repetitive and hard to scan
- No way to tell if displayed prices are fresh without manually checking Kalshi
- No way to tell if Talos should be acting but isn't (missed re-entries, imbalanced positions)

## Solution

### 1. Two Rows Per Event

Each event renders as a pair of rows in the DataTable, one per leg/team. Inspired by sports odds tracker layout where each team gets its own row.

**Row 1 (team A):** Full team name, league, game status, NO price, volume, position, queue, CPM, ETA, edge, status, locked in, exposure
**Row 2 (team B):** Full team name, NO price, volume, position, queue, CPM, ETA — shared columns left blank

**Visual grouping:**
- Alternating pair backgrounds (shaded / unshaded)
- Bottom separator line on row 2 to delineate event boundaries
- Row keys: `{event_ticker}:a` and `{event_ticker}:b`

**Team names:** Full team names (e.g., "Boston Bruins", not "Boston" or "BOS"). Sourced from market `title` fields (each market's title contains the team name) or `Event.sub_title` split on " vs ". Stored as per-leg labels: `{event_ticker: ("Boston Bruins", "Washington Capitals")}`. **Fallback:** If parsing fails, use market ticker suffix or the event ticker as display name.

### 2. Column Layout (14 columns, down from 20)

| # | Column | Width | Source | Rows |
|---|--------|-------|--------|------|
| 1 | (dot) | 2 | orderbook last_update | Both |
| 2 | Team | auto | market title / sub_title | Both |
| 3 | Lg | 5 | series ticker lookup | Row 1 only |
| 4 | Game | 9 | game status resolver | Row 1 only |
| 5 | NO | 5 | scanner snapshot | Both |
| 6 | Vol | 6 | volumes_24h | Both |
| 7 | Pos | 14 | position summary | Both |
| 8 | Queue | 6 | position summary | Both |
| 9 | CPM | 8 | CPM tracker | Both |
| 10 | ETA | 7 | CPM tracker | Both |
| 11 | Edge | 6 | scanner snapshot | Row 1 only |
| 12 | Status | 16 | position summary | Row 1 only |
| 13 | Locked | 10 | position summary (locked_profit_cents) | Row 1 only |
| 14 | Exposure | 10 | position summary (exposure_cents) | Row 1 only |

**Dropped columns:** Sport (league is sufficient), Date (Game column shows time/live status), all A/B duplicates.

Note: 14 columns total (Locked and Exposure replace the single P&L column).

### 3. Freshness Indicator

A colored dot in column 1 of each row, showing how recently the WS orderbook received an update for that specific market ticker:

- **Green dot** (< 5 seconds) — data is live, trust it
- **Yellow dot** (5-30 seconds) — warming/reconnecting, use with caution
- **Red dot** (30+ seconds) — stale, don't trust these prices

**Implementation:** The orderbook already tracks `last_update: float` (unix timestamp) per `LocalOrderBook`. The table reads `time.monotonic() - last_update` at render time to pick the dot color. No type change needed — use existing `float` timestamps.

**Fallback:** If no WS data has ever been received for a market (e.g., just added), show a dim dot (no color) rather than red, to distinguish "never connected" from "was connected and went stale".

### 4. Locked In & Exposure (replacing P&L)

Per-event, row 1 only:
- **Locked** — Net guaranteed profit from matched pairs (both sides filled). Green when positive. From `locked_profit_cents`.
- **Exposure** — Cost of unmatched fills at risk. If 5 filled on side A, 2 on side B, exposure = cost of 3 unmatched A contracts. From `exposure_cents`. Always shown in red (any exposure is risk worth highlighting). Zero exposure shows as dim dash.

### 5. Summary Panel

New panel below the table, to the left of the Activity Log. Replaces or extends the existing AccountPanel.

```
PORTFOLIO

Cash:       $1,234.56
Locked In:  $12.40
Exposure:   $8.20
Invested:   $156.80
───────────────────
Today:      $6.40 (4.1%)
Yesterday:  $3.20 (2.8%)
Last 7d:   $28.60 (3.5%)
```

- **Cash** — account balance from Kalshi API
- **Locked In** — sum of locked_profit_cents across all active events
- **Exposure** — sum of exposure_cents across all active events
- **Invested** — total cost of all filled bids across active events
- **Today/Yesterday/7d** — P&L from settled events only (Kalshi `determined`/`settled`), using midnight PT as day boundary. ROI in parentheses = P&L / total cost of filled bids for settled events in that time window.

**Data source for historical P&L:** Kalshi's `GET /portfolio/settlements` endpoint. Kalshi is the source of truth for what was actually paid out. Settlement records include `settled_time`, `revenue`, and `event_ticker` — group by PT day boundary for time-windowed display.

**Invested calculation:** Sum of `(fill_price * quantity)` for all filled orders across active (unsettled) events. This represents current capital at work.

### 6. Internal P&L Tracking & Reconciliation

Track our own computed expected P&L per event based on fill records (prices and quantities we recorded). When Kalshi settles an event, compare:

- **Our calculated P&L** — from our fill records
- **Kalshi's reported P&L** — from settlement data

Discrepancies are logged to the Activity Log as warnings. Stored in the database for later review.

### 7. Per-Event Historical View

A detail screen (separate from the main table) showing per-event history:
- All fills with timestamps and prices
- Our calculated expected P&L
- Kalshi's reported P&L (once settled)
- ROI for settled events: `$6.40 (5.2%)`
- Discrepancy flag if our calculation doesn't match Kalshi's

Accessible via a keybinding or row action (design TBD during implementation planning). This is a later-phase feature — the data collection infrastructure (internal fill tracking, settlement comparison) should be built first, with the UI screen added after.

## Files Affected

- `src/talos/ui/widgets.py` — OpportunitiesTable rewrite (two-row rendering, freshness dots, new columns), AccountPanel update to summary panel
- `src/talos/ui/app.py` — Wiring for new panel data, updated row selection handling for two-row layout
- `src/talos/orderbook.py` — Add per-ticker `last_update_time` tracking
- `src/talos/models/position.py` — Ensure `locked_profit_cents` and `exposure_cents` are available (may already exist)
- `src/talos/game_manager.py` — Per-leg team name extraction and storage
- New: settlement tracking module for historical P&L
- New: per-event detail screen
- `tests/` — Updated table tests for two-row layout

## Out of Scope

- Sorting behavior for two-row layout (follow-up)
- Keyboard navigation changes (cursor moves by event pair vs individual row — follow-up)
- Mobile/narrow terminal layout adjustments
