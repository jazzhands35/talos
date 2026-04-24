# TUI Dashboard (Layer 5) Design

**Goal:** Build a real-time Textual terminal dashboard that displays arbitrage opportunities, portfolio state, and order activity — with the ability to add games and place manual NO bids from within the app.

**Architecture:** Single-screen dashboard app built on Textual. The app is the orchestration layer — it wires Layers 1-3 together, owns the event loop, and provides the operator interface. Polling-based UI refresh (500ms for opportunities, 10s for REST data).

---

## Layout

Single screen, three vertical zones:

```
┌─────────────────────────────────────────────────────────────┐
│  ◆ TALOS              ● Connected (demo)        12:34:05 PM │  ← Header
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  Event            NO-A    NO-B   Cost  Edge  Qty  Profit    │
│  ─────────────────────────────────────────────────────────  │
│  STAN vs MIA      38¢     55¢    93¢   7¢    100  $7.00  ▸ │
│  DUKE vs UNC      42¢     52¢    94¢   6¢    250  $15.00 ▸ │
│  UCLA vs USC      48¢     51¢    99¢   1¢    75   $0.75  ▸ │
│  ARZ vs OREG      55¢     50¢    —     —     —    —        │
│                                                             │
│                    ~60% of screen height                    │
│                                                             │
├──────────────────────────┬──────────────────────────────────┤
│  ACCOUNT                 │  ORDERS                          │
│  Cash:     $1,250.00     │  12:33 BUY NO STAN  38¢ x100 ✓  │
│  Portfolio: $2,100.50    │  12:33 BUY NO MIA   55¢ x100 ✓  │
│                          │  12:30 BUY NO DUKE  42¢ x50  ◷  │
│  POSITIONS               │  12:28 BUY NO UNC   52¢ x50  ✗  │
│  STAN-NO: 100 @ 38¢     │                                  │
│  MIA-NO:  100 @ 55¢     │                                  │
├──────────────────────────┴──────────────────────────────────┤
│  [a] Add Games  [d] Remove Game  [q] Quit                  │  ← Footer
└─────────────────────────────────────────────────────────────┘
```

- **Top:** Header with app name, connection status (green/red dot), and clock
- **Middle (~60%):** Opportunities DataTable — the primary widget
- **Bottom (~30%):** Two side-by-side panels — Account (balance + positions, 40%) and Orders (recent order log, 60%)
- **Footer:** Keybinding hints

---

## Visual Style: Catppuccin Mocha

| Token | Hex | Use |
|-------|-----|-----|
| Base | `#1e1e2e` | App background |
| Surface | `#313244` | Panel backgrounds |
| Overlay | `#45475a` | Borders, separators |
| Text | `#cdd6f4` | Primary text |
| Subtext | `#a6adc8` | Secondary/dimmed text |
| Blue | `#89b4fa` | Accents, headers, selected row |
| Green | `#a6e3a1` | Positive edge, connected status, filled orders |
| Red | `#f38ba8` | Disconnected, errors, cancelled orders |
| Yellow | `#f9e2af` | Warnings, stale indicators |
| Mauve | `#cba6f7` | Buttons, interactive elements |

---

## Opportunities Table

The main DataTable widget. Row cursor navigation with keyboard + mouse.

### Columns

| Column | Source | Format | Notes |
|--------|--------|--------|-------|
| Event | `opp.event_ticker` | Short display name | e.g. "STAN vs MIA" |
| NO-A | `opp.no_a` | `XX¢` | NO ask for leg A |
| NO-B | `opp.no_b` | `XX¢` | NO ask for leg B |
| Cost | `no_a + no_b` | `XX¢` | Total cost per pair |
| Edge | `opp.raw_edge` | `X¢` | Green if > 0 |
| Qty | `opp.tradeable_qty` | Integer | Contracts available |
| Profit | `edge * qty` | `$X.XX` | Max profit at current prices |
| Action | — | `▸` indicator | Shows row is actionable |

### Behavior

- Refreshes every 500ms by reading `scanner.opportunities`
- Rows keyed by `event_ticker` — update in place, add new, remove vanished
- Games with edge > 0: normal styling with green edge text
- Games with no edge: dimmed row, dashes for edge/qty/profit/action
- Sorted: opportunities first (by edge desc), then no-edge games
- `cursor_type="row"` — arrow keys navigate, Enter opens bid modal
- `zebra_stripes=True` for readability

### Display name parsing

Event tickers like `kxncaawbgame-26mar04stanmia` need a human-readable form. The simplest approach: extract the last segment and format it. For the MVP, show the raw ticker shortened. A display name mapping can be added later when `GameManager.add_game()` returns the event title from the REST response.

---

## Modals

### Add Games Modal

Triggered by `a` key. A `ModalScreen` with:

- Title: "Add Games"
- Instructions: "Paste Kalshi game URLs or event tickers, one per line"
- `TextArea` widget for multi-line input
- Cancel / Add buttons
- On "Add": parses lines, calls `game_manager.add_games(urls)` async
- Loading indicator while REST calls in progress
- Error display for invalid URLs or non-game events (inline, modal stays open)
- On success: modal dismisses, new games appear in table

### Bid Confirmation Modal

Triggered by Enter on an opportunity row or clicking the action indicator. A `ModalScreen` with:

- Title: "Place NO Bids"
- Event name and edge summary
- Two leg lines: "BUY NO [TICKER-A] @ XX¢" and "BUY NO [TICKER-B] @ XX¢"
- Editable quantity input (defaults to `tradeable_qty`, max capped)
- Computed total cost and expected profit
- Cancel / Confirm buttons
- On "Confirm": places two `rest_client.create_order()` calls (one per leg)
- Shows result (success/failure) before closing

---

## Bottom Panels

### Account Panel (left, ~40% width)

- **Balance section:** Cash and portfolio value from `rest_client.get_balance()`
- **Positions section:** List of open NO positions from `rest_client.get_positions()`
  - Format: `TICKER-NO  QTY @ PRICE  VALUE`
  - Only shows positions with `position != 0`
- Refreshes every 10 seconds via REST polling

### Order Log Panel (right, ~60% width)

- Scrollable list of recent orders from `rest_client.get_orders()`
- Format: `HH:MM  BUY NO TICKER  PRICE x QTY  STATUS`
- Status icons: `✓` = filled (green), `◷` = open (yellow), `✗` = cancelled (red)
- Most recent at top
- Refreshes every 10 seconds via REST polling
- Orders placed through the bid modal appear immediately (optimistic insert)

---

## Application Architecture

### Startup Sequence

```python
# TalosApp.on_mount()
config = KalshiConfig.from_env()
rest = KalshiRESTClient(config)
ws = KalshiWSClient(config)
books = OrderBookManager()
feed = MarketFeed(ws, books)
scanner = ArbitrageScanner(books)
game_mgr = GameManager(rest, feed, scanner)

# Wire callback
feed.on_book_update = scanner.scan

# Start timers
set_interval(0.5, self.refresh_table)      # poll scanner.opportunities
set_interval(10.0, self.refresh_account)    # poll REST for balance/positions/orders

# Start WS connection
asyncio.create_task(feed.start())
```

### Why polling for the table?

The scanner updates on every WS delta (10-50x/second). Refreshing the DataTable on every delta would thrash rendering. Instead, the table polls `scanner.opportunities` every 500ms — fast enough to feel real-time, cheap enough to not lag.

### Module Structure

```
src/talos/
  ├── ui/
  │   ├── __init__.py
  │   ├── app.py        ← TalosApp (main Textual App, orchestration)
  │   ├── screens.py    ← AddGamesScreen, BidScreen (ModalScreens)
  │   ├── widgets.py    ← OpportunitiesTable, AccountPanel, OrderLog
  │   └── theme.py      ← Catppuccin Mocha colors + TCSS constants
  └── __main__.py       ← Entry point: python -m talos
```

### Entry Point

`python -m talos` launches the app:

```python
# src/talos/__main__.py
from talos.ui.app import TalosApp

app = TalosApp()
app.run()
```

---

## Error Handling

| Scenario | UI Behavior |
|----------|-------------|
| WS disconnected | Header shows "● Disconnected" in red. Auto-reconnect (MarketFeed handles). |
| REST error (balance/orders) | Toast notification. Panels show stale data with warning color. |
| Stale orderbook | Affected opportunity row shows yellow warning indicator. |
| Bad URL in Add Games | Inline error in modal. Modal stays open. |
| Non-game event (!=2 markets) | Inline error in modal with descriptive message. |
| Order placement failure | Bid modal shows error. Order log shows failed entry with `✗`. |

---

## Testing Plan

### UI Components (unit — no network)

| Test | Verifies |
|------|----------|
| App mounts without error | Startup wiring, widget composition |
| Table renders opportunities | Correct columns, formatting, row count |
| Table updates on refresh | New/updated/removed rows handled |
| Add Games modal opens/closes | Modal lifecycle, keyboard binding |
| Bid modal pre-fills data | Correct prices, qty from opportunity |
| Account panel renders balance | Formatting, position display |
| Order log renders orders | Status icons, sorting |
| Theme colors applied | CSS classes produce correct styles |

### Integration (with mocked REST/WS)

| Test | Verifies |
|------|----------|
| Add game flow | URL → REST → scanner pair → table row appears |
| Bid flow | Select row → modal → confirm → create_order called |
| Connection status updates | Feed connect/disconnect → header indicator changes |

Testing Textual apps uses `App.run_test()` which provides a headless pilot for simulating user interaction.
