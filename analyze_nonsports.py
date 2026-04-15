"""Analyze non-sports trading performance from Talos settlement data."""

import sqlite3
from collections import defaultdict

# Connect to both databases
data_db = sqlite3.connect("talos_data.db")
hist_db = sqlite3.connect("kalshi_history.db")

# 1) Get all settlements with non-zero activity
settlements = data_db.execute("""
    SELECT ticker, event_ticker, market_result, revenue, fee_cost,
           yes_count, no_count, yes_total_cost, no_total_cost, settled_time
    FROM settlement_cache
    WHERE (yes_count > 0 OR no_count > 0)
    ORDER BY settled_time
""").fetchall()
print(f"Total settlements with fills: {len(settlements)}")

# 2) Get category lookup from kalshi_history
categories = {}
rows = hist_db.execute("SELECT event_ticker, category FROM events").fetchall()
for et, cat in rows:
    categories[et] = cat
print(f"Category lookup: {len(categories)} events")

# 3) Known sports ticker prefixes for events not in history DB
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


def is_sports(event_ticker: str) -> bool:
    cat = categories.get(event_ticker, "")
    if cat:
        return cat == "Sports"
    prefix = event_ticker.split("-")[0] if "-" in event_ticker else event_ticker
    return prefix in SPORTS_PREFIXES


# 4) Separate sports vs non-sports
nonsports_settlements = [s for s in settlements if not is_sports(s[1])]
sports_settlements = [s for s in settlements if is_sports(s[1])]
print(f"Sports settlements: {len(sports_settlements)}")
print(f"Non-sports settlements: {len(nonsports_settlements)}")

# 5) Aggregate non-sports by event
events: dict = defaultdict(lambda: {
    "tickers": [], "revenue": 0, "cost": 0, "fees": 0,
    "yes_count": 0, "no_count": 0, "result": "", "settled_time": "",
    "implicit_revenue": 0,
})
for ticker, et, result, rev, fees, yc, nc, yc_cost, nc_cost, st in nonsports_settlements:
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
        e["result"] = result

print(f"Non-sports unique events: {len(events)}")

# 6) Calculate P&L per event
total_pnl = 0
total_invested = 0
total_fees = 0
total_revenue = 0
wins = 0
losses = 0
breakeven = 0
event_pnls = []

for et, e in events.items():
    pnl = e["revenue"] + e["implicit_revenue"] - e["cost"] - e["fees"]
    e["pnl"] = pnl
    total_pnl += pnl
    total_invested += e["cost"]
    total_fees += e["fees"]
    total_revenue += e["revenue"] + e["implicit_revenue"]
    if pnl > 0:
        wins += 1
    elif pnl < 0:
        losses += 1
    else:
        breakeven += 1
    event_pnls.append((et, pnl, e["cost"], e["settled_time"]))

# 7) Classify events by type
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


def classify_type(event_ticker: str) -> str:
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
    return f"Other ({prefix})"


# 8) Print results
event_pnls.sort(key=lambda x: x[1])

print()
print("=" * 72)
print("  NON-SPORTS TRADING ANALYSIS — All Time")
print("=" * 72)
print()
print(f"  Total events traded:    {len(events)}")
print(f"  Total settlements:      {len(nonsports_settlements)}")
print(f"  Total invested:         ${total_invested / 100:>12,.2f}")
print(f"  Total revenue:          ${total_revenue / 100:>12,.2f}")
print(f"  Total fees:             ${total_fees / 100:>12,.2f}")
print(f"  Net P&L:                ${total_pnl / 100:>12,.2f}")
if total_invested:
    print(f"  ROI:                    {total_pnl / total_invested * 100:>12.2f}%")
print()
print(f"  Record:   {wins}W / {losses}L / {breakeven}B")
if wins + losses:
    print(f"  Win rate: {wins / (wins + losses) * 100:.1f}%")
if total_invested and len(events):
    print(f"  Avg cost/event:         ${total_invested / len(events) / 100:>12,.2f}")

# Category breakdown
type_buckets: dict = defaultdict(lambda: {"pnl": 0, "cost": 0, "count": 0, "events": 0})
for et, e in events.items():
    t = classify_type(et)
    b = type_buckets[t]
    b["pnl"] += e["pnl"]
    b["cost"] += e["cost"]
    b["count"] += len(e["tickers"])
    b["events"] += 1

print()
print("  P&L BY CATEGORY")
print("  " + "-" * 70)
header = f"  {'Category':<20} {'Events':>6} {'Sttlmnts':>8} {'Invested':>12} {'P&L':>12} {'ROI':>8}"
print(header)
print("  " + "-" * 70)
for t in sorted(type_buckets, key=lambda x: type_buckets[x]["pnl"]):
    b = type_buckets[t]
    roi = b["pnl"] / b["cost"] * 100 if b["cost"] else 0
    invested = f"${b['cost'] / 100:,.2f}"
    pnl = f"${b['pnl'] / 100:,.2f}"
    print(f"  {t:<20} {b['events']:>6} {b['count']:>8} {invested:>12} {pnl:>12} {roi:>7.1f}%")

# Best/worst events
print()
print("  10 WORST EVENTS")
print("  " + "-" * 70)
for et, pnl, cost, st in event_pnls[:10]:
    t = classify_type(et)
    pnl_str = f"${pnl / 100:,.2f}"
    print(f"    {et:<40} {pnl_str:>10}  {t:<15} {st[:10]}")

print()
print("  10 BEST EVENTS")
print("  " + "-" * 70)
for et, pnl, cost, st in event_pnls[-10:]:
    t = classify_type(et)
    pnl_str = f"${pnl / 100:,.2f}"
    print(f"    {et:<40} {pnl_str:>10}  {t:<15} {st[:10]}")

# Monthly P&L
monthly: dict = defaultdict(lambda: {"pnl": 0, "cost": 0, "events": 0})
for et, e in events.items():
    if e["settled_time"]:
        month = e["settled_time"][:7]
        monthly[month]["pnl"] += e["pnl"]
        monthly[month]["cost"] += e["cost"]
        monthly[month]["events"] += 1

print()
print("  MONTHLY P&L")
print("  " + "-" * 70)
header = f"  {'Month':<12} {'Events':>6} {'Invested':>12} {'P&L':>12} {'ROI':>8}"
print(header)
print("  " + "-" * 70)
cumulative = 0
for m in sorted(monthly):
    b = monthly[m]
    roi = b["pnl"] / b["cost"] * 100 if b["cost"] else 0
    cumulative += b["pnl"]
    invested = f"${b['cost'] / 100:,.2f}"
    pnl_str = f"${b['pnl'] / 100:,.2f}"
    cum_str = f"${cumulative / 100:,.2f}"
    print(f"  {m:<12} {b['events']:>6} {invested:>12} {pnl_str:>12} {roi:>7.1f}%   cum: {cum_str}")

# Sports comparison
sports_events: dict = defaultdict(lambda: {
    "revenue": 0, "cost": 0, "fees": 0, "implicit_revenue": 0,
    "yes_count": 0, "no_count": 0,
})
for ticker, et, result, rev, fees, yc, nc, yc_cost, nc_cost, st in sports_settlements:
    e = sports_events[et]
    e["revenue"] += rev
    e["cost"] += yc_cost + nc_cost
    e["fees"] += fees
    e["implicit_revenue"] += min(yc, nc) * 100

sports_pnl = sum(
    e["revenue"] + e["implicit_revenue"] - e["cost"] - e["fees"]
    for e in sports_events.values()
)
sports_cost = sum(e["cost"] for e in sports_events.values())

print()
print("  " + "=" * 70)
print("  SPORTS vs NON-SPORTS COMPARISON")
print("  " + "-" * 70)
print(f"  {'':20} {'Events':>8} {'Invested':>14} {'P&L':>14} {'ROI':>8}")
print(f"  {'Sports':<20} {len(sports_events):>8} ${sports_cost / 100:>12,.2f} ${sports_pnl / 100:>12,.2f} {sports_pnl / sports_cost * 100 if sports_cost else 0:>7.1f}%")
print(f"  {'Non-Sports':<20} {len(events):>8} ${total_invested / 100:>12,.2f} ${total_pnl / 100:>12,.2f} {total_pnl / total_invested * 100 if total_invested else 0:>7.1f}%")
all_cost = sports_cost + total_invested
all_pnl = sports_pnl + total_pnl
print(f"  {'TOTAL':<20} {len(sports_events) + len(events):>8} ${all_cost / 100:>12,.2f} ${all_pnl / 100:>12,.2f} {all_pnl / all_cost * 100 if all_cost else 0:>7.1f}%")

data_db.close()
hist_db.close()
