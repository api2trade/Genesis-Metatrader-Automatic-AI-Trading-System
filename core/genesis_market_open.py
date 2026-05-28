#!/usr/bin/env python3
"""
GENESIS Market Open Intensive Scanner — runs every 30 seconds.
Only does heavy work during the first 30 minutes of London, NY, and Asia opens.
Exits silently the rest of the time (no AI cost, no noise).
"""
import json, subprocess, sys
from datetime import datetime, timezone

PYTHON  = "/opt/hermes-agent/.venv-hermes/bin/python3"
AGENT   = "/opt/hermes-agent"

now = datetime.now(timezone.utc)
hr  = now.hour
mn  = now.minute
wd  = now.weekday()

# Market open windows (first 30 minutes of each session)
# London: 07:00–07:30 UTC
# NY:     13:00–13:30 UTC  (13:00 = 9am NY time)
# Asia:   22:00–22:30 UTC  (22:00 = Tokyo midnight open)

OPEN_WINDOWS = [
    {"name": "London Open",  "h": 7,  "pairs": ["EURUSDxx","GBPUSDxx","EURGBPxx"]},
    {"name": "New York Open","h": 13, "pairs": ["EURUSDxx","GBPUSDxx","XAUUSDxx"]},
    {"name": "Asia Open",    "h": 22, "pairs": ["XAUUSDxx","GBPJPYxx","USDJPYxx"]},
]

# Only fire during an open window
active_window = None
for w in OPEN_WINDOWS:
    if hr == w["h"] and mn < 30 and wd < 5:
        active_window = w
        break

if not active_window:
    sys.exit(0)  # Silent exit — not an open window

import requests

def api(path):
    try: return requests.get(f"{BRIDGE}{path}", timeout=8).json()
    except: return {}

def tool(name, cmd, sym):
    args = [PYTHON, f"{AGENT}/{name}_tool.py", cmd, sym]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=25, cwd=AGENT)
        return json.loads(r.stdout.strip()) if r.stdout.strip() else {}
    except: return {}

acc = api("/balance")
balance = float(acc.get("balance", 0))
equity  = float(acc.get("equity", 0))

print(f"=== {active_window['name'].upper()} INTENSIVE SCAN ===")
print(f"Time: {now.strftime('%H:%M')} UTC | Balance=€{balance:.2f} Equity=€{equity:.2f}")
print(f"Scanning: {', '.join(active_window['pairs'])}")

signals = []
for sym in active_window["pairs"]:
    # Zeus is best at market opens (killzone)
    r = tool("zeus", "analyze", sym)
    if r.get("action") == "trade":
        signals.append(("ZEUS", sym, r))
        print(f"  ⚡ ZEUS/{sym}: SIGNAL {r.get('direction')} | Score={r.get('confidence_score','?')} | RR={r.get('rr_ratio','?')}")
        continue
    # Apollo for trend-following at opens
    r = tool("apollo", "analyze", sym)
    if r.get("action") == "trade":
        signals.append(("APOLLO", sym, r))
        print(f"  🏹 APOLLO/{sym}: SIGNAL {r.get('direction')} | RR={r.get('rr_ratio','?')}")
        continue
    print(f"  ⚪ {sym}: no signal")

if signals:
    print(f"\n{len(signals)} signal(s) at {active_window['name']}!")
    for strat, sym, r in signals:
        clean = sym.replace("xx","")
        print(f"  EXECUTE: {PYTHON} {AGENT}/{strat.lower()}_tool.py execute {clean}")
    print(f"\nThis is a HIGH-PRIORITY window ({active_window['name']}).")
    print("Execute the best signal immediately if conditions confirm.")
else:
    print(f"\nNo signals at {active_window['name']} open yet. Continue monitoring.")
