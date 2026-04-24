"""Dump raw /events response for the hurricane/storm tickers the user hit
in the SchedulePopup, so we can see what timing fields Kalshi actually
returns for these events.
"""
from __future__ import annotations

import json
from urllib.request import urlopen

BASE = "https://api.elections.kalshi.com/trade-api/v2"


def dump_event(event_ticker: str) -> None:
    url = f"{BASE}/events/{event_ticker}?with_nested_markets=true"
    print(f"\n=== {url}")
    with urlopen(url, timeout=30) as resp:
        data = json.loads(resp.read())
    event = data.get("event", {})
    print("--- EVENT-LEVEL FIELDS ---")
    for k in sorted(event.keys()):
        if k == "markets":
            print(f"  markets: [{len(event['markets'])} markets]")
        else:
            v = event[k]
            s = json.dumps(v, default=str)
            print(f"  {k}: {s[:140]}")
    if event.get("markets"):
        print("--- FIRST MARKET FIELDS ---")
        m = event["markets"][0]
        for k in sorted(m.keys()):
            v = m[k]
            s = json.dumps(v, default=str)
            print(f"  {k}: {s[:140]}")


def main() -> None:
    # Tickers from the user's SchedulePopup
    for et in ("KXHURCTOTMAJ-26DEC01", "KXTROPSTORM-26DEC01", "KXHURCTOT-26DEC01"):
        try:
            dump_event(et)
        except Exception as e:
            print(f"\n=== {et} FAILED: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
