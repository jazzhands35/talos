# Scanner Tree Redesign — Phase 1 Scaffold

**Branch:** `feat/scanner-tree-redesign` → `main`
**Commits:** 32 · **Diff:** +4,670 / −71 across 40 files · **Tests:** 1,325 passing (started at 1,212; +113 new)

## Motivation

On 2026-04-15, Talos accepted fills on `KXSURVIVORMENTION-26APR16-MRBE` until 21:16 EDT — 76 minutes into an 8 PM episode. Final fill bought YES at 96¢; market resolved NO. Post-mortem revealed the `_check_exit_only` path was using `expected_expiration_time − 3h` as a proxy for event-start. For that ticker, Kalshi's expiration was 10 AM the next day, so the estimate put the "start" at 7 AM Apr 16 — nine hours after the actual 8 PM airtime. Preemptive exit-only was mathematically unreachable during the entire trading window.

Investigation surfaced Kalshi's `/milestones` endpoint — a curated, publicly-accessible schedule of event-start times with `related_event_tickers` links. That's the real source of truth, not expiration-minus-offset.

## What this branch delivers

All behavior gated behind `automation_config.tree_mode: bool = False` (default off). Phase 1 is the scaffold; no existing path changes until you flip the flag.

### New subsystems

- **`DiscoveryService`** — Kalshi catalog cache. `/series` eagerly at startup, `/events` lazily on tree-expand (5-min TTL), own 5-slot semaphore so it can't starve trading calls.
- **`MilestoneResolver`** — paginated `/milestones` index keyed by `event_ticker`, atomic-swap refresh every 5 min.
- **`TreeMetadataStore`** — typed wrapper over new `tree_metadata.json` (event-level metadata: first-seen / reviewed-at / manual_event_start / deliberately_unticked / deliberately_unticked_pending).
- **`TreeScreen`** — new Textual modal for inspecting and curating what Talos monitors. State-aware glyphs (`[ ]` / `[✓]` / `[·]` / `[W]`), keybindings for toggle (`space`) / commit (`c`) / refresh (`r`), modal `SchedulePopup` prompting for manual event-start when an event lacks a milestone.
- **Resolver cascade** in `Engine._check_exit_only`: manual override → Kalshi milestone → sports GSR → no-op (logged). Dedupes per `kalshi_event_ticker` so multi-market events flip together.

### Engine integration

- **`Engine.add_pairs_from_selection`** — mirrors the existing `add_games` orchestration including `resolve_batch()` for GSR, plus volume seeding from discovery cache.
- **`Engine.remove_pairs_from_selection`** — inventory-aware; returns structured `RemoveOutcome` per pair (`removed` / `winding_down` / `not_found` / `failed`). Persists `engine_state = "winding_down"` so wind-down state survives restart.
- **`ready_for_trading` startup gate** — first refresh tick awaits milestone load with a 30-second hard cap (per the new "Safety over speed" principle added to `brain/principles.md`).
- **Winding-down reconciliation** — engine emits `event_fully_removed` when the last pair for a Kalshi event clears; TreeScreen listens and applies deferred `[·]` flags.

### Persistence

- **`games_full.json`** schema-additive: new optional `source` ("tree" / "manual_url" / "restore" / "migration") and `engine_state` fields. The legacy `_persist_games` writer in `__main__.py` is updated to preserve both round-trip so flag-off sessions don't strip them.
- **`tree_metadata.json`** new file under `get_data_dir()`. Forward-compatible schema (missing keys backfilled with defaults).
- **`settings.json`** unchanged in format; tree filter settings live under a `tree` sub-object if/when added.

## Safety invariants enforced

1. **No event enters monitoring without a schedule source.** Commit-time validator blocks adds for events with no milestone, no manual override, and no sports GSR coverage — `SchedulePopup` forces an ISO-8601 timezone-aware time or explicit "no exit-only" opt-out before commit proceeds.
2. **Unticked-with-inventory pairs survive restart.** `engine_state = "winding_down"` persists to `games_full.json`; restore path re-adds to `_winding_down` + `_exit_only_events` before the first tick.
3. **Deferred `[·]` survives restart.** `deliberately_unticked_pending` in `tree_metadata.json` rehydrates the in-memory set on mount.
4. **Flag-off round-trip preserves new fields.** Regression test in `test_legacy_writer_roundtrip.py` proves a flag-off session can read + re-write a tree-mode-written `games_full.json` without stripping `source` or `engine_state`.
5. **Exit-only flag is pair-keyed.** `_flip_exit_only_for_key` expands the kalshi key to all sibling pairs' pair-level event_tickers, keeping `_enforce_all_exit_only`'s ledger lookups working for multi-market events.
6. **Timezone-aware manual input only.** `SchedulePopup._parse_aware_datetime` rejects naive timestamps that would poison the resolver cascade's arithmetic.

## Acceptance tests

`tests/test_survivor_replay.py`:

- Manual override for an uncurated event (KXSURVIVORMENTION) triggers exit-only at the 30-min lead time — proves the original failure mode is closed for the SURVIVOR class.
- Manual opt-out never triggers exit-only — proves the opt-out escape hatch works.
- Milestone-covered event (KXFEDMENTION) uses milestone directly — proves the resolver never falls through to the deleted expiration-fallback for milestone-covered events.

## Phase 1 is the scaffold. Phases 2–5 remain:

- **Phase 2 (Sean):** manual dogfood with `tree_mode = True` in local config.
- **Phase 3:** dual-run — alternate flag on/off sessions over a few days to validate the round-trip durability guarantee in production.
- **Phase 4:** flip `tree_mode = True` as default.
- **Phase 5 (separate PR):** delete legacy paths — `GameManager.scan_events`, `DEFAULT_NONSPORTS_CATEGORIES`, `_nonsports_max_days`, hardcoded `volume_24h > 0` gates, `_expiration_fallback`, `_check_exit_only_legacy`, the `tree_mode` flag itself.

## Included bonus: pre-existing fees refactor

Reconciled the in-progress `fees.py` refactor that was stashed during implementation. Introduces `effective_fee_rate(fee_type)` for live-fetch paths (ignores the unreliable Series.fee_multiplier field) and `coerce_persisted_fee_rate(fee_type, fee_rate)` for restore paths (validates cached rates against known constants, heals corrupt or zero values). Three call-sites in `game_manager.py` migrated.

## Spec + plan

- Design: [docs/superpowers/specs/2026-04-16-scanner-tree-redesign-design.md](specs/2026-04-16-scanner-tree-redesign-design.md) — 6 rounds of Codex review, all findings resolved.
- Implementation plan: [docs/superpowers/plans/2026-04-16-scanner-tree-redesign.md](plans/2026-04-16-scanner-tree-redesign.md) — 28 tasks, all landed.

## Codex review rounds

| Round | Findings addressed | Commits |
|---|---|---|
| 1 | Engine lifecycle vs GameManager; pair-identity model; brain/ vs get_data_dir() | Spec revisions |
| 2 | resolve_batch() preservation; RemoveOutcome struct; multi-market reconciliation | Spec revisions |
| 3 | Winding-down persistence; batch atomicity via suppress_on_change | Spec revisions |
| 4 | Legacy writer field preservation; volume seeding | Spec revisions |
| 5 | Summary drift with durability design | Spec revisions |
| Post-implementation 1 | Exit-only keying; startup gate wiring; deferred-untick persistence; keybindings | 4c41f57, 9338cea |
| Post-implementation 2 | Commit-time validator; tree state/untick representation | a6beb6d, 2f6e09d |
| Post-implementation 3 | Tz-aware input; commit() success signal | ab1ec62 |
