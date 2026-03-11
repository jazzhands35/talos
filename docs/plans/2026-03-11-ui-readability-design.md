# UI Readability Overhaul — Design

## Problem

The Talos dashboard renders all data as plain white text. The operator must read every cell to find what matters. Key pain points:

- No visual hierarchy — edge values, P&L, and em-dashes all have equal weight
- Delta neutrality check (Pos-A vs Pos-B) requires mental math every scan
- Status column conflates system behavior with market conditions
- Full ticker names waste ~40 characters per row
- Numbers are left-aligned, making column scanning slow
- Queue position warnings (`!!`) are easy to miss

## Approach

Transform all DataTable cells from plain strings to Rich `Text` objects with color, dimming, and right-alignment. No structural changes to layout, column count, or polling.

## Design

### 1. Event Column — Short Labels

Strip the series prefix and date. Use the `sub_title` field from the API (e.g., `"Rombra vs Brady (Mar 10)"`) to extract a short human-readable label like `Rombra-Brady`.

- Fallback: unique ticker suffix (e.g., `ROMBRA`)
- Full ticker visible on row select (BidScreen modal)

### 2. Em-Dash Dimming

Every `"—"` placeholder rendered as `Text("—", style="dim")`. Pushes empty cells into the background so the operator's eye skips them.

### 3. Numeric Right-Alignment

All numeric columns right-aligned via `Text(value, justify="right")` with fixed-width padding:

- Cents: `" 8¢"` / `"79¢"` (3 chars)
- Edge: `" 2.5¢"` / `"-0.9"` (5 chars)
- P&L: `" $0.10"` / `"-$3.52"` (consistent `.2f`)

Left-aligned: Event, Status only.

### 4. Color Coding

Every color paired with a text indicator (sign/icon) for accessibility.

| Column | Condition | Color | Catppuccin Token |
|--------|-----------|-------|-----------------|
| Edge | `> 0` | Green | `#a6e3a1` |
| Edge | `≤ 0` | Dim | `#6c7086` |
| P&L | Positive | Green | `#a6e3a1` |
| P&L | Negative | Red | `#f38ba8` |
| Net/Odds | GTD | Green | `#a6e3a1` |
| Net/Odds | Underwater | Red | `#f38ba8` |
| Q-A/Q-B | `!!` (jumped) | Yellow | `#f9e2af` |
| Q-A/Q-B | Normal | Default | `#cdd6f4` |
| NO-A/NO-B | Always | Default | `#cdd6f4` |

### 5. Delta Neutrality Highlighting

Compare filled and resting counts across Pos-A and Pos-B each refresh:

| State | Visual |
|-------|--------|
| Balanced | Default text |
| Imbalanced fills | Yellow on the behind side |
| Imbalanced resting | Yellow on the side missing resting |

Yellow (not red) because imbalance during filling is expected, not an error.

### 6. Status Icons

Status reflects "What is Talos doing about this event, and why?" — a system behavior indicator for supervising automation.

| Situation | Display | Color |
|-----------|---------|-------|
| No position, edge below threshold | `○ Low edge` | Dim |
| No position, edge good but unstable | `○ Unstable` | Dim |
| No position, proposal pending | `◎ Proposed` | Blue |
| Resting, waiting for fills | `◷ Resting` | Yellow |
| Resting but jumped | `◷ Jumped A/B/AB` | Peach |
| One side filling faster | `◐ Filling B-8` | Blue |
| Both sides complete | `✓ Locked` | Green |
| Imbalance needs rebalance | `⚠ Imbalance` | Yellow |
| Discrepancy / hold | `⚠ Hold` | Red |

Dim states = "Talos is watching, not acting" (skip visually).
Colored states = "something is happening" in order of urgency.

## Column Semantics (Clarified)

The table has three conceptual groups:

1. **Market opportunity** (NO-A, NO-B, Edge) — "Should I enter/re-enter?"
2. **Position health** (Pos-A, Pos-B, P&L, Net/Odds) — "How are my existing positions doing?"
3. **Fill progress** (Q-A, Q-B, CPM-A, ETA-A, CPM-B, ETA-B) — "How are my fills progressing?"

Status spans all three: it reports Talos's current decision about the event.

## What Doesn't Change

- Column count (15)
- Layout (table top, account/orders bottom, proposals right)
- Polling intervals (0.5s scanner, 1s proposals, 3s queue, 10s account)
- Row click → BidScreen behavior
- Proposal panel styling

## Implementation Scope

- `widgets.py` — Cell formatting helpers, `refresh_from_scanner` uses Rich `Text`
- `theme.py` — No new constants needed (palette already defined)
- `engine.py` / `position_ledger.py` — Minor additions for "Jumped A/B" status computation if not already surfaced
- `top_of_market.py` — May need to expose per-side jump state for status

## Future (Approach 2 — Column Consolidation)

Deferred to a later iteration:
- Merge Q + CPM + ETA into a single `Fill-A` / `Fill-B` column
- Merge NO-A + NO-B into `NO A/B`
- Conditional column visibility (hide CPM/ETA when no positions)
