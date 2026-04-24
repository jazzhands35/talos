"""Deep non-sports analysis — correlate wins/losses with operational factors."""

import sqlite3
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
CUTOFF = "2026-02-12T00:00:00Z"

data_db = sqlite3.connect("talos_data.db")
hist_db = sqlite3.connect("kalshi_history.db")

# Category lookup
categories = {}
for et, cat in hist_db.execute("SELECT event_ticker, category FROM events").fetchall():
    categories[et] = cat

SPORTS_PREFIXES = {
    "KXNBAGAME", "KXNHLGAME", "KXAHLGAME", "KXCBAGAME", "KXMLBGAME",
    "KXNCAAMLAXGAME", "KXKHLGAME", "KXLOLGAME", "KXCS2GAME", "KXCODGAME",
    "KXATPMATCH", "KXATPCHALLENGERMATCH", "KXWTAMATCH", "KXWTACHALLENGERMATCH",
    "KXUFCFIGHT", "KXEUROLEAGUEGAME", "KXT20MATCH", "KXWBCGAME", "KXSHLGAME",
    "KXNBLGAME", "KXDOTA2GAME", "KXBOXING", "KXBBLGAME", "KXAFLGAME",
    "KXKBLGAME", "KXWOWHOCKEY", "KXWOCURLGAME", "KXVALORANTGAME",
    "KXATPDOUBLES", "KXIWWOMEN", "KXIWMEN", "KXCRICKETODIMATCH",
    "KXNCAAMBGAME", "KXNCAAMBFIRST10", "KXWOFSKATE",
}


def is_sports(event_ticker):
    cat = categories.get(event_ticker, "")
    if cat:
        return cat == "Sports"
    prefix = event_ticker.split("-")[0] if "-" in event_ticker else event_ticker
    return prefix in SPORTS_PREFIXES


# ================================================================
# 1) Build event-level view from settlement_cache (P&L)
# ================================================================
settlements = data_db.execute("""
    SELECT ticker, event_ticker, revenue, fee_cost,
           yes_count, no_count, yes_total_cost, no_total_cost, settled_time
    FROM settlement_cache
    WHERE (yes_count > 0 OR no_count > 0) AND settled_time >= ?
""", (CUTOFF,)).fetchall()

events = defaultdict(lambda: {
    "revenue": 0, "cost": 0, "fees": 0, "yes_count": 0, "no_count": 0,
    "implicit_revenue": 0, "settled_time": "",
})
for ticker, et, rev, fees, yc, nc, yc_cost, nc_cost, st in settlements:
    if is_sports(et):
        continue
    e = events[et]
    e["revenue"] += rev
    e["cost"] += yc_cost + nc_cost
    e["fees"] += fees
    e["yes_count"] += yc
    e["no_count"] += nc
    e["implicit_revenue"] += min(yc, nc) * 100
    if st > e["settled_time"]:
        e["settled_time"] = st

for et, e in events.items():
    e["pnl"] = e["revenue"] + e["implicit_revenue"] - e["cost"] - e["fees"]

# ================================================================
# 2) Enrich with event_outcomes (traps, timing, game state)
# ================================================================
outcome_rows = data_db.execute("""
    SELECT event_ticker, trapped, trap_side, trap_delta, trap_loss,
           filled_a, filled_b, avg_price_a, avg_price_b,
           total_cost_a, total_cost_b, total_fees_a, total_fees_b,
           revenue, total_pnl, game_state_at_fill, time_to_start, fill_duration
    FROM event_outcomes
    WHERE ts >= ?
""", (CUTOFF,)).fetchall()

outcomes = {}
for r in outcome_rows:
    if is_sports(r[0]):
        continue
    outcomes[r[0]] = {
        "trapped": r[1], "trap_side": r[2], "trap_delta": r[3], "trap_loss": r[4],
        "filled_a": r[5], "filled_b": r[6],
        "avg_price_a": r[7], "avg_price_b": r[8],
        "cost_a": r[9], "cost_b": r[10],
        "fees_a": r[11], "fees_b": r[12],
        "eo_revenue": r[13], "eo_pnl": r[14],
        "game_state": r[15], "time_to_start": r[16], "fill_duration": r[17],
    }

# ================================================================
# 3) Enrich with game_adds (edge at entry, volume, fee rate)
# ================================================================
adds_rows = data_db.execute("""
    SELECT event_ticker, series_ticker, sport, league, source,
           volume_a, volume_b, fee_type, fee_rate, scheduled_start
    FROM game_adds
    WHERE ts >= ?
""", (CUTOFF,)).fetchall()

# Take the first add per event (when it was originally added)
game_adds = {}
for r in adds_rows:
    et = r[0]
    if is_sports(et) or et in game_adds:
        continue
    game_adds[et] = {
        "series": r[1], "sport": r[2], "league": r[3], "source": r[4],
        "volume_a": r[5], "volume_b": r[6],
        "fee_type": r[7], "fee_rate": r[8], "scheduled_start": r[9],
    }

# ================================================================
# 4) Enrich with fills (maker/taker, prices, queue, timing)
# ================================================================
fill_rows = data_db.execute("""
    SELECT event_ticker, ticker, side, price, count, fee_cost,
           is_taker, post_position, queue_position, time_since_order
    FROM fills
    WHERE ts >= ?
""", (CUTOFF,)).fetchall()

event_fills = defaultdict(list)
for r in fill_rows:
    et = r[0]
    if is_sports(et):
        continue
    event_fills[et].append({
        "ticker": r[1], "side": r[2], "price": r[3], "count": r[4],
        "fee_cost": r[5], "is_taker": r[6], "post_position": r[7],
        "queue_position": r[8], "time_since_order": r[9],
    })

# ================================================================
# 5) Enrich with orders (fill rate, order lifecycle)
# ================================================================
order_rows = data_db.execute("""
    SELECT event_ticker, ticker, side, action, status, price,
           initial_count, fill_count, remaining_count, maker_fill_cost, maker_fees
    FROM orders
    WHERE ts >= ?
""", (CUTOFF,)).fetchall()

event_orders = defaultdict(list)
for r in order_rows:
    et = r[0]
    if is_sports(et):
        continue
    event_orders[et].append({
        "ticker": r[1], "side": r[2], "action": r[3], "status": r[4],
        "price": r[5], "initial_count": r[6], "fill_count": r[7],
        "remaining_count": r[8], "maker_fill_cost": r[9], "maker_fees": r[10],
    })

# ================================================================
# ANALYSIS — correlate factors with winning/losing
# ================================================================

def bucket(pnl):
    if pnl > 0:
        return "WIN"
    elif pnl < 0:
        return "LOSS"
    return "FLAT"


# Collect per-event features
event_features = []
for et, e in events.items():
    pnl = e["pnl"]
    outcome = bucket(pnl)

    fills = event_fills.get(et, [])
    orders = event_orders.get(et, [])
    oc = outcomes.get(et, {})
    ga = game_adds.get(et, {})

    # Fill stats
    total_fills = len(fills)
    taker_fills = sum(1 for f in fills if f["is_taker"])
    maker_fills = total_fills - taker_fills
    taker_pct = taker_fills / total_fills * 100 if total_fills else 0
    avg_queue = (
        sum(f["queue_position"] or 0 for f in fills) / total_fills
        if total_fills else None
    )
    avg_time_since = (
        sum(f["time_since_order"] or 0 for f in fills) / total_fills
        if total_fills and any(f["time_since_order"] for f in fills)
        else None
    )

    # Order stats
    total_orders = len(orders)
    filled_orders = sum(1 for o in orders if (o["fill_count"] or 0) > 0)
    cancelled_orders = sum(1 for o in orders if o["status"] == "cancelled")
    fill_rate = filled_orders / total_orders * 100 if total_orders else 0

    # Trap
    trapped = oc.get("trapped", 0)
    trap_delta = oc.get("trap_delta", 0)

    # Timing
    time_to_start = oc.get("time_to_start")
    fill_duration = oc.get("fill_duration")
    game_state = oc.get("game_state", "")

    # Volume at add
    vol_a = ga.get("volume_a", 0) or 0
    vol_b = ga.get("volume_b", 0) or 0
    total_volume = vol_a + vol_b

    # Matched completion
    filled_a = oc.get("filled_a", 0) or 0
    filled_b = oc.get("filled_b", 0) or 0
    both_filled = filled_a > 0 and filled_b > 0
    fill_imbalance = abs(filled_a - filled_b)

    event_features.append({
        "et": et, "pnl": pnl, "outcome": outcome, "cost": e["cost"],
        "total_fills": total_fills, "taker_pct": taker_pct,
        "maker_fills": maker_fills, "taker_fills": taker_fills,
        "avg_queue": avg_queue, "avg_time_since": avg_time_since,
        "total_orders": total_orders, "fill_rate": fill_rate,
        "cancelled_orders": cancelled_orders,
        "trapped": trapped, "trap_delta": trap_delta,
        "time_to_start": time_to_start, "fill_duration": fill_duration,
        "game_state": game_state,
        "total_volume": total_volume,
        "filled_a": filled_a, "filled_b": filled_b,
        "both_filled": both_filled, "fill_imbalance": fill_imbalance,
    })


def avg(vals):
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def median(vals):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    n = len(vals)
    if n % 2 == 1:
        return vals[n // 2]
    return (vals[n // 2 - 1] + vals[n // 2]) / 2


def fmt(val, decimals=1, prefix=""):
    if val is None:
        return "---"
    return f"{prefix}{val:.{decimals}f}"


wins = [f for f in event_features if f["outcome"] == "WIN"]
losses = [f for f in event_features if f["outcome"] == "LOSS"]

print("=" * 72)
print("  DEEP CORRELATION ANALYSIS — Post-Super Bowl Non-Sports")
print("=" * 72)
print(f"  {len(wins)} wins, {len(losses)} losses, {len(event_features) - len(wins) - len(losses)} flat")
print()

# --- A) MAKER vs TAKER ---
print("  A) MAKER vs TAKER FILL COMPOSITION")
print("  " + "-" * 68)
print(f"  {'':20} {'Winners':>12} {'Losers':>12} {'Delta':>10}")
w_taker = avg([f["taker_pct"] for f in wins])
l_taker = avg([f["taker_pct"] for f in losses])
print(f"  {'Avg taker %':<20} {fmt(w_taker):>12} {fmt(l_taker):>12} {fmt(w_taker - l_taker if w_taker and l_taker else None, prefix='+' if (w_taker or 0) >= (l_taker or 0) else ''):>10}")
w_fills = avg([f["total_fills"] for f in wins])
l_fills = avg([f["total_fills"] for f in losses])
print(f"  {'Avg fills/event':<20} {fmt(w_fills):>12} {fmt(l_fills):>12}")

# Bucket: pure maker, pure taker, mixed
for label, lo, hi in [("Pure maker (0%)", -1, 1), ("Mostly maker (<30%)", 1, 30), ("Mixed (30-70%)", 30, 70), ("Mostly taker (>70%)", 70, 99), ("Pure taker (100%)", 99, 101)]:
    in_bucket = [f for f in event_features if lo < f["taker_pct"] <= hi]
    if not in_bucket:
        continue
    b_wins = sum(1 for f in in_bucket if f["outcome"] == "WIN")
    b_losses = sum(1 for f in in_bucket if f["outcome"] == "LOSS")
    b_pnl = sum(f["pnl"] for f in in_bucket)
    wr = b_wins / (b_wins + b_losses) * 100 if (b_wins + b_losses) else 0
    print(f"    {label:<25} {len(in_bucket):>4} events  {b_wins}W/{b_losses}L  WR={wr:.0f}%  P&L=${b_pnl / 100:.2f}")

# --- B) QUEUE POSITION ---
print()
print("  B) QUEUE POSITION AT FILL")
print("  " + "-" * 68)
w_queue = avg([f["avg_queue"] for f in wins if f["avg_queue"] is not None])
l_queue = avg([f["avg_queue"] for f in losses if f["avg_queue"] is not None])
print(f"  {'Avg queue pos':<20} {fmt(w_queue):>12} {fmt(l_queue):>12}")
w_queue_m = median([f["avg_queue"] for f in wins if f["avg_queue"] is not None])
l_queue_m = median([f["avg_queue"] for f in losses if f["avg_queue"] is not None])
print(f"  {'Median queue pos':<20} {fmt(w_queue_m):>12} {fmt(l_queue_m):>12}")

# --- C) TIME SINCE ORDER (how fast fills come) ---
print()
print("  C) TIME SINCE ORDER (seconds to fill)")
print("  " + "-" * 68)
w_time = avg([f["avg_time_since"] for f in wins if f["avg_time_since"] is not None])
l_time = avg([f["avg_time_since"] for f in losses if f["avg_time_since"] is not None])
w_time_m = median([f["avg_time_since"] for f in wins if f["avg_time_since"] is not None])
l_time_m = median([f["avg_time_since"] for f in losses if f["avg_time_since"] is not None])
print(f"  {'Avg time to fill':<20} {fmt(w_time):>12}s {fmt(l_time):>12}s")
print(f"  {'Median time to fill':<20} {fmt(w_time_m):>12}s {fmt(l_time_m):>12}s")

# Bucket by time to fill
for label, lo, hi in [("< 1s", 0, 1), ("1-5s", 1, 5), ("5-30s", 5, 30), ("30-120s", 30, 120), ("> 120s", 120, 999999)]:
    in_bucket = [f for f in event_features if f["avg_time_since"] is not None and lo <= f["avg_time_since"] < hi]
    if not in_bucket:
        continue
    b_wins = sum(1 for f in in_bucket if f["outcome"] == "WIN")
    b_losses = sum(1 for f in in_bucket if f["outcome"] == "LOSS")
    b_pnl = sum(f["pnl"] for f in in_bucket)
    wr = b_wins / (b_wins + b_losses) * 100 if (b_wins + b_losses) else 0
    print(f"    {label:<15} {len(in_bucket):>4} events  {b_wins}W/{b_losses}L  WR={wr:.0f}%  P&L=${b_pnl / 100:.2f}")

# --- D) TRAP vs NO-TRAP ---
print()
print("  D) TRAPPED vs COMPLETED PAIRS")
print("  " + "-" * 68)
trapped = [f for f in event_features if f["trapped"]]
not_trapped = [f for f in event_features if not f["trapped"] and f["both_filled"]]
one_sided = [f for f in event_features if not f["trapped"] and not f["both_filled"]]

for label, group in [("Trapped (1 side filled)", trapped), ("Both sides filled", not_trapped), ("One side only (not trapped)", one_sided)]:
    if not group:
        continue
    g_wins = sum(1 for f in group if f["outcome"] == "WIN")
    g_losses = sum(1 for f in group if f["outcome"] == "LOSS")
    g_pnl = sum(f["pnl"] for f in group)
    g_cost = sum(f["cost"] for f in group)
    wr = g_wins / (g_wins + g_losses) * 100 if (g_wins + g_losses) else 0
    roi = g_pnl / g_cost * 100 if g_cost else 0
    print(f"  {label:<30} {len(group):>4}  {g_wins}W/{g_losses}L  WR={wr:.0f}%  P&L=${g_pnl / 100:.2f}  ROI={roi:.1f}%")

# Trap delta distribution
print()
print("  Trap delta distribution (how many contracts short of completing):")
delta_buckets = defaultdict(lambda: {"count": 0, "pnl": 0})
for f in trapped:
    d = f["trap_delta"] or 0
    delta_buckets[d]["count"] += 1
    delta_buckets[d]["pnl"] += f["pnl"]
for d in sorted(delta_buckets):
    b = delta_buckets[d]
    print(f"    delta={d}: {b['count']} events  P&L=${b['pnl'] / 100:.2f}")

# --- E) FILL IMBALANCE ---
print()
print("  E) FILL IMBALANCE (|filled_a - filled_b|)")
print("  " + "-" * 68)
for label, lo, hi in [("Balanced (0)", -1, 1), ("Slight (1-2)", 1, 3), ("Moderate (3-5)", 3, 6), ("Heavy (6+)", 6, 999999)]:
    in_bucket = [f for f in event_features if lo < f["fill_imbalance"] <= hi]
    if not in_bucket:
        continue
    b_wins = sum(1 for f in in_bucket if f["outcome"] == "WIN")
    b_losses = sum(1 for f in in_bucket if f["outcome"] == "LOSS")
    b_pnl = sum(f["pnl"] for f in in_bucket)
    b_cost = sum(f["cost"] for f in in_bucket)
    wr = b_wins / (b_wins + b_losses) * 100 if (b_wins + b_losses) else 0
    roi = b_pnl / b_cost * 100 if b_cost else 0
    print(f"    {label:<20} {len(in_bucket):>4} events  {b_wins}W/{b_losses}L  WR={wr:.0f}%  P&L=${b_pnl / 100:.2f}  ROI={roi:.1f}%")

# --- F) VOLUME AT ENTRY ---
print()
print("  F) MARKET VOLUME AT ENTRY (volume_a + volume_b from game_adds)")
print("  " + "-" * 68)
has_vol = [f for f in event_features if f["total_volume"] > 0]
no_vol = [f for f in event_features if f["total_volume"] == 0]
print(f"  Events with volume data: {len(has_vol)}, without: {len(no_vol)}")

for label, lo, hi in [("< 100", 0, 100), ("100-500", 100, 500), ("500-2000", 500, 2000), ("2000-10000", 2000, 10000), ("> 10000", 10000, 9999999)]:
    in_bucket = [f for f in has_vol if lo <= f["total_volume"] < hi]
    if not in_bucket:
        continue
    b_wins = sum(1 for f in in_bucket if f["outcome"] == "WIN")
    b_losses = sum(1 for f in in_bucket if f["outcome"] == "LOSS")
    b_pnl = sum(f["pnl"] for f in in_bucket)
    b_cost = sum(f["cost"] for f in in_bucket)
    wr = b_wins / (b_wins + b_losses) * 100 if (b_wins + b_losses) else 0
    roi = b_pnl / b_cost * 100 if b_cost else 0
    avg_vol = avg([f["total_volume"] for f in in_bucket])
    print(f"    {label:<15} {len(in_bucket):>4} events  {b_wins}W/{b_losses}L  WR={wr:.0f}%  P&L=${b_pnl / 100:.2f}  ROI={roi:.1f}%  avg_vol={fmt(avg_vol, 0)}")

# --- G) FILL RATE (orders placed vs filled) ---
print()
print("  G) ORDER FILL RATE")
print("  " + "-" * 68)
w_fr = avg([f["fill_rate"] for f in wins if f["total_orders"] > 0])
l_fr = avg([f["fill_rate"] for f in losses if f["total_orders"] > 0])
print(f"  {'Avg fill rate':<20} {fmt(w_fr):>12}% {fmt(l_fr):>12}%")

for label, lo, hi in [("< 20%", 0, 20), ("20-40%", 20, 40), ("40-60%", 40, 60), ("60-80%", 60, 80), ("80-100%", 80, 101)]:
    in_bucket = [f for f in event_features if f["total_orders"] > 0 and lo <= f["fill_rate"] < hi]
    if not in_bucket:
        continue
    b_wins = sum(1 for f in in_bucket if f["outcome"] == "WIN")
    b_losses = sum(1 for f in in_bucket if f["outcome"] == "LOSS")
    b_pnl = sum(f["pnl"] for f in in_bucket)
    wr = b_wins / (b_wins + b_losses) * 100 if (b_wins + b_losses) else 0
    print(f"    {label:<15} {len(in_bucket):>4} events  {b_wins}W/{b_losses}L  WR={wr:.0f}%  P&L=${b_pnl / 100:.2f}")

# --- H) GAME STATE AT FILL ---
print()
print("  H) GAME STATE AT FILL")
print("  " + "-" * 68)
state_buckets = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0, "cost": 0})
for f in event_features:
    gs = f["game_state"] or "unknown"
    b = state_buckets[gs]
    if f["outcome"] == "WIN":
        b["wins"] += 1
    elif f["outcome"] == "LOSS":
        b["losses"] += 1
    b["pnl"] += f["pnl"]
    b["cost"] += f["cost"]

print(f"  {'State':<20} {'Count':>6} {'W/L':>8} {'WR':>6} {'P&L':>10} {'ROI':>7}")
for gs in sorted(state_buckets, key=lambda x: state_buckets[x]["pnl"]):
    b = state_buckets[gs]
    total = b["wins"] + b["losses"]
    wr = b["wins"] / total * 100 if total else 0
    roi = b["pnl"] / b["cost"] * 100 if b["cost"] else 0
    print(f"  {gs:<20} {b['wins'] + b['losses']:>6} {b['wins']:>3}/{b['losses']:<4} {wr:>5.0f}% ${b['pnl'] / 100:>8,.2f} {roi:>6.1f}%")

# --- I) TIME TO START (how far before scheduled event start) ---
print()
print("  I) TIME TO START (seconds between fill and event start)")
print("  " + "-" * 68)
has_tts = [f for f in event_features if f["time_to_start"] is not None and f["time_to_start"] > 0]
print(f"  Events with time_to_start data: {len(has_tts)}")
if has_tts:
    for label, lo, hi in [("< 5 min", 0, 300), ("5-30 min", 300, 1800), ("30-120 min", 1800, 7200), ("2-12 hr", 7200, 43200), ("12-24 hr", 43200, 86400), ("> 24 hr", 86400, 9999999)]:
        in_bucket = [f for f in has_tts if lo <= f["time_to_start"] < hi]
        if not in_bucket:
            continue
        b_wins = sum(1 for f in in_bucket if f["outcome"] == "WIN")
        b_losses = sum(1 for f in in_bucket if f["outcome"] == "LOSS")
        b_pnl = sum(f["pnl"] for f in in_bucket)
        b_cost = sum(f["cost"] for f in in_bucket)
        wr = b_wins / (b_wins + b_losses) * 100 if (b_wins + b_losses) else 0
        roi = b_pnl / b_cost * 100 if b_cost else 0
        print(f"    {label:<15} {len(in_bucket):>4} events  {b_wins}W/{b_losses}L  WR={wr:.0f}%  P&L=${b_pnl / 100:.2f}  ROI={roi:.1f}%")

# --- J) FILL DURATION (how long to complete all fills) ---
print()
print("  J) FILL DURATION (seconds from first to last fill)")
print("  " + "-" * 68)
has_fd = [f for f in event_features if f["fill_duration"] is not None]
print(f"  Events with fill_duration data: {len(has_fd)}")
if has_fd:
    for label, lo, hi in [("< 1s (instant)", 0, 1), ("1-10s", 1, 10), ("10-60s", 10, 60), ("1-5 min", 60, 300), ("5-30 min", 300, 1800), ("> 30 min", 1800, 9999999)]:
        in_bucket = [f for f in has_fd if lo <= (f["fill_duration"] or 0) < hi]
        if not in_bucket:
            continue
        b_wins = sum(1 for f in in_bucket if f["outcome"] == "WIN")
        b_losses = sum(1 for f in in_bucket if f["outcome"] == "LOSS")
        b_pnl = sum(f["pnl"] for f in in_bucket)
        b_cost = sum(f["cost"] for f in in_bucket)
        wr = b_wins / (b_wins + b_losses) * 100 if (b_wins + b_losses) else 0
        roi = b_pnl / b_cost * 100 if b_cost else 0
        print(f"    {label:<20} {len(in_bucket):>4} events  {b_wins}W/{b_losses}L  WR={wr:.0f}%  P&L=${b_pnl / 100:.2f}  ROI={roi:.1f}%")

# --- K) INVESTMENT SIZE ---
print()
print("  K) INVESTMENT SIZE PER EVENT")
print("  " + "-" * 68)
for label, lo, hi in [("< $5", 0, 500), ("$5-$20", 500, 2000), ("$20-$50", 2000, 5000), ("$50-$100", 5000, 10000), ("$100-$500", 10000, 50000), ("> $500", 50000, 99999999)]:
    in_bucket = [f for f in event_features if lo <= f["cost"] < hi]
    if not in_bucket:
        continue
    b_wins = sum(1 for f in in_bucket if f["outcome"] == "WIN")
    b_losses = sum(1 for f in in_bucket if f["outcome"] == "LOSS")
    b_pnl = sum(f["pnl"] for f in in_bucket)
    b_cost = sum(f["cost"] for f in in_bucket)
    wr = b_wins / (b_wins + b_losses) * 100 if (b_wins + b_losses) else 0
    roi = b_pnl / b_cost * 100 if b_cost else 0
    print(f"    {label:<15} {len(in_bucket):>4} events  {b_wins}W/{b_losses}L  WR={wr:.0f}%  P&L=${b_pnl / 100:.2f}  ROI={roi:.1f}%")

# --- L) COMPARE VS SPORTS for same factors ---
print()
print("=" * 72)
print("  SPORTS COMPARISON ON KEY FACTORS")
print("=" * 72)

# Rebuild for sports
sports_fills = defaultdict(list)
for r in fill_rows:
    et = r[0]
    if not is_sports(et):
        continue
    sports_fills[et].append({
        "is_taker": r[6], "queue_position": r[8], "time_since_order": r[9],
    })

sports_outcomes = {}
for r in outcome_rows:
    if not is_sports(r[0]):
        continue
    sports_outcomes[r[0]] = {
        "trapped": r[1], "filled_a": r[5], "filled_b": r[6],
    }

sports_settlements = data_db.execute("""
    SELECT ticker, event_ticker, revenue, fee_cost,
           yes_count, no_count, yes_total_cost, no_total_cost
    FROM settlement_cache
    WHERE (yes_count > 0 OR no_count > 0) AND settled_time >= ?
""", (CUTOFF,)).fetchall()

sports_evts = defaultdict(lambda: {"cost": 0, "pnl": 0})
for ticker, et, rev, fees, yc, nc, yc_cost, nc_cost in sports_settlements:
    if not is_sports(et):
        continue
    e = sports_evts[et]
    cost = yc_cost + nc_cost
    implicit = min(yc, nc) * 100
    e["cost"] += cost
    e["pnl"] += rev + implicit - cost - fees

s_total_fills = sum(len(v) for v in sports_fills.values())
s_taker = sum(1 for fills in sports_fills.values() for f in fills if f["is_taker"])
s_taker_pct = s_taker / s_total_fills * 100 if s_total_fills else 0

ns_total_fills = sum(len(v) for v in event_fills.values())
ns_taker = sum(1 for fills in event_fills.values() for f in fills if f["is_taker"])
ns_taker_pct = ns_taker / ns_total_fills * 100 if ns_total_fills else 0

s_trapped = sum(1 for o in sports_outcomes.values() if o["trapped"])
s_total_oc = len(sports_outcomes)
s_trap_rate = s_trapped / s_total_oc * 100 if s_total_oc else 0

ns_trapped = sum(1 for f in event_features if f["trapped"])
ns_trap_rate = ns_trapped / len(event_features) * 100 if event_features else 0

s_both = sum(1 for o in sports_outcomes.values() if (o["filled_a"] or 0) > 0 and (o["filled_b"] or 0) > 0)
s_both_rate = s_both / s_total_oc * 100 if s_total_oc else 0
ns_both = sum(1 for f in event_features if f["both_filled"])
ns_both_rate = ns_both / len(event_features) * 100 if event_features else 0

s_pnl = sum(e["pnl"] for e in sports_evts.values())
s_cost = sum(e["cost"] for e in sports_evts.values())
s_wins = sum(1 for e in sports_evts.values() if e["pnl"] > 0)
s_losses = sum(1 for e in sports_evts.values() if e["pnl"] < 0)
s_wr = s_wins / (s_wins + s_losses) * 100 if (s_wins + s_losses) else 0

ns_pnl = sum(f["pnl"] for f in event_features)
ns_cost = sum(f["cost"] for f in event_features)
ns_wins_ct = len(wins)
ns_losses_ct = len(losses)
ns_wr = ns_wins_ct / (ns_wins_ct + ns_losses_ct) * 100 if (ns_wins_ct + ns_losses_ct) else 0

print(f"  {'Metric':<25} {'Sports':>12} {'Non-Sports':>12}")
print("  " + "-" * 50)
print(f"  {'Events':<25} {len(sports_evts):>12} {len(event_features):>12}")
print(f"  {'P&L':<25} ${s_pnl / 100:>10,.2f} ${ns_pnl / 100:>10,.2f}")
print(f"  {'ROI':<25} {s_pnl / s_cost * 100 if s_cost else 0:>11.1f}% {ns_pnl / ns_cost * 100 if ns_cost else 0:>11.1f}%")
print(f"  {'Win Rate':<25} {s_wr:>11.1f}% {ns_wr:>11.1f}%")
print(f"  {'Taker fill %':<25} {s_taker_pct:>11.1f}% {ns_taker_pct:>11.1f}%")
print(f"  {'Trap rate':<25} {s_trap_rate:>11.1f}% {ns_trap_rate:>11.1f}%")
print(f"  {'Both-sides-filled rate':<25} {s_both_rate:>11.1f}% {ns_both_rate:>11.1f}%")

data_db.close()
hist_db.close()
