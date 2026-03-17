# Phase 4 — Integrate as GameStatusResolver fallback

Back to [[plans/05-expiration-start-time/overview]]

## Goal

Wire the offset-based start time estimation into `GameStatusResolver` as a fallback for events that don't match any external provider. This is the key integration point — after this phase, unmapped leagues get `scheduled_start` populated and exit-only auto-triggers.

## Changes

**`src/talos/game_status.py`** — In `GameStatusResolver`:

1. Store `expected_expiration_time` per event ticker (received from engine/ArbPair at game-add time). Add a method like `set_expiration(event_ticker, expected_expiration_time)` or accept it in the existing `add()` / registration path.

2. In `resolve_batch()` (or the per-event resolve path), after all external providers fail and the result would be `GameStatus(state="unknown")`: check if `expected_expiration_time` is available for this event. If so, call `estimate_start_time()` and return `GameStatus(state="pre", scheduled_start=estimated, detail="~est")` instead.

3. The `~est` detail marker lets the UI distinguish estimated from confirmed start times.

**`src/talos/engine.py`** — When adding a game, pass `expected_expiration_time` from the `ArbPair` to the `GameStatusResolver`. This is likely 1-2 lines in the game-add flow.

**Tests** — Integration tests verifying that an unmapped league with `expected_expiration_time` gets `state="pre"` with the computed start time, while mapped leagues still use external providers.

## Data Structures

- `GameStatusResolver._expirations: dict[str, str]` — event_ticker → expected_expiration_time
- No new models — reuses existing `GameStatus`

## Verification

### Static
- `pyright` passes
- `ruff check` clean

### Runtime
- Test: Unmapped league (e.g., `KXCBAGAME-...`) with `expected_expiration_time` → `GameStatus(state="pre", scheduled_start=computed)`
- Test: Mapped league (e.g., `KXNHLGAME-...`) still uses ESPN regardless of `expected_expiration_time`
- Test: Event with no `expected_expiration_time` → still returns `state="unknown"`
- Test: Exit-only auto-triggers for an unmapped league when computed start is within threshold
