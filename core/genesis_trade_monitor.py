#!/usr/bin/env python3
"""
GENESIS Trade Monitor — zero LLM cost.
Runs every minute. Sends Telegram ONLY when a trade opens or closes.
No AI, no summaries, no noise.
"""
import json, os, requests
from datetime import datetime, timezone
from pathlib import Path

TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT    = os.getenv("TELEGRAM_CHAT_ID", "")
MT5_API  = os.getenv("MT5_API_URL", "https://mt5.mt4api.dev")
MT5_ID   = os.getenv("MT5_ACCOUNT_UUID", "")
MT5_AUTH = (os.getenv("MT5_API_USER", ""), os.getenv("MT5_API_PASS", ""))
SNAP    = Path("/var/log/hermes/position_snapshot.json")

def tg(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": CHAT, "text": msg}, timeout=10)
    except: pass

def api(path):
    try: return requests.get(f"{BRIDGE}{path}", timeout=8).json()
    except: return {}

def pip_val(sym):
    if "JPY" in sym: return 0.01
    if "XAU" in sym or "GOLD" in sym: return 0.1
    return 0.0001

# Current state
acc      = api("/balance")
pos      = api("/positions")
balance  = float(acc.get("balance", 0))
equity   = float(acc.get("equity", 0))
open_pos = pos if isinstance(pos, list) else []
now      = datetime.now(timezone.utc).strftime("%H:%M UTC")

# Load previous snapshot
prev = {}
if SNAP.exists():
    try: prev = json.loads(SNAP.read_text())
    except: prev = {}

# Build current snapshot {ticket: position_dict}
curr = {str(p.get("ticket")): p for p in open_pos}

# ── Detect OPENED positions ──────────────────────────────────────────────
for ticket, p in curr.items():
    if ticket not in prev:
        sym    = p.get("symbol","?")
        side   = p.get("orderType","?").upper()
        lots   = p.get("lots","?")
        entry  = p.get("openPrice","?")
        sl     = p.get("stopLoss","?")
        tp     = p.get("takeProfit","?")
        strat  = p.get("comment","?")
        pip    = pip_val(sym)
        sl_pip = round(abs(float(entry)-float(sl))/pip, 1) if sl and entry else "?"
        tp_pip = round(abs(float(tp)-float(entry))/pip, 1) if tp and entry else "?"
        tg(
            f"🟢 TRADE OPENED — {now}\n"
            f"Strategy: {strat}\n"
            f"{sym} {side} {lots} lot\n"
            f"Entry: {entry}\n"
            f"SL: {sl} (-{sl_pip} pips)\n"
            f"TP: {tp} (+{tp_pip} pips)\n"
            f"Balance: €{balance:,.2f}"
        )

# ── Detect CLOSED positions ──────────────────────────────────────────────
for ticket, p in prev.items():
    if ticket not in curr:
        sym    = p.get("symbol","?")
        side   = p.get("orderType","?").upper()
        lots   = p.get("lots","?")
        entry  = p.get("openPrice","?")
        strat  = p.get("comment","?")

        # Get P&L from history (bridge /history)
        hist   = api("/history")
        pnl    = None
        if isinstance(hist, list):
            for h in reversed(hist):
                if str(h.get("ticket")) == ticket:
                    pnl = float(h.get("profit", h.get("pnl", 0)))
                    break

        icon   = "🟢" if (pnl or 0) >= 0 else "🔴"
        pnl_str = f"€{pnl:+.2f}" if pnl is not None else "see MT5"
        tg(
            f"{icon} TRADE CLOSED — {now}\n"
            f"Strategy: {strat}\n"
            f"{sym} {side} {lots} lot\n"
            f"Entry: {entry}\n"
            f"Result: {pnl_str}\n"
            f"Balance: €{balance:,.2f}"
        )

# ── Save new snapshot ────────────────────────────────────────────────────
SNAP.parent.mkdir(parents=True, exist_ok=True)
SNAP.write_text(json.dumps(curr))
