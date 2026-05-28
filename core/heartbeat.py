#!/usr/bin/env python3
"""
GENESIS Heartbeat Monitor
Runs every 60 minutes. Sends a system health report to Telegram.
If this message stops appearing, the VPS is down.
"""
import os, requests, json, subprocess
from datetime import datetime, timezone
from pathlib import Path

# Load dotenv if exists
try:
    from dotenv import load_dotenv
    load_dotenv()
except:
    pass

TG_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
MT5_API    = os.getenv("MT5_API_URL", "https://mt5.mt4api.dev")
MT5_ID     = os.getenv("MT5_ACCOUNT_ID", "")
MT5_AUTH   = (os.getenv("MT5_API_USER", ""), os.getenv("MT5_API_PASS", ""))
ARES_BRIDGE = MT5_API # Maps to same direct API2TRADE service

# Resolve safe journal paths
default_journal = "/var/log/hermes/trade_journal.jsonl"
try:
    Path(default_journal).parent.mkdir(parents=True, exist_ok=True)
    JOURNAL = Path(default_journal)
except Exception:
    JOURNAL = Path(__file__).parents[1] / "logs" / "hermes" / "trade_journal.jsonl"
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)

default_ares_journal = "/var/log/ares/trade_journal.jsonl"
try:
    Path(default_ares_journal).parent.mkdir(parents=True, exist_ok=True)
    ARES_JOURNAL = Path(default_ares_journal)
except Exception:
    ARES_JOURNAL = Path(__file__).parents[1] / "logs" / "ares" / "trade_journal.jsonl"
    ARES_JOURNAL.parent.mkdir(parents=True, exist_ok=True)

def tg(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json={"chat_id": TG_CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=10)
    except: pass

def check_service(name):
    try:
        r = subprocess.run(["systemctl", "is-active", name], capture_output=True, text=True)
        return r.stdout.strip() == "active"
    except: return False

def main():
    now = datetime.now(timezone.utc)
    issues = []

    # Account state
    balance_str = "N/A"
    equity_str  = "N/A"
    pnl_str     = "N/A"
    try:
        r = requests.get(f"{MT5_API}/AccountSummary", params={"id": MT5_ID}, auth=MT5_AUTH, timeout=5)
        d = r.json()
        balance_str = f"€{d['balance']:.2f}"
        equity_str  = f"€{d['equity']:.2f}"
        pnl_str     = f"€{d.get('profit', 0):.2f}"
    except:
        issues.append("🔴 Bridge DOWN")

    # Open position
    positions_str = "None"
    try:
        r = requests.get(f"{MT5_API}/OpenedOrders", params={"id": MT5_ID}, auth=MT5_AUTH, timeout=5)
        pos = r.json()
        if isinstance(pos, list) and len(pos) > 0:
            p = pos[0]
            positions_str = f"{p.get('symbol')} {p.get('orderType')} {p.get('lots')}lot | P&L: €{p.get('profit', 0):.2f}"
    except:
        issues.append("🔴 Cannot read positions")

    # Service health
    if not check_service("genesis-trading.timer"):
        issues.append("🔴 Trading timer STOPPED")
    if not check_service("hermes-gateway"):
        issues.append("🔴 Hermes gateway STOPPED")
    if not check_service("mt5-bridge"):
        issues.append("🟡 MT5 bridge service not found")
    if not check_service("ares"):
        issues.append("🟡 Ares bot STOPPED")

    # Ares (Account B) state — mapped to same direct account in v2.1
    ares_balance_str = balance_str
    if equity_str != "N/A":
        ares_balance_str = f"{balance_str} (eq {equity_str})"
    
    ares_pos_str = "None"
    if 'pos' in locals() and isinstance(pos, list):
        ares_positions = [
            f"{p.get('symbol')} {p.get('orderType')} {p.get('lots')}lot | P&L: €{p.get('profit', 0):.2f}"
            for p in pos if "ARES" in str(p.get("comment", "")).upper() or "ARES" in str(p.get("Comment", "")).upper()
        ]
        if ares_positions:
            ares_pos_str = ares_positions[0]

    # Ares journal stats
    ares_wins = ares_losses = 0
    if ARES_JOURNAL.exists():
        for line in ARES_JOURNAL.read_text().strip().split("\n"):
            if not line: continue
            try:
                t = json.loads(line)
                if t.get("result") == "win":  ares_wins += 1
                if t.get("result") == "loss": ares_losses += 1
            except: pass

    # Journal stats
    wins, losses = 0, 0
    if JOURNAL.exists():
        for line in JOURNAL.read_text().strip().split("\n"):
            if not line: continue
            try:
                t = json.loads(line)
                if t.get("result") == "win":   wins += 1
                if t.get("result") == "loss":  losses += 1
            except: pass

    status = "✅ ALL SYSTEMS NOMINAL" if not issues else "\n".join(issues)

    msg = (
        f"🤖 *GENESIS HEARTBEAT*\n"
        f"🕐 {now.strftime('%Y-%m-%d %H:%M')} UTC\n\n"
        f"*System Status:* {status}\n\n"
        f"━━━ ⚡ HERMES (Account A) ━━━\n"
        f"💰 Balance: {balance_str}\n"
        f"📊 Equity: {equity_str}\n"
        f"📈 Open P&L: {pnl_str}\n"
        f"🔓 Position: {positions_str}\n"
        f"📒 History: {wins}W / {losses}L\n\n"
        f"━━━ ⚔️ ARES (Account B) ━━━\n"
        f"💰 Balance: {ares_balance_str}\n"
        f"🔓 Position: {ares_pos_str}\n"
        f"📒 History: {ares_wins}W / {ares_losses}L\n\n"
        f"_Next heartbeat in 60 minutes._"
    )
    tg(msg)

if __name__ == "__main__":
    main()
