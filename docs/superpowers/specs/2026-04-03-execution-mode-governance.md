# Execution Mode Governance

Replaces the implicit auto-accept startup behavior with an explicit, configurable execution mode system. Separates three previously conflated concerns: startup policy, current execution state, and data health.

## Problem

Talos currently hardcodes `_start_auto_accept(168.0)` on mount (`app.py:149`), with a comment saying "24h." The architecture docs describe supervised automation where "the human decides," but the actual runtime is a semi-autonomous executor. This mismatch is dangerous now that Talos.exe is distributed to other users who read "supervised" and get automatic.

Additionally, the WS disconnect warning overwrites the execution mode display in the sub-title, making it impossible to know both "will Talos execute?" and "are the inputs trustworthy?" at the same time.

## Design

### Execution Modes

Two modes. No third "timed" mode — the timer is a property of automatic, not a separate state.

| Mode | Behavior |
|------|----------|
| **Automatic** | Proposals auto-approve as they arrive. Intended operating mode. |
| **Manual** | Manual proposal approval — operator presses `Y`/`N` on each proposal. Safety and risk-reduction flows (rebalance, catch-up, overcommit reduction) still execute automatically regardless of mode. Override/debug state. |

Automatic mode has an optional `auto_stop_at` deadline. When set, mode reverts to manual after the timer expires. When unset, automatic runs indefinitely.

**What execution mode controls:** Only proposal approval (bids, adjustments). The mode does NOT gate safety-critical auto-execution paths (rebalance, catch-up, overcommit reduction) which bypass the proposal queue and run in both modes. These paths protect capital by correcting imbalances — pausing them in manual mode would leave the operator exposed while they review proposals.

### Startup Defaults

`settings.json` gains two keys that define **boot policy** — what mode Talos starts in. These are startup defaults, not persisted runtime state. If automatic mode times out and falls back to manual mid-session, the configured default is NOT rewritten.

```json
{
  "execution_mode": "automatic",
  "auto_stop_hours": null
}
```

- `execution_mode`: `"automatic"` | `"manual"`. Factory default: `"automatic"`.
- `auto_stop_hours`: `float | null`. Optional auto-stop timer in hours. `null` = indefinite. Only meaningful when `execution_mode` is `"automatic"`.

On startup, Talos reads these and boots into the configured state. The hardcoded `_start_auto_accept(168.0)` is removed entirely.

### Status Bar

Sub-title becomes a structured bar with orthogonal dimensions, all always visible:

```
SPORTS | MODE: AUTO | DATA: LIVE | 12 accepted
SPORTS | MODE: AUTO 5:23:10 left | DATA: LIVE | 12 accepted
SPORTS | MODE: MANUAL | DATA: LIVE
SPORTS | MODE: AUTO | DATA: STALE | 12 accepted
NON-SPORTS | MODE: MANUAL | DATA: STALE
```

Components:
- **Scan mode**: `SPORTS` / `NON-SPORTS` (existing behavior, unchanged)
- **Execution mode**: `MODE: AUTO` or `MODE: AUTO {H:MM:SS} left` or `MODE: MANUAL`
- **Data health**: `DATA: LIVE` or `DATA: STALE`
- **Accepted count**: shown only in automatic mode; omitted in manual

The accepted count is **session-local** — it resets every time automatic mode is entered. It is not a lifetime counter.

### Data Health

`DATA: STALE` is driven by actual data freshness, not just WebSocket connection state. A connected socket with stale books is still stale. The freshness check should consider:

- WebSocket connected and receiving deltas → `LIVE`
- WebSocket disconnected → `STALE`
- WebSocket connected but no delta received within the staleness window (e.g., 60s since last book update across any tracked ticker) → `STALE`

The existing WS disconnect red banner stays as a visual alarm — it is not removed. But it no longer replaces the sub_title. Banner and status bar coexist: the banner is an attention-grabber, the status bar is a persistent state display.

### User Interaction

- **`F` key**: Toggles between modes. If currently automatic → switch to manual immediately. If currently manual → open modal for automatic mode (duration input with "indefinite" as default).
- **Modal**: The existing `AutoAcceptScreen` is reused. Default input changes from `2.0` to blank/indefinite. User can enter a number of hours for timed automatic, or leave blank for indefinite.
- **Timer expiry**: When `auto_stop_at` is reached, the automatic session ends and mode falls back to manual. This follows the same shutdown path as a manual `F`-key stop: log final positions via `log_session_end()`, close the JSONL session, then enter manual mode. A toast notification fires. The configured `settings.json` default is NOT modified — next restart will boot back into the configured mode.

### JSONL Logging

The existing `AutoAcceptLogger` continues to log session events in JSONL format. The log entries gain a `mode` field for clarity, but the structure is otherwise unchanged. One JSONL file per automatic session, written to `{data_dir}/auto_accept_sessions/`.

## Code Changes

### `auto_accept.py` — Modify in place (no rename)

Replace `AutoAcceptState` internals with the new model. Keep the filename to avoid churn.

```python
class Mode(Enum):
    AUTOMATIC = "automatic"
    MANUAL = "manual"

@dataclass
class ExecutionMode:
    mode: Mode = Mode.AUTOMATIC
    auto_stop_at: datetime | None = None
    accepted_count: int = 0
    started_at: datetime | None = None

    def enter_automatic(self, hours: float | None = None) -> None:
        """Enter automatic mode. hours=None means indefinite."""
        self.mode = Mode.AUTOMATIC
        self.started_at = datetime.now(UTC)
        self.accepted_count = 0
        if hours is not None:
            self.auto_stop_at = self.started_at + timedelta(hours=hours)
        else:
            self.auto_stop_at = None

    def enter_manual(self) -> None:
        """Enter manual mode."""
        self.mode = Mode.MANUAL
        self.auto_stop_at = None

    def is_expired(self) -> bool:
        """True if auto_stop_at has passed."""
        if self.auto_stop_at is None:
            return False
        return datetime.now(UTC) >= self.auto_stop_at

    @property
    def is_automatic(self) -> bool:
        return self.mode is Mode.AUTOMATIC

    # remaining_str(), elapsed_str() — same logic, adapted for auto_stop_at
```

### `app.py` — Startup and status bar

1. Remove `self._start_auto_accept(168.0)` from `on_mount`.
2. Add startup initialization that reads from settings:
   ```python
   mode = settings.get("execution_mode", "automatic")
   auto_stop_hours = settings.get("auto_stop_hours", None)
   if mode == "automatic":
       self._execution_mode.enter_automatic(hours=auto_stop_hours)
       # start JSONL logger
   else:
       self._execution_mode.enter_manual()
   ```
3. `_refresh_proposals` builds the structured sub_title:
   ```python
   parts = [mode_tag]  # SPORTS / NON-SPORTS
   if self._execution_mode.is_automatic:
       mode_str = "MODE: AUTO"
       remaining = self._execution_mode.remaining_str()
       if remaining:
           mode_str += f" {remaining} left"
       parts.append(mode_str)
   else:
       parts.append("MODE: MANUAL")
   parts.append("DATA: STALE" if self._is_data_stale() else "DATA: LIVE")
   if self._execution_mode.is_automatic:
       parts.append(f"{self._execution_mode.accepted_count} accepted")
   self.sub_title = " | ".join(parts)
   ```
4. Data staleness check:
   ```python
   def _is_data_stale(self) -> bool:
       if self._engine is None:
           return True
       if not self._engine.ws_connected:
           return True
       # Connected but no recent book data
       return self._engine.seconds_since_last_book_update() > 60.0
   ```
5. `F` key handler updated — toggle between automatic/manual.
6. Extract `_end_automatic_session()` method that: logs final positions via `log_session_end()`, clears the logger reference, calls `self._execution_mode.enter_manual()`, fires a toast. Both the `F`-key stop path and the timer expiry path call this same method. This replaces the current `_stop_auto_accept()`.
7. Timer expiry in `_auto_accept_tick`: call `self._end_automatic_session()`, do NOT write to settings.json.

### `engine.py` — Add staleness query

Add `seconds_since_last_book_update() -> float` method that checks `MarketFeed` or `OrderBookManager` for the timestamp of the most recent delta. This is a read-only query on existing state — the book manager already tracks update times for its staleness recovery logic.

### `__main__.py` — Read startup defaults

Read `execution_mode` and `auto_stop_hours` from `load_settings()` and pass to `TalosApp` or wire into the startup sequence. The existing `settings` dict already flows through — just add the two new keys.

### `persistence.py` — No changes

Existing `load_settings()` / `save_settings()` handle arbitrary dict keys. No structural changes needed.

### `automation_config.py` — No changes

`AutomationConfig` controls proposal generation thresholds (edge, stability, cooldown). Execution mode is orthogonal.

## What This Does NOT Change

- `ProposalQueue` — still the choke point. Automatic mode just approves proposals through it.
- `AutoAcceptLogger` / JSONL logging — kept as-is, still logs every auto-accepted proposal with full state snapshot.
- `AutomationConfig` — still controls proposal generation, not execution.
- Rebalance/catch-up/overcommit execution — these bypass the proposal queue and auto-execute in both modes (`engine.py:1873`, `engine.py:1944`, `rebalance.py:657`). This is intentional: they are safety/risk-reduction flows that protect capital, not speculative bids. Execution mode controls proposal approval only.
- File naming — `auto_accept.py`, `auto_accept_log.py` keep their names. The governance fix is the state model and UI semantics, not a rename.

## Testing

- Unit tests for `ExecutionMode` state transitions (enter_automatic, enter_manual, expiry, session-local counter reset)
- `_is_data_stale()` with mock engine: WS disconnected, WS connected but stale books, WS connected and fresh
- Status bar formatting: all combinations of mode x data health x timer x accepted count
- Startup: settings with `execution_mode: automatic`, `manual`, missing key (defaults to automatic)
- Timer expiry does not modify settings.json (verify file unchanged after fallback)
- `F` key toggle in both directions
