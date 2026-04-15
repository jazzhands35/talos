"""Deep non-sports analysis v2 — join at the correct granularity.

Key insight: settlement_cache uses Kalshi event_tickers (KXHIGHTATL-26MAR23),
but event_outcomes/fills/orders use market-level tickers (KXHIGHTATL-26MAR23-B76.5).
This version works from event_outcomes as the primary source.
"""

import sqlite3
from collections import defaultdict

CUTOFF = "2026-02-12T00:00:00Z"

data_db = sqlite3.connect("talos_data.db")
hist_db = sqlite3.connect("kalshi_history.db")

# Category lookup from kalshi_history
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

TYPE_MAP = {
    "KXBTC": "Crypto", "KXBTCD": "Crypto", "KXBTCMINMON": "Crypto", "KXBTCMAXMON": "Crypto",
    "KXETH": "Crypto", "KXETHD": "Crypto", "KXETHMAXMON": "Crypto",
    "KXXRP": "Crypto", "KXXRPD": "Crypto", "KXXRPMINMON": "Crypto", "KXXRPMAXMON": "Crypto",
    "KXDOGE": "Crypto", "KXDOGED": "Crypto", "KXDOGEMAXMON": "Crypto", "KXDOGEMINMON": "Crypto",
    "KXSOLD": "Crypto", "KXSOLE": "Crypto", "KXSOLMAXMON": "Crypto", "KXSOLMINMON": "Crypto",
    "KXHYPE": "Crypto", "KXSHIBA": "Crypto", "KXSHIBAD": "Crypto",
    "KXZECMINMON": "Crypto", "KXZECMAXMON": "Crypto",
}
WEATHER_PREFIXES = (
    "KXHIGHT", "KXLOWT", "KXHIGHA", "KXHIGHC", "KXHIGHD", "KXHIGHL",
    "KXHIGHM", "KXHIGHN", "KXHIGHP", "KXHIGHS",
    "KXRAIN", "KXSNOW", "KXSEASNOW", "KXDETSNOW",
    "KXSLCSNOW", "KXPHILSNOW", "KXNYCSNOW", "KXCHISNOW", "KXBOSSNOW",
    "KXAUSSNOW", "KXDENSNOW", "KXDCSNOW", "KXDALSNOW", "KXARCTICICE", "KXTORNADO",
)
POLITICS_PREFIXES = (
    "KXTRUMP", "KXVOTE", "KXPARDON", "KXAPRPOTUS", "KXNATL", "KXICE",
    "KXFEDGOV", "KXBILLS", "KXCABLE", "KXNEWDEAL", "KXDEREMEROUT",
    "KXGABBARDOUT", "KXBONDIOUT", "KXAGANNOUNCE", "KXUSIRAN",
    "KXHORMUZ", "KXRETURN", "KXLAGODAYS", "KXWAINCOMETAX",
)
MENTION_PREFIXES = (
    "KXVANCE", "KXPELOSI", "KXBERNIE", "KXRUBIO", "KXSECPRESS",
    "KXSCOTUS", "KXBARR", "KXHOMAN", "KXSTARMER", "KXMAMDANI",
    "KXCARNEY", "KXSHIRLEY", "KXNETANYAHU", "KXINFANTINO", "KXGOVERNOR",
    "KXCONGRESS", "KXPOLITICS", "KXPERSONMENTION", "KXLEAVITT",
    "KXMELANIA", "KXDIMON", "KXHEGSETH", "KXMADDOW", "KXFOXNEWS",
)
ENTERTAINMENT_PREFIXES = (
    "KXOSCAR", "KXPERFORM", "KXSBGUEST", "KXSBAD", "KXALBUM", "KXSPOTIFY",
    "KXSNL", "KXMRBEAST", "KXTOPMODEL", "KX1SONG", "KXSURVIVOR",
    "KXMEDIAGUEST", "KXLASTWORD", "KXTRUTHSOCIAL", "KXNYTHEAD",
)
TECH_PREFIXES = (
    "KXTESLA", "KXLLM", "KXH100", "KXH200", "KXA100", "KXB200",
    "KXRTX", "KXSPACEX", "KXARTEMIS", "KXTECH", "KXFDA",
    "KXBEZEL", "KXCARTIER", "KXOMEGA", "KXTUDOR", "KXROLEX", "KXRT",
)


def series_prefix(et: str) -> str:
    """Extract the series prefix from a market-level event_ticker."""
    # KXHIGHTATL-26MAR23-B76.5 -> KXHIGHTATL
    parts = et.split("-")
    return parts[0] if parts else et


def is_sports(et: str) -> bool:
    prefix = series_prefix(et)
    # Check history DB using Kalshi event_ticker (first two parts)
    kalshi_et = "-".join(et.split("-")[:2]) if "-" in et else et
    cat = categories.get(kalshi_et, "")
    if cat:
        return cat == "Sports"
    return prefix in SPORTS_PREFIXES


def classify_type(et: str) -> str:
    prefix = series_prefix(et)
    if prefix in TYPE_MAP:
        return TYPE_MAP[prefix]
    if any(prefix.startswith(w) for w in WEATHER_PREFIXES):
        return "Weather"
    if "MENTION" in prefix:
        return "Mentions"
    if any(prefix.startswith(p) for p in POLITICS_PREFIXES):
        return "Politics"
    if any(prefix.startswith(p) for p in MENTION_PREFIXES):
        return "Mentions"
    if any(prefix.startswith(p) for p in ENTERTAINMENT_PREFIXES):
        return "Entertainment"
    if any(prefix.startswith(p) for p in TECH_PREFIXES):
        return "Tech/Science"
    if "EARN" in prefix:
        return "Earnings"
    return "Other"


# ================================================================
# Load event_outcomes — one row per market-pair tracked by Talos
# ================================================================
eo_rows = data_db.execute("""
    SELECT event_ticker, sport, league,
           filled_a, filled_b, avg_price_a, avg_price_b,
           total_cost_a, total_cost_b, total_fees_a, total_fees_b,
           result_a, result_b, revenue, total_pnl,
           trapped, trap_side, trap_delta, trap_loss,
           game_state_at_fill, time_to_start, fill_duration
    FROM event_outcomes
    WHERE ts >= ?
""", (CUTOFF,)).fetchall()

eo_data = []
for r in eo_rows:
    et = r[0]
    if is_sports(et):
        continue
    eo_data.append({
        "et": et, "sport": r[1], "league": r[2],
        "filled_a": r[3] or 0, "filled_b": r[4] or 0,
        "avg_price_a": r[5] or 0, "avg_price_b": r[6] or 0,
        "cost_a": r[7] or 0, "cost_b": r[8] or 0,
        "fees_a": r[9] or 0, "fees_b": r[10] or 0,
        "result_a": r[11] or "", "result_b": r[12] or "",
        "revenue": r[13] or 0, "pnl": r[14] or 0,
        "trapped": r[15] or 0, "trap_side": r[16] or "",
        "trap_delta": r[17] or 0, "trap_loss": r[18] or 0,
        "game_state": r[19] or "", "time_to_start": r[20],
        "fill_duration": r[21],
        "type": classify_type(et),
        "total_cost": (r[7] or 0) + (r[8] or 0),
        "total_fees": (r[9] or 0) + (r[10] or 0),
        "both_filled": (r[3] or 0) > 0 and (r[4] or 0) > 0,
        "fill_imbalance": abs((r[3] or 0) - (r[4] or 0)),
    })

# Load fills for these events
fill_rows = data_db.execute("""
    SELECT event_ticker, side, price, count, fee_cost,
           is_taker, post_position, queue_position, time_since_order
    FROM fills WHERE ts >= ?
""", (CUTOFF,)).fetchall()

event_fills = defaultdict(list)
for r in fill_rows:
    et = r[0]
    if is_sports(et):
        continue
    event_fills[et].append({
        "side": r[1], "price": r[2], "count": r[3], "fee_cost": r[4],
        "is_taker": r[5], "queue_position": r[7], "time_since_order": r[8],
    })

# Load orders
order_rows = data_db.execute("""
    SELECT event_ticker, side, action, status, price,
           initial_count, fill_count, remaining_count
    FROM orders WHERE ts >= ?
""", (CUTOFF,)).fetchall()

event_orders = defaultdict(list)
for r in order_rows:
    et = r[0]
    if is_sports(et):
        continue
    event_orders[et].append({
        "side": r[1], "action": r[2], "status": r[3], "price": r[4],
        "initial_count": r[5], "fill_count": r[6], "remaining_count": r[7],
    })

# Load game_adds
ga_rows = data_db.execute("""
    SELECT event_ticker, volume_a, volume_b, fee_rate, scheduled_start, source
    FROM game_adds WHERE ts >= ?
""", (CUTOFF,)).fetchall()

game_adds = {}
for r in ga_rows:
    et = r[0]
    if is_sports(et) or et in game_adds:
        continue
    game_adds[et] = {
        "volume_a": r[1] or 0, "volume_b": r[2] or 0,
        "fee_rate": r[3], "scheduled_start": r[4], "source": r[5],
    }

print(f"Loaded: {len(eo_data)} non-sports market-pairs from event_outcomes")
print(f"        {sum(len(v) for v in event_fills.values())} fills")
print(f"        {sum(len(v) for v in event_orders.values())} orders")
print(f"        {len(game_adds)} game_adds entries")

# ================================================================
# Enrich each event_outcome with fill/order/game_add data
# ================================================================
for eo in eo_data:
    et = eo["et"]
    fills = event_fills.get(et, [])
    orders = event_orders.get(et, [])
    ga = game_adds.get(et, {})

    eo["total_fills"] = len(fills)
    eo["taker_fills"] = sum(1 for f in fills if f["is_taker"])
    eo["maker_fills"] = eo["total_fills"] - eo["taker_fills"]
    eo["taker_pct"] = eo["taker_fills"] / eo["total_fills"] * 100 if eo["total_fills"] else 0

    queue_vals = [f["queue_position"] for f in fills if f["queue_position"] is not None]
    eo["avg_queue"] = sum(queue_vals) / len(queue_vals) if queue_vals else None

    time_vals = [f["time_since_order"] for f in fills if f["time_since_order"] is not None]
    eo["avg_time_since"] = sum(time_vals) / len(time_vals) if time_vals else None

    eo["total_orders"] = len(orders)
    filled_orders = sum(1 for o in orders if (o["fill_count"] or 0) > 0)
    eo["fill_rate"] = filled_orders / len(orders) * 100 if orders else 0

    eo["entry_volume"] = ga.get("volume_a", 0) + ga.get("volume_b", 0)
    eo["fee_rate"] = ga.get("fee_rate")
    eo["source"] = ga.get("source", "")

    eo["outcome"] = "WIN" if eo["pnl"] > 0 else ("LOSS" if eo["pnl"] < 0 else "FLAT")


# ================================================================
# Helpers
# ================================================================
def avg(vals):
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None

def median(vals):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    n = len(vals)
    return vals[n // 2] if n % 2 == 1 else (vals[n // 2 - 1] + vals[n // 2]) / 2

def fmt(val, decimals=1, prefix=""):
    if val is None:
        return "---"
    return f"{prefix}{val:.{decimals}f}"


wins = [e for e in eo_data if e["outcome"] == "WIN"]
losses = [e for e in eo_data if e["outcome"] == "LOSS"]
flat = [e for e in eo_data if e["outcome"] == "FLAT"]

total_pnl = sum(e["pnl"] for e in eo_data)
total_cost = sum(e["total_cost"] for e in eo_data)
win_pnl = sum(e["pnl"] for e in wins)
loss_pnl = sum(e["pnl"] for e in losses)

# ================================================================
# OUTPUT
# ================================================================
print()
print("=" * 72)
print("  POST-SUPER BOWL NON-SPORTS — DEEP ANALYSIS (market-pair level)")
print("=" * 72)
print()
print(f"  Market-pairs:  {len(eo_data)}  ({len(wins)}W / {len(losses)}L / {len(flat)}B)")
print(f"  Total invested: ${total_cost / 100:,.2f}")
print(f"  Net P&L:        ${total_pnl / 100:,.2f}")
print(f"  Gross wins:     ${win_pnl / 100:,.2f}    Gross losses: ${loss_pnl / 100:,.2f}")
if wins and losses:
    print(f"  Avg win:        ${win_pnl / len(wins) / 100:.2f}    Avg loss:    ${loss_pnl / len(losses) / 100:.2f}")

# --- LOSS SIZE DISTRIBUTION ---
print()
print("  LOSS SIZE DISTRIBUTION (cents)")
print("  " + "-" * 68)
for label, lo, hi in [
    ("$0 to -$0.50", 0, 50), ("-$0.50 to -$1", 50, 100), ("-$1 to -$2", 100, 200),
    ("-$2 to -$5", 200, 500), ("-$5 to -$10", 500, 1000), ("-$10 to -$25", 1000, 2500),
    ("-$25 to -$50", 2500, 5000), ("-$50 to -$100", 5000, 10000), ("-$100+", 10000, 999999),
]:
    in_b = [e for e in losses if lo < abs(e["pnl"]) <= hi]
    if lo == 0:
        in_b = [e for e in losses if abs(e["pnl"]) <= hi]
    b_loss = sum(e["pnl"] for e in in_b)
    if in_b:
        bar = "#" * min(len(in_b), 50)
        print(f"  {label:<16} {len(in_b):>5} pairs  ${b_loss / 100:>9,.2f}  {bar}")

# --- P&L BY CATEGORY ---
print()
print("  P&L BY CATEGORY")
print("  " + "-" * 68)
type_stats = defaultdict(lambda: {"pnl": 0, "cost": 0, "count": 0, "wins": 0, "losses": 0,
                                   "trapped": 0, "both_filled": 0})
for e in eo_data:
    t = e["type"]
    b = type_stats[t]
    b["pnl"] += e["pnl"]
    b["cost"] += e["total_cost"]
    b["count"] += 1
    if e["outcome"] == "WIN":
        b["wins"] += 1
    elif e["outcome"] == "LOSS":
        b["losses"] += 1
    if e["trapped"]:
        b["trapped"] += 1
    if e["both_filled"]:
        b["both_filled"] += 1

print(f"  {'Category':<17} {'Pairs':>5} {'W/L':>8} {'WR':>5} {'Invested':>10} {'P&L':>10} {'Trapped':>8} {'Both':>6}")
print("  " + "-" * 68)
for t in sorted(type_stats, key=lambda x: type_stats[x]["pnl"]):
    b = type_stats[t]
    wr = b["wins"] / (b["wins"] + b["losses"]) * 100 if (b["wins"] + b["losses"]) else 0
    trap_pct = b["trapped"] / b["count"] * 100 if b["count"] else 0
    both_pct = b["both_filled"] / b["count"] * 100 if b["count"] else 0
    print(f"  {t:<17} {b['count']:>5} {b['wins']:>3}/{b['losses']:<4} {wr:>4.0f}% ${b['cost'] / 100:>8,.2f} ${b['pnl'] / 100:>8,.2f}  {trap_pct:>5.0f}%  {both_pct:>4.0f}%")

# --- A) TRAPPED vs COMPLETED ---
print()
print("  A) TRAPPED vs COMPLETED PAIRS")
print("  " + "-" * 68)
trapped = [e for e in eo_data if e["trapped"]]
completed = [e for e in eo_data if e["both_filled"] and not e["trapped"]]
one_side = [e for e in eo_data if not e["both_filled"] and not e["trapped"]]

for label, group in [("Trapped (partial fill)", trapped), ("Completed pair", completed), ("One side only", one_side)]:
    if not group:
        print(f"  {label:<30} 0 pairs")
        continue
    g_w = sum(1 for e in group if e["outcome"] == "WIN")
    g_l = sum(1 for e in group if e["outcome"] == "LOSS")
    g_pnl = sum(e["pnl"] for e in group)
    g_cost = sum(e["total_cost"] for e in group)
    wr = g_w / (g_w + g_l) * 100 if (g_w + g_l) else 0
    roi = g_pnl / g_cost * 100 if g_cost else 0
    print(f"  {label:<30} {len(group):>5}  {g_w}W/{g_l}L  WR={wr:.0f}%  P&L=${g_pnl / 100:,.2f}  ROI={roi:.1f}%")

# Trap delta distribution
print()
print("  Trap delta (contracts short of completing):")
delta_b = defaultdict(lambda: {"count": 0, "pnl": 0, "trap_loss": 0})
for e in trapped:
    d = e["trap_delta"]
    delta_b[d]["count"] += 1
    delta_b[d]["pnl"] += e["pnl"]
    delta_b[d]["trap_loss"] += e["trap_loss"]
for d in sorted(delta_b):
    b = delta_b[d]
    print(f"    delta={d}: {b['count']:>5} pairs  P&L=${b['pnl'] / 100:>8,.2f}  trap_loss=${b['trap_loss'] / 100:>8,.2f}")

# --- B) MAKER vs TAKER ---
print()
print("  B) MAKER vs TAKER FILL COMPOSITION")
print("  " + "-" * 68)
has_fills = [e for e in eo_data if e["total_fills"] > 0]
print(f"  Pairs with fill data: {len(has_fills)} / {len(eo_data)}")
if has_fills:
    w_taker = avg([e["taker_pct"] for e in has_fills if e["outcome"] == "WIN"])
    l_taker = avg([e["taker_pct"] for e in has_fills if e["outcome"] == "LOSS"])
    print(f"  Avg taker %:  winners={fmt(w_taker)}%  losers={fmt(l_taker)}%")

    for label, lo, hi in [("Pure maker (0%)", -1, 1), ("Low taker (<30%)", 1, 30), ("Mixed (30-70%)", 30, 70), ("High taker (>70%)", 70, 99), ("Pure taker (100%)", 99, 101)]:
        in_b = [e for e in has_fills if lo < e["taker_pct"] <= hi]
        if not in_b:
            continue
        bw = sum(1 for e in in_b if e["outcome"] == "WIN")
        bl = sum(1 for e in in_b if e["outcome"] == "LOSS")
        bp = sum(e["pnl"] for e in in_b)
        wr = bw / (bw + bl) * 100 if (bw + bl) else 0
        print(f"    {label:<25} {len(in_b):>5} pairs  {bw}W/{bl}L  WR={wr:.0f}%  P&L=${bp / 100:.2f}")

# --- C) QUEUE POSITION ---
print()
print("  C) QUEUE POSITION")
print("  " + "-" * 68)
has_queue = [e for e in eo_data if e["avg_queue"] is not None]
print(f"  Pairs with queue data: {len(has_queue)}")
if has_queue:
    w_q = avg([e["avg_queue"] for e in has_queue if e["outcome"] == "WIN"])
    l_q = avg([e["avg_queue"] for e in has_queue if e["outcome"] == "LOSS"])
    print(f"  Avg queue:  winners={fmt(w_q)}  losers={fmt(l_q)}")

    for label, lo, hi in [("Front (0-2)", -1, 2), ("Near front (2-5)", 2, 5), ("Mid (5-20)", 5, 20), ("Back (20-100)", 20, 100), ("Far back (100+)", 100, 999999)]:
        in_b = [e for e in has_queue if lo < e["avg_queue"] <= hi]
        if lo == -1:
            in_b = [e for e in has_queue if e["avg_queue"] <= hi]
        if not in_b:
            continue
        bw = sum(1 for e in in_b if e["outcome"] == "WIN")
        bl = sum(1 for e in in_b if e["outcome"] == "LOSS")
        bp = sum(e["pnl"] for e in in_b)
        wr = bw / (bw + bl) * 100 if (bw + bl) else 0
        print(f"    {label:<20} {len(in_b):>5} pairs  {bw}W/{bl}L  WR={wr:.0f}%  P&L=${bp / 100:.2f}")

# --- D) TIME SINCE ORDER ---
print()
print("  D) TIME SINCE ORDER (seconds to fill)")
print("  " + "-" * 68)
has_time = [e for e in eo_data if e["avg_time_since"] is not None]
print(f"  Pairs with time data: {len(has_time)}")
if has_time:
    w_t = avg([e["avg_time_since"] for e in has_time if e["outcome"] == "WIN"])
    l_t = avg([e["avg_time_since"] for e in has_time if e["outcome"] == "LOSS"])
    print(f"  Avg time to fill:  winners={fmt(w_t)}s  losers={fmt(l_t)}s")

    for label, lo, hi in [("< 1s", 0, 1), ("1-10s", 1, 10), ("10-60s", 10, 60), ("1-5 min", 60, 300), ("5-30 min", 300, 1800), ("> 30 min", 1800, 999999)]:
        in_b = [e for e in has_time if lo <= e["avg_time_since"] < hi]
        if not in_b:
            continue
        bw = sum(1 for e in in_b if e["outcome"] == "WIN")
        bl = sum(1 for e in in_b if e["outcome"] == "LOSS")
        bp = sum(e["pnl"] for e in in_b)
        wr = bw / (bw + bl) * 100 if (bw + bl) else 0
        print(f"    {label:<15} {len(in_b):>5} pairs  {bw}W/{bl}L  WR={wr:.0f}%  P&L=${bp / 100:.2f}")

# --- E) FILL DURATION ---
print()
print("  E) FILL DURATION (seconds from first to last fill in pair)")
print("  " + "-" * 68)
has_fd = [e for e in eo_data if e["fill_duration"] is not None]
print(f"  Pairs with fill_duration: {len(has_fd)}")
if has_fd:
    w_fd = avg([e["fill_duration"] for e in has_fd if e["outcome"] == "WIN"])
    l_fd = avg([e["fill_duration"] for e in has_fd if e["outcome"] == "LOSS"])
    print(f"  Avg fill duration:  winners={fmt(w_fd)}s  losers={fmt(l_fd)}s")

    for label, lo, hi in [("Instant (<1s)", 0, 1), ("1-10s", 1, 10), ("10-60s", 10, 60), ("1-5 min", 60, 300), ("5-30 min", 300, 1800), ("> 30 min", 1800, 999999)]:
        in_b = [e for e in has_fd if lo <= (e["fill_duration"] or 0) < hi]
        if not in_b:
            continue
        bw = sum(1 for e in in_b if e["outcome"] == "WIN")
        bl = sum(1 for e in in_b if e["outcome"] == "LOSS")
        bp = sum(e["pnl"] for e in in_b)
        bc = sum(e["total_cost"] for e in in_b)
        wr = bw / (bw + bl) * 100 if (bw + bl) else 0
        roi = bp / bc * 100 if bc else 0
        print(f"    {label:<15} {len(in_b):>5} pairs  {bw}W/{bl}L  WR={wr:.0f}%  P&L=${bp / 100:,.2f}  ROI={roi:.1f}%")

# --- F) TIME TO START ---
print()
print("  F) TIME TO START (seconds between fill and event resolution)")
print("  " + "-" * 68)
has_tts = [e for e in eo_data if e["time_to_start"] is not None and e["time_to_start"] > 0]
print(f"  Pairs with time_to_start: {len(has_tts)}")
if has_tts:
    for label, lo, hi in [("< 5 min", 0, 300), ("5-30 min", 300, 1800), ("30min-2hr", 1800, 7200), ("2-12 hr", 7200, 43200), ("12-24 hr", 43200, 86400), ("> 24 hr", 86400, 9999999)]:
        in_b = [e for e in has_tts if lo <= e["time_to_start"] < hi]
        if not in_b:
            continue
        bw = sum(1 for e in in_b if e["outcome"] == "WIN")
        bl = sum(1 for e in in_b if e["outcome"] == "LOSS")
        bp = sum(e["pnl"] for e in in_b)
        wr = bw / (bw + bl) * 100 if (bw + bl) else 0
        print(f"    {label:<15} {len(in_b):>5} pairs  {bw}W/{bl}L  WR={wr:.0f}%  P&L=${bp / 100:,.2f}")

# --- G) VOLUME AT ENTRY ---
print()
print("  G) MARKET VOLUME AT ENTRY")
print("  " + "-" * 68)
has_vol = [e for e in eo_data if e["entry_volume"] > 0]
print(f"  Pairs with volume data: {len(has_vol)} / {len(eo_data)}")
if has_vol:
    for label, lo, hi in [("< 50", 0, 50), ("50-200", 50, 200), ("200-1000", 200, 1000), ("1000-5000", 1000, 5000), ("> 5000", 5000, 9999999)]:
        in_b = [e for e in has_vol if lo <= e["entry_volume"] < hi]
        if not in_b:
            continue
        bw = sum(1 for e in in_b if e["outcome"] == "WIN")
        bl = sum(1 for e in in_b if e["outcome"] == "LOSS")
        bp = sum(e["pnl"] for e in in_b)
        wr = bw / (bw + bl) * 100 if (bw + bl) else 0
        print(f"    {label:<15} {len(in_b):>5} pairs  {bw}W/{bl}L  WR={wr:.0f}%  P&L=${bp / 100:,.2f}")

# --- H) INVESTMENT SIZE ---
print()
print("  H) INVESTMENT SIZE PER MARKET-PAIR")
print("  " + "-" * 68)
for label, lo, hi in [("< $1", 0, 100), ("$1-$5", 100, 500), ("$5-$10", 500, 1000), ("$10-$25", 1000, 2500), ("$25-$50", 2500, 5000), ("$50-$100", 5000, 10000), ("> $100", 10000, 99999999)]:
    in_b = [e for e in eo_data if lo <= e["total_cost"] < hi]
    if not in_b:
        continue
    bw = sum(1 for e in in_b if e["outcome"] == "WIN")
    bl = sum(1 for e in in_b if e["outcome"] == "LOSS")
    bp = sum(e["pnl"] for e in in_b)
    bc = sum(e["total_cost"] for e in in_b)
    wr = bw / (bw + bl) * 100 if (bw + bl) else 0
    roi = bp / bc * 100 if bc else 0
    bt = sum(1 for e in in_b if e["trapped"])
    tr = bt / len(in_b) * 100
    print(f"    {label:<12} {len(in_b):>5} pairs  {bw}W/{bl}L  WR={wr:.0f}%  P&L=${bp / 100:>8,.2f}  ROI={roi:>5.1f}%  trap={tr:.0f}%")

# --- I) GAME STATE AT FILL ---
print()
print("  I) GAME STATE AT FILL")
print("  " + "-" * 68)
state_b = defaultdict(lambda: {"w": 0, "l": 0, "pnl": 0, "cost": 0, "trapped": 0, "n": 0})
for e in eo_data:
    gs = e["game_state"] or "unknown"
    b = state_b[gs]
    b["n"] += 1
    if e["outcome"] == "WIN": b["w"] += 1
    elif e["outcome"] == "LOSS": b["l"] += 1
    b["pnl"] += e["pnl"]
    b["cost"] += e["total_cost"]
    if e["trapped"]: b["trapped"] += 1

print(f"  {'State':<15} {'Count':>6} {'W/L':>8} {'WR':>5} {'P&L':>10} {'ROI':>7} {'Trap%':>6}")
for gs in sorted(state_b, key=lambda x: state_b[x]["pnl"]):
    b = state_b[gs]
    total = b["w"] + b["l"]
    wr = b["w"] / total * 100 if total else 0
    roi = b["pnl"] / b["cost"] * 100 if b["cost"] else 0
    tr = b["trapped"] / b["n"] * 100 if b["n"] else 0
    print(f"  {gs:<15} {b['n']:>6} {b['w']:>3}/{b['l']:<4} {wr:>4.0f}% ${b['pnl'] / 100:>8,.2f} {roi:>6.1f}% {tr:>5.0f}%")

# --- J) FILL RATE ---
print()
print("  J) ORDER FILL RATE (orders with fills / total orders)")
print("  " + "-" * 68)
has_orders = [e for e in eo_data if e["total_orders"] > 0]
print(f"  Pairs with order data: {len(has_orders)}")
if has_orders:
    for label, lo, hi in [("< 10%", 0, 10), ("10-25%", 10, 25), ("25-50%", 25, 50), ("50-75%", 50, 75), ("75-100%", 75, 101)]:
        in_b = [e for e in has_orders if lo <= e["fill_rate"] < hi]
        if not in_b:
            continue
        bw = sum(1 for e in in_b if e["outcome"] == "WIN")
        bl = sum(1 for e in in_b if e["outcome"] == "LOSS")
        bp = sum(e["pnl"] for e in in_b)
        wr = bw / (bw + bl) * 100 if (bw + bl) else 0
        print(f"    {label:<15} {len(in_b):>5} pairs  {bw}W/{bl}L  WR={wr:.0f}%  P&L=${bp / 100:,.2f}")

# --- K) AVG PRICE (are we buying cheap or expensive contracts?) ---
print()
print("  K) AVERAGE FILL PRICE (side A)")
print("  " + "-" * 68)
has_price = [e for e in eo_data if e["avg_price_a"] > 0]
print(f"  Pairs with price data: {len(has_price)}")
if has_price:
    for label, lo, hi in [("1-10c (deep OTM)", 1, 10), ("10-25c", 10, 25), ("25-40c", 25, 40), ("40-60c (near 50/50)", 40, 60), ("60-75c", 60, 75), ("75-90c", 75, 90), ("90-99c (deep ITM)", 90, 100)]:
        in_b = [e for e in has_price if lo <= e["avg_price_a"] < hi]
        if not in_b:
            continue
        bw = sum(1 for e in in_b if e["outcome"] == "WIN")
        bl = sum(1 for e in in_b if e["outcome"] == "LOSS")
        bp = sum(e["pnl"] for e in in_b)
        bc = sum(e["total_cost"] for e in in_b)
        wr = bw / (bw + bl) * 100 if (bw + bl) else 0
        roi = bp / bc * 100 if bc else 0
        bt = sum(1 for e in in_b if e["trapped"])
        print(f"    {label:<25} {len(in_b):>5} pairs  {bw}W/{bl}L  WR={wr:.0f}%  P&L=${bp / 100:>8,.2f}  ROI={roi:.1f}%  traps={bt}")

# --- L) SPORTS COMPARISON ---
print()
print("=" * 72)
print("  SPORTS vs NON-SPORTS COMPARISON (from event_outcomes)")
print("=" * 72)

sports_eo = data_db.execute("""
    SELECT event_ticker, filled_a, filled_b, total_pnl, trapped,
           total_cost_a, total_cost_b, game_state_at_fill, time_to_start, fill_duration
    FROM event_outcomes WHERE ts >= ?
""", (CUTOFF,)).fetchall()

sports = [r for r in sports_eo if is_sports(r[0])]
sports_fills_raw = [r for r in fill_rows if is_sports(r[0])]

s_total = len(sports)
s_wins = sum(1 for r in sports if (r[3] or 0) > 0)
s_losses = sum(1 for r in sports if (r[3] or 0) < 0)
s_pnl = sum(r[3] or 0 for r in sports)
s_cost = sum((r[5] or 0) + (r[6] or 0) for r in sports)
s_trapped = sum(1 for r in sports if r[4])
s_both = sum(1 for r in sports if (r[1] or 0) > 0 and (r[2] or 0) > 0)
s_taker = sum(1 for r in sports_fills_raw if r[5])  # is_taker index
s_total_fills = len(sports_fills_raw)

ns_trapped_ct = sum(1 for e in eo_data if e["trapped"])
ns_both_ct = sum(1 for e in eo_data if e["both_filled"])
ns_taker_ct = sum(e["taker_fills"] for e in eo_data)
ns_total_fills_ct = sum(e["total_fills"] for e in eo_data)

print(f"  {'Metric':<30} {'Sports':>12} {'Non-Sports':>12}")
print("  " + "-" * 55)
print(f"  {'Market-pairs':<30} {s_total:>12} {len(eo_data):>12}")
print(f"  {'P&L':<30} ${s_pnl / 100:>10,.2f} ${total_pnl / 100:>10,.2f}")
print(f"  {'ROI':<30} {s_pnl / s_cost * 100 if s_cost else 0:>11.1f}% {total_pnl / total_cost * 100 if total_cost else 0:>11.1f}%")
print(f"  {'Win rate':<30} {s_wins / (s_wins + s_losses) * 100 if (s_wins + s_losses) else 0:>11.1f}% {len(wins) / (len(wins) + len(losses)) * 100 if (len(wins) + len(losses)) else 0:>11.1f}%")
print(f"  {'Trap rate':<30} {s_trapped / s_total * 100 if s_total else 0:>11.1f}% {ns_trapped_ct / len(eo_data) * 100 if eo_data else 0:>11.1f}%")
print(f"  {'Both-sides-filled rate':<30} {s_both / s_total * 100 if s_total else 0:>11.1f}% {ns_both_ct / len(eo_data) * 100 if eo_data else 0:>11.1f}%")
print(f"  {'Taker fill %':<30} {s_taker / s_total_fills * 100 if s_total_fills else 0:>11.1f}% {ns_taker_ct / ns_total_fills_ct * 100 if ns_total_fills_ct else 0:>11.1f}%")
s_avg_cost = s_cost / s_total if s_total else 0
ns_avg_cost = total_cost / len(eo_data) if eo_data else 0
print(f"  {'Avg cost/pair':<30} ${s_avg_cost / 100:>10,.2f} ${ns_avg_cost / 100:>10,.2f}")

# Time to start comparison
s_tts = [r[8] for r in sports if r[8] is not None and r[8] > 0]
ns_tts = [e["time_to_start"] for e in eo_data if e["time_to_start"] is not None and e["time_to_start"] > 0]
if s_tts or ns_tts:
    print(f"  {'Avg time_to_start':<30} {fmt(avg(s_tts), 0):>11}s {fmt(avg(ns_tts), 0):>11}s")
    print(f"  {'Median time_to_start':<30} {fmt(median(s_tts), 0):>11}s {fmt(median(ns_tts), 0):>11}s")

# Fill duration comparison
s_fd = [r[9] for r in sports if r[9] is not None]
ns_fd = [e["fill_duration"] for e in eo_data if e["fill_duration"] is not None]
if s_fd or ns_fd:
    print(f"  {'Avg fill_duration':<30} {fmt(avg(s_fd)):>11}s {fmt(avg(ns_fd)):>11}s")
    print(f"  {'Median fill_duration':<30} {fmt(median(s_fd)):>11}s {fmt(median(ns_fd)):>11}s")

# --- M) WORST 20 MARKET-PAIRS ---
print()
print("  20 WORST MARKET-PAIRS")
print("  " + "-" * 68)
worst = sorted(eo_data, key=lambda e: e["pnl"])[:20]
for e in worst:
    trap = " TRAPPED" if e["trapped"] else ""
    both = " both" if e["both_filled"] else " 1-side"
    print(f"    {e['et']:<42} ${e['pnl'] / 100:>7,.2f}  inv=${e['total_cost'] / 100:>6,.2f}  {e['type']:<12}{both}{trap}")

data_db.close()
hist_db.close()
