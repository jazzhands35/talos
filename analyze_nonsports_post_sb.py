"""Post-Super Bowl non-sports loss analysis — where is the money going?"""

import sqlite3
from collections import defaultdict

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


def is_sports(event_ticker):
    cat = categories.get(event_ticker, "")
    if cat:
        return cat == "Sports"
    prefix = event_ticker.split("-")[0] if "-" in event_ticker else event_ticker
    return prefix in SPORTS_PREFIXES


def classify_type(event_ticker):
    prefix = event_ticker.split("-")[0] if "-" in event_ticker else event_ticker
    if prefix in TYPE_MAP:
        return TYPE_MAP[prefix]
    if any(prefix.startswith(w) for w in WEATHER_PREFIXES):
        return "Weather"
    if "MENTION" in prefix:
        return "Mentions"
    if any(prefix.startswith(p) for p in POLITICS_PREFIXES):
        return "Politics"
    if any(prefix.startswith(p) for p in ENTERTAINMENT_PREFIXES):
        return "Entertainment"
    if any(prefix.startswith(p) for p in TECH_PREFIXES):
        return "Tech/Science"
    if "EARN" in prefix:
        return "Earnings"
    return "Other"


# --- Filter to post-Super Bowl (after 2026-02-11) ---
CUTOFF = "2026-02-12T00:00:00Z"

settlements = data_db.execute("""
    SELECT ticker, event_ticker, market_result, revenue, fee_cost,
           yes_count, no_count, yes_total_cost, no_total_cost, settled_time
    FROM settlement_cache
    WHERE (yes_count > 0 OR no_count > 0) AND settled_time >= ?
    ORDER BY settled_time
""", (CUTOFF,)).fetchall()

nonsports = [s for s in settlements if not is_sports(s[1])]

# Aggregate by event
events = defaultdict(lambda: {
    "tickers": [], "revenue": 0, "cost": 0, "fees": 0,
    "yes_count": 0, "no_count": 0, "settled_time": "",
    "implicit_revenue": 0, "results": [],
})
for ticker, et, result, rev, fees, yc, nc, yc_cost, nc_cost, st in nonsports:
    e = events[et]
    e["tickers"].append(ticker)
    e["revenue"] += rev
    e["cost"] += yc_cost + nc_cost
    e["fees"] += fees
    e["yes_count"] += yc
    e["no_count"] += nc
    e["implicit_revenue"] += min(yc, nc) * 100
    if st > e["settled_time"]:
        e["settled_time"] = st
    if result:
        e["results"].append(result)

# Calculate P&L per event
for et, e in events.items():
    e["pnl"] = e["revenue"] + e["implicit_revenue"] - e["cost"] - e["fees"]
    e["type"] = classify_type(et)

# --- Pull trap data from event_outcomes ---
trap_data = {}
rows = data_db.execute("""
    SELECT event_ticker, trapped, trap_side, trap_delta, trap_loss,
           filled_a, filled_b, total_cost_a, total_cost_b, total_pnl,
           game_state_at_fill, time_to_start, fill_duration
    FROM event_outcomes
    WHERE ts >= ?
""", (CUTOFF,)).fetchall()
for r in rows:
    trap_data[r[0]] = {
        "trapped": r[1], "trap_side": r[2], "trap_delta": r[3], "trap_loss": r[4],
        "filled_a": r[5], "filled_b": r[6], "cost_a": r[7], "cost_b": r[8],
        "pnl": r[9], "game_state": r[10], "time_to_start": r[11], "fill_duration": r[12],
    }


# ================================================================
# ANALYSIS OUTPUT
# ================================================================

total_pnl = sum(e["pnl"] for e in events.values())
total_cost = sum(e["cost"] for e in events.values())
winners = [(et, e) for et, e in events.items() if e["pnl"] > 0]
losers = [(et, e) for et, e in events.items() if e["pnl"] < 0]
flat = [(et, e) for et, e in events.items() if e["pnl"] == 0]

win_total = sum(e["pnl"] for _, e in winners)
loss_total = sum(e["pnl"] for _, e in losers)

print("=" * 72)
print("  POST-SUPER BOWL NON-SPORTS ANALYSIS (Feb 12 onward)")
print("=" * 72)
print()
print(f"  Events:     {len(events)}  ({len(winners)}W / {len(losers)}L / {len(flat)}B)")
print(f"  Invested:   ${total_cost / 100:,.2f}")
print(f"  Net P&L:    ${total_pnl / 100:,.2f}")
print(f"  Gross wins: ${win_total / 100:,.2f}    Gross losses: ${loss_total / 100:,.2f}")
if winners:
    print(f"  Avg win:    ${win_total / len(winners) / 100:,.2f}    Avg loss:    ${loss_total / len(losers) / 100:,.2f}")

# --- 1) LOSS SIZE DISTRIBUTION ---
print()
print("  LOSS SIZE DISTRIBUTION")
print("  " + "-" * 68)
buckets = [
    ("$0 to -$0.50", 0, 50),
    ("-$0.50 to -$1", 50, 100),
    ("-$1 to -$2", 100, 200),
    ("-$2 to -$5", 200, 500),
    ("-$5 to -$10", 500, 1000),
    ("-$10 to -$25", 1000, 2500),
    ("-$25+", 2500, 999999),
]
for label, lo, hi in buckets:
    in_bucket = [(et, e) for et, e in losers if lo < abs(e["pnl"]) <= hi]
    if lo == 0:
        in_bucket = [(et, e) for et, e in losers if abs(e["pnl"]) <= hi]
    bucket_loss = sum(e["pnl"] for _, e in in_bucket)
    bar = "#" * min(len(in_bucket), 50)
    print(f"  {label:<16} {len(in_bucket):>4} events  ${bucket_loss / 100:>8,.2f}  {bar}")

# --- 2) P&L BY CATEGORY (post-SB) ---
print()
print("  P&L BY CATEGORY")
print("  " + "-" * 68)
type_stats = defaultdict(lambda: {
    "pnl": 0, "cost": 0, "events": 0, "wins": 0, "losses": 0,
    "win_pnl": 0, "loss_pnl": 0,
})
for et, e in events.items():
    t = e["type"]
    b = type_stats[t]
    b["pnl"] += e["pnl"]
    b["cost"] += e["cost"]
    b["events"] += 1
    if e["pnl"] > 0:
        b["wins"] += 1
        b["win_pnl"] += e["pnl"]
    elif e["pnl"] < 0:
        b["losses"] += 1
        b["loss_pnl"] += e["pnl"]

print(f"  {'Category':<17} {'Evts':>5} {'W/L':>7} {'Invested':>10} {'P&L':>10} {'ROI':>7}  {'Avg W':>7} {'Avg L':>7}")
print("  " + "-" * 68)
for t in sorted(type_stats, key=lambda x: type_stats[x]["pnl"]):
    b = type_stats[t]
    roi = b["pnl"] / b["cost"] * 100 if b["cost"] else 0
    avg_w = f"${b['win_pnl'] / b['wins'] / 100:.2f}" if b["wins"] else "---"
    avg_l = f"${b['loss_pnl'] / b['losses'] / 100:.2f}" if b["losses"] else "---"
    print(f"  {t:<17} {b['events']:>5} {b['wins']:>3}/{b['losses']:<3} ${b['cost'] / 100:>8,.2f} ${b['pnl'] / 100:>8,.2f} {roi:>6.1f}%  {avg_w:>7} {avg_l:>7}")

# --- 3) TRAP ANALYSIS ---
print()
print("  TRAP ANALYSIS (from event_outcomes)")
print("  " + "-" * 68)
trapped_events = {et: td for et, td in trap_data.items() if td["trapped"] and not is_sports(et)}
non_trapped = {et: td for et, td in trap_data.items() if not td["trapped"] and not is_sports(et)}
trap_loss = sum(td["trap_loss"] or 0 for td in trapped_events.values())
trap_cost = sum((td["cost_a"] or 0) + (td["cost_b"] or 0) for td in trapped_events.values())
print(f"  Trapped events:     {len(trapped_events)}")
print(f"  Non-trapped events: {len(non_trapped)}")
if trapped_events:
    print(f"  Total trap loss:    ${trap_loss / 100:,.2f}")
    print(f"  Capital in traps:   ${trap_cost / 100:,.2f}")
    print()
    # Break down traps by type
    trap_by_type = defaultdict(lambda: {"count": 0, "loss": 0})
    for et, td in trapped_events.items():
        t = classify_type(et)
        trap_by_type[t]["count"] += 1
        trap_by_type[t]["loss"] += td["trap_loss"] or 0
    print(f"  {'Category':<17} {'Trapped':>7} {'Trap Loss':>10}")
    for t in sorted(trap_by_type, key=lambda x: trap_by_type[x]["loss"]):
        b = trap_by_type[t]
        print(f"  {t:<17} {b['count']:>7} ${b['loss'] / 100:>8,.2f}")

    # Worst traps
    print()
    print("  WORST TRAPS (by trap_loss)")
    print("  " + "-" * 68)
    worst_traps = sorted(trapped_events.items(), key=lambda x: x[1]["trap_loss"] or 0)[:15]
    for et, td in worst_traps:
        t = classify_type(et)
        tl = td["trap_loss"] or 0
        side = td["trap_side"] or "?"
        delta = td["trap_delta"] or 0
        print(f"    {et:<38} ${tl / 100:>7,.2f}  side={side}  delta={delta}  ({t})")

# --- 4) ALL LOSING EVENTS > $3 loss ---
print()
print("  ALL EVENTS WITH > $3 LOSS")
print("  " + "-" * 68)
big_losers = sorted(
    [(et, e) for et, e in events.items() if e["pnl"] < -300],
    key=lambda x: x[1]["pnl"],
)
for et, e in big_losers:
    trap_info = ""
    if et in trap_data and trap_data[et]["trapped"]:
        trap_info = f"  TRAPPED({trap_data[et]['trap_side']})"
    cost_str = f"${e['cost'] / 100:.2f}"
    pnl_str = f"${e['pnl'] / 100:.2f}"
    print(f"    {et:<38} {pnl_str:>8}  inv={cost_str:>8}  ({e['type']}){trap_info}")

# --- 5) WIN vs LOSS ASYMMETRY ---
print()
print("  WIN vs LOSS SIZE ANALYSIS")
print("  " + "-" * 68)
win_pnls = sorted([e["pnl"] for _, e in winners])
loss_pnls = sorted([e["pnl"] for _, e in losers])
if win_pnls:
    print(f"  Winners:  min=${min(win_pnls) / 100:.2f}  median=${win_pnls[len(win_pnls) // 2] / 100:.2f}  max=${max(win_pnls) / 100:.2f}  total=${sum(win_pnls) / 100:.2f}")
if loss_pnls:
    print(f"  Losers:   min=${min(loss_pnls) / 100:.2f}  median=${loss_pnls[len(loss_pnls) // 2] / 100:.2f}  max=${max(loss_pnls) / 100:.2f}  total=${sum(loss_pnls) / 100:.2f}")

# Distribution of invested amounts
invest_amounts = sorted([e["cost"] for _, e in events.items()])
print()
print(f"  Investment per event:")
print(f"    Min: ${min(invest_amounts) / 100:.2f}   Median: ${invest_amounts[len(invest_amounts) // 2] / 100:.2f}   Max: ${max(invest_amounts) / 100:.2f}")

# --- 6) WEATHER DEEP DIVE (biggest category) ---
print()
print("  WEATHER DEEP DIVE")
print("  " + "-" * 68)
weather_events = [(et, e) for et, e in events.items() if e["type"] == "Weather"]

# Group by sub-type (high temp, low temp, rain, snow, etc.)
weather_sub = defaultdict(lambda: {"pnl": 0, "cost": 0, "events": 0, "wins": 0, "losses": 0})
for et, e in weather_events:
    prefix = et.split("-")[0]
    if prefix.startswith("KXHIGHT") or prefix.startswith("KXHIGH"):
        sub = "High Temp"
    elif prefix.startswith("KXLOWT"):
        sub = "Low Temp"
    elif prefix.startswith("KXRAIN"):
        sub = "Rain"
    elif "SNOW" in prefix:
        sub = "Snow"
    elif "ARCTIC" in prefix:
        sub = "Arctic Ice"
    elif "TORNADO" in prefix:
        sub = "Tornado"
    else:
        sub = f"Other ({prefix})"
    b = weather_sub[sub]
    b["pnl"] += e["pnl"]
    b["cost"] += e["cost"]
    b["events"] += 1
    if e["pnl"] > 0:
        b["wins"] += 1
    elif e["pnl"] < 0:
        b["losses"] += 1

print(f"  {'Sub-type':<15} {'Evts':>5} {'W/L':>7} {'Invested':>10} {'P&L':>10} {'ROI':>7}")
for sub in sorted(weather_sub, key=lambda x: weather_sub[x]["pnl"]):
    b = weather_sub[sub]
    roi = b["pnl"] / b["cost"] * 100 if b["cost"] else 0
    print(f"  {sub:<15} {b['events']:>5} {b['wins']:>3}/{b['losses']:<3} ${b['cost'] / 100:>8,.2f} ${b['pnl'] / 100:>8,.2f} {roi:>6.1f}%")

# --- 7) MENTIONS DEEP DIVE ---
print()
print("  MENTIONS DEEP DIVE")
print("  " + "-" * 68)
mention_events = [(et, e) for et, e in events.items() if e["type"] == "Mentions"]
mention_sub = defaultdict(lambda: {"pnl": 0, "cost": 0, "events": 0})
for et, e in mention_events:
    prefix = et.split("-")[0]
    mention_sub[prefix]["pnl"] += e["pnl"]
    mention_sub[prefix]["cost"] += e["cost"]
    mention_sub[prefix]["events"] += 1

print(f"  {'Series':<30} {'Evts':>4} {'Invested':>10} {'P&L':>10}")
for sub in sorted(mention_sub, key=lambda x: mention_sub[x]["pnl"]):
    b = mention_sub[sub]
    print(f"  {sub:<30} {b['events']:>4} ${b['cost'] / 100:>8,.2f} ${b['pnl'] / 100:>8,.2f}")

# --- 8) CRYPTO DEEP DIVE ---
print()
print("  CRYPTO DEEP DIVE")
print("  " + "-" * 68)
crypto_events = [(et, e) for et, e in events.items() if e["type"] == "Crypto"]
crypto_sub = defaultdict(lambda: {"pnl": 0, "cost": 0, "events": 0})
for et, e in crypto_events:
    prefix = et.split("-")[0]
    crypto_sub[prefix]["pnl"] += e["pnl"]
    crypto_sub[prefix]["cost"] += e["cost"]
    crypto_sub[prefix]["events"] += 1

print(f"  {'Series':<20} {'Evts':>4} {'Invested':>10} {'P&L':>10}")
for sub in sorted(crypto_sub, key=lambda x: crypto_sub[x]["pnl"]):
    b = crypto_sub[sub]
    print(f"  {sub:<20} {b['events']:>4} ${b['cost'] / 100:>8,.2f} ${b['pnl'] / 100:>8,.2f}")

# --- 9) WEEKLY TREND ---
print()
print("  WEEKLY P&L TREND")
print("  " + "-" * 68)
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
PT = ZoneInfo("America/Los_Angeles")

weekly = defaultdict(lambda: {"pnl": 0, "cost": 0, "events": 0})
for et, e in events.items():
    if e["settled_time"]:
        try:
            dt = datetime.fromisoformat(e["settled_time"].replace("Z", "+00:00")).astimezone(PT)
            # ISO week starting Monday
            week_start = (dt - timedelta(days=dt.weekday())).strftime("%Y-%m-%d")
            weekly[week_start]["pnl"] += e["pnl"]
            weekly[week_start]["cost"] += e["cost"]
            weekly[week_start]["events"] += 1
        except ValueError:
            pass

cumulative = 0
print(f"  {'Week of':<12} {'Evts':>5} {'Invested':>10} {'P&L':>10} {'Cumulative':>12}")
for w in sorted(weekly):
    b = weekly[w]
    cumulative += b["pnl"]
    print(f"  {w:<12} {b['events']:>5} ${b['cost'] / 100:>8,.2f} ${b['pnl'] / 100:>8,.2f} ${cumulative / 100:>10,.2f}")

data_db.close()
hist_db.close()
