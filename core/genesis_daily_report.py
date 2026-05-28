#!/usr/bin/env python3
"""
GENESIS Daily P&L Report
Runs at 17:00 UTC. Sends a full daily performance summary to Telegram.
"""
import json, os, requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT   = os.getenv("TELEGRAM_CHAT_ID", "")
MT5_API  = os.getenv("MT5_API_URL", "https://mt5.mt4api.dev")
MT5_ID   = os.getenv("MT5_ACCOUNT_UUID", "")
MT5_AUTH = (os.getenv("MT5_API_USER", ""), os.getenv("MT5_API_PASS", ""))

def api(path):
    try: return requests.get(f"{BRIDGE}{path}", timeout=10).json()
    except: return {}

def tg(msg):
    if not TOKEN: return
    try:
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": CHAT, "text": msg, "parse_mode": "Markdown"}, timeout=10)
    except: pass

now     = datetime.now(timezone.utc)
today   = now.date()

# Account state
acc     = api("/balance")
balance = float(acc.get("balance", 0))
equity  = float(acc.get("equity", 0))
profit  = float(acc.get("profit", 0))

# Open positions
pos     = api("/positions")
open_pos = pos if isinstance(pos, list) else []

# Trade history — filter today's closed trades
hist = api("/history")
trades_today = []
if isinstance(hist, list):
    for t in hist:
        close_time = t.get("closeTime", "")
        try:
            ct = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
            if ct.date() == today and float(t.get("closeLots", 0)) > 0:
                trades_today.append(t)
        except: pass

# Stats
wins   = [t for t in trades_today if float(t.get("profit", 0)) > 0]
losses = [t for t in trades_today if float(t.get("profit", 0)) < 0]
be     = [t for t in trades_today if float(t.get("profit", 0)) == 0]
total_pnl = sum(float(t.get("profit", 0)) for t in trades_today)
best   = max(trades_today, key=lambda t: float(t.get("profit",0)), default=None)
worst  = min(trades_today, key=lambda t: float(t.get("profit",0)), default=None)
win_rate = round(len(wins)/len(trades_today)*100) if trades_today else 0

# Strategy breakdown
by_strat = {}
for t in trades_today:
    tag = t.get("comment", "UNKNOWN").split("-")[0]
    by_strat.setdefault(tag, {"trades": 0, "pnl": 0.0})
    by_strat[tag]["trades"] += 1
    by_strat[tag]["pnl"] += float(t.get("profit", 0))

# Format open positions
open_lines = []
for p in open_pos:
    sym    = p.get("symbol","?")
    side   = p.get("orderType","?").upper()
    lots   = p.get("lots","?")
    pnl    = float(p.get("profit", 0))
    tag    = p.get("comment","?")
    icon   = "📈" if side == "BUY" else "📉"
    open_lines.append(f"  {icon} {sym} {side} {lots}L [{tag}] €{pnl:+.2f}")

# Build message
pnl_icon = "🟢" if total_pnl >= 0 else "🔴"
msg = (
    f"📊 *GENESIS Daily Report — {today.strftime('%d %b %Y')}*\n"
    f"{'─'*32}\n"
    f"*Account*\n"
    f"  Balance: €{balance:,.2f}\n"
    f"  Equity:  €{equity:,.2f}\n"
    f"  Float:   €{profit:+.2f}\n\n"
    f"*Today's Performance*\n"
    f"  {pnl_icon} P&L:      €{total_pnl:+.2f}\n"
    f"  📋 Trades:    {len(trades_today)} ({len(wins)}W / {len(losses)}L / {len(be)}BE)\n"
    f"  🎯 Win Rate:  {win_rate}%\n"
)

if best:
    msg += f"  🏆 Best:     €{float(best.get('profit',0)):+.2f} ({best.get('symbol','')} [{best.get('comment','')}])\n"
if worst and worst != best:
    msg += f"  💀 Worst:    €{float(worst.get('profit',0)):+.2f} ({worst.get('symbol','')} [{worst.get('comment','')}])\n"

if by_strat:
    msg += f"\n*By Strategy*\n"
    for tag, data in sorted(by_strat.items()):
        icon = "🟢" if data["pnl"] >= 0 else "🔴"
        msg += f"  {icon} {tag}: {data['trades']} trades | €{data['pnl']:+.2f}\n"

if open_pos:
    msg += f"\n*Open Positions ({len(open_pos)})*\n"
    msg += "\n".join(open_lines) + "\n"
else:
    msg += f"\n*Open Positions:* None\n"

if not trades_today:
    msg += "\n_No closed trades today._\n"

tg(msg)
print(msg)
