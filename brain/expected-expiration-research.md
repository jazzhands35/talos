# Expected Expiration Time Research

Research into using Kalshi's `expected_expiration_time` field as a fallback for game start times on unmapped leagues (CBA, KBL, Boxing, AFL, NRL, NCAA Lacrosse, Cricket T20).

## Key Finding

Kalshi's Market API returns `expected_expiration_time` — the estimated time the market will resolve. For sports markets, this is approximately **game start + game duration**. Subtracting a sport-specific offset gives an estimated start time.

## Field Behavior

- **Not in our Market model yet** — needs to be parsed from raw API response
- **Set at market creation, never updated** — confirmed by re-fetching 13 tickers ~2.5h apart, all identical
- **Available on every market** — universal fallback, no external API needed
- **`close_time` is useless** — always 14 days after `expected_expiration_time` (early-close buffer)

## Verified Offsets (expected_expiration - actual_start)

| Sport/League | Offset | Samples | Confidence |
|-------------|--------|---------|------------|
| NBA | 3h | 5/5 | High |
| NHL | 3h | 5/5 | High |
| NCAA Lacrosse | 3h | 5/5 | High |
| NRL Rugby | 3h | 5/5 (cross-day corrected) | High |
| AFL | 3h | 4/5 (1 had PT conversion bug, actually 3h) | High |
| ATP Tennis | 3h | 3/3 (but all same tournament day) | Medium |
| CBA Basketball | 3h | 5/5 | Medium — see caveat below |
| KBL Basketball | 3h | 1/1 | Low — only 1 sample |
| Cricket T20 | 3h | 1/1 (cross-day corrected) | Low — only 1 sample |
| UFC/MMA | 5h | 5/5 | Medium — all from one card |
| Boxing | 5h | 2/3 (Fury-Makh = placeholder 0h) | Low |

## CBA Caveat

Kalshi lists ALL CBA games on the same day with identical `expected_expiration_time` (e.g., all Mar 18 games at 14:35 UTC). Independent check via basketball24.com showed:
- Mar 18: games at 7:15 PM Beijing (not 7:35 PM)
- Mar 19: games at 8:00 PM Beijing (different from Mar 18)

Kalshi appears to use an approximate default time for CBA, not actual per-game schedules. The 3h offset still yields a reasonable estimate but won't be precise.

## Boxing Anomaly

`Fury vs Makhmudov` has `expected_expiration_time = 2026-04-12T00:00:00Z` (midnight UTC exactly). This is almost certainly a placeholder — fight is a month away. Other boxing events show 5h offset. May update closer to event date, but our re-check confirmed Kalshi doesn't update dynamically.

## Implementation

Completed as [[plans/05-expiration-start-time/overview]] (2026-03-17). Key modules: `game_status.py` (offset table + `estimate_start_time()` + `_expiration_fallback()`), `engine.py` (backfill + wiring), `widgets.py` (`~` prefix display).

## Raw Data

Collected 2026-03-16 ~15:10 UTC. Full dataset with Kalshi links in `tools/start_time_checker.html`.

### Sample expected_expiration_time values (UTC)

```
NBA  LAL at HOU     2026-03-19T04:30:00Z  (actual start: 2026-03-19T01:30:00Z, delta=3h)
NHL  DAL at COL     2026-03-19T04:30:00Z  (actual start: 2026-03-19T01:30:00Z, delta=3h)
CBA  SHA vs SHAD    2026-03-18T14:35:00Z  (actual start: 2026-03-18T11:35:00Z, delta=3h)
UFC  Evloev-Murphy  2026-03-22T02:40:00Z  (actual start: 2026-03-21T21:40:00Z, delta=5h)
BOX  Tyson-May      2026-04-26T05:00:00Z  (actual start: 2026-04-26T00:00:00Z, delta=5h)
AFL  NMK vs WCE     2026-03-22T10:10:00Z  (actual start: 2026-03-22T07:10:00Z, delta=3h)
LAX  Quin-Prov      2026-03-19T01:00:00Z  (actual start: 2026-03-18T22:00:00Z, delta=3h)
NRL  GCO at NQU     2026-03-22T10:15:00Z  (actual start: 2026-03-22T07:15:00Z, delta=3h)
```
