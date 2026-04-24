# Phase 3 — Sport-specific offset table and start time computation

Back to [[plans/05-expiration-start-time/overview]]

## Goal

Create a pure function that computes `estimated_start` from `expected_expiration_time` and a sport-specific offset. This is the core logic — no I/O, fully testable.

## Changes

**`src/talos/game_status.py`** (or new `src/talos/expiration_offset.py` if it grows) — Add:

1. An offset lookup table mapping series ticker prefixes to `timedelta` offsets. Default 3h, with 5h for UFC/Boxing prefixes.
2. A pure function `estimate_start_time(expected_expiration: str, series_prefix: str) -> datetime | None` that parses the ISO timestamp, subtracts the offset, and returns the estimated start time. Returns `None` for placeholder values (midnight UTC exactly — known Boxing anomaly).

**Offset table** (from `brain/expected-expiration-research.md`):

| Prefix Pattern | Offset | Sports |
|---------------|--------|--------|
| `KXUFCFIGHT` | 5h | UFC/MMA |
| `KXBOXING` | 5h | Boxing |
| Everything else | 3h | NBA, NHL, CBA, KBL, AFL, NRL, Cricket, Lacrosse, Tennis, etc. |

**Tests** — Pure function tests with known input/output pairs from the research data.

## Data Structures

- `EXPIRATION_OFFSETS: dict[str, timedelta]` — prefix → offset mapping
- `DEFAULT_OFFSET: timedelta = timedelta(hours=3)`
- `estimate_start_time(expected_expiration: str, series_prefix: str) -> datetime | None`

## Verification

### Static
- `pyright` passes
- `ruff check` clean

### Runtime
- Test: NBA market `"2026-03-19T04:30:00Z"` with default offset → `2026-03-19T01:30:00Z`
- Test: UFC market `"2026-03-22T02:40:00Z"` with `KXUFCFIGHT` prefix → `2026-03-21T21:40:00Z`
- Test: Boxing placeholder `"2026-04-12T00:00:00Z"` (midnight UTC) → `None`
- Test: Missing/None `expected_expiration_time` → `None`
- Test: Unknown prefix → default 3h offset
