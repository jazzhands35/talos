# Trap Analysis Deep Dive

Date: 2026-03-23. Source: Clio analysis of 234 Talos outcomes.

## Core Problem

57% of all Talos trades are traps (one side fills, other doesn't). 99% of dollar losses come from traps. Solving traps is the single highest-leverage improvement.

## What We Thought vs What The Data Says

**Initial claim:** "The high-volume side is trapped 100% of the time."
**Corrected:** The unfilled side is low-volume 42% of the time, high-volume 58% of the time. Volume alone is a poor predictor of which side will fail to fill.

**Why:** `volume_a`/`volume_b` from kalshi_history.db is lifetime volume at scrape time, not orderbook depth when Talos placed its order. A market with 50K lifetime volume can have 3 resting orders right now.

## Data We Need But Don't Have

- **Orderbook depth at placement time** — Kalshi returns `yes_bid_size_fp`/`yes_ask_size_fp` but Talos doesn't save it
- **OI time series** — `market_snapshots` has the columns but always writes zero
- **Open vs close trade distinction** — can't reconstruct OI from trades

## Sequential Placement Strategy

Theory: place the harder-to-fill side first. If it fills, place the easy side. If it doesn't, walk away — no trap.

Simulated net impact: +$501.95 vs current (from $68.69 to $570.64). But this assumes we can reliably identify the "harder" side, which current data doesn't support well.

## Key Insight For Strategy Design

The right signal is probably **not** lifetime volume but **orderbook depth at the moment of placement**. This requires:
1. Fix Talos to save OI + bid/ask sizes in market_snapshots (the API returns them)
2. Collect a few weeks of enriched snapshots
3. Re-run Clio with depth features to see if they predict trap side
