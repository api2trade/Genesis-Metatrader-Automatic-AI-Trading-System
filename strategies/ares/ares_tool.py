#!/usr/bin/env python3
"""
GENESIS — Ares Strategy B CLI Tool
Called by Hermes autonomously as a shell command.

Usage:
  python3 ares_tool.py analyze EURUSD        # Run BB+RSI analysis, returns JSON
  python3 ares_tool.py execute EURUSD        # Analyze + auto-execute if signal found
  python3 ares_tool.py status                # Account state + open Ares position
  python3 ares_tool.py close                 # Close any open Ares position
  python3 ares_tool.py symbols               # List tradeable symbols for Ares

Hermes uses this tool independently — completely separate from trading_cycle.py.
Ares uses: BB(20,2) + RSI(14) mean reversion on M1 with M15 SMA50 context.
"""
import sys, os, json
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))

from ares_cycle import run_analysis, bridge, JOURNAL, MAGIC_COMMENT, pip_size, tg
from datetime import datetime, timezone
from pathlib import Path

ARES_SYMBOLS = ["EURUSDxx", "XAUUSDxx", "GBPUSDxx", "USDJPYxx", "GBPJPYxx"]

def journal_write(entry: dict):
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    with open(JOURNAL, "a") as f:
        f.write(json.dumps(entry) + "\n")

def cmd_analyze(symbol: str) -> dict:
    """Run full Ares analysis. Returns signal dict."""
    sym = symbol.upper()
    if not sym.endswith("XX"):
        sym = sym[:-2].upper() + "xx" if sym.endswith("xx") else sym + "xx"
    result = run_analysis(sym)
    print(json.dumps(result, indent=2, default=str))
    return result

def cmd_execute(symbol: str) -> dict:
    """
    Analyze + execute if signal found. Hermes calls this when it decides
    the Ares strategy conditions are right. Trades on the main account
    with ARES-v1 comment tag so it's distinguishable from Hermes trades.
    """
    sym = symbol.upper()
    if not sym.endswith("XX"):
        sym = sym + "xx"

    result = run_analysis(sym)

    if result.get("action") != "trade":
        print(json.dumps({
            "executed": False,
            "reason": result.get("reason", "No signal"),
            "conditions_met": result.get("conditions_met", []),
        }, indent=2))
        return result

    # Place the order
    order = bridge("/market", "POST", {
        "symbol":      result["symbol"],
        "volume":      result["volume"],
        "type":        result["direction"],
        "stop_loss":   result["stop_loss"],
        "take_profit": result["take_profit"],
        "comment":     MAGIC_COMMENT,   # "ARES-v1" — distinguishes from GENESIS-v2
    })

    ticket = order.get("ticket") or order.get("Ticket")
    now    = datetime.now(timezone.utc)

    if ticket:
        journal_write({
            "ticket":    str(ticket),
            "symbol":    result["symbol"],
            "direction": result["direction"],
            "volume":    result["volume"],
            "sl":        result["stop_loss"],
            "tp":        result["take_profit"],
            "entry":     result["entry"],
            "rr":        result["rr_ratio"],
            "opened":    now.isoformat(),
            "result":    None,
            "pnl":       None,
            "strategy":  "ares-bb-rsi",
            "triggered_by": "hermes-autonomous",
            "conditions": result.get("conditions_met", []),
        })

        msg = (
            f"⚔️ *ARES TRADE — Hermes Triggered*\n"
            f"📈 `{result['symbol']}` {result['direction']} | Vol: {result['volume']}\n"
            f"Entry: `{result['entry']}` | SL: `{result['stop_loss']}` | TP: `{result['take_profit']}`\n"
            f"R:R: `{result['rr_ratio']}` | Confidence: {result['confidence']}\n"
            f"🎯 Strategy: BB+RSI Mean Reversion (M1)\n"
            f"📌 {', '.join(result.get('conditions_met', [])[:3])}"
        )
        tg(msg)

        output = {
            "executed": True,
            "ticket":   str(ticket),
            "symbol":   result["symbol"],
            "direction": result["direction"],
            "volume":   result["volume"],
            "sl":       result["stop_loss"],
            "tp":       result["take_profit"],
            "rr":       result["rr_ratio"],
            "reason":   result["reason"],
        }
    else:
        err = order.get("message", str(order))
        output = {"executed": False, "reason": f"Order failed: {err}"}
        tg(f"⚠️ *ARES*: Order FAILED — `{err}`")

    print(json.dumps(output, indent=2, default=str))
    return output

def cmd_status() -> dict:
    """Return current account state and any open Ares position."""
    acc = bridge("/balance")
    positions = bridge("/positions")

    ares_pos = None
    if isinstance(positions, list):
        for p in positions:
            if MAGIC_COMMENT.split("-")[0] in str(p.get("comment", "")):
                ares_pos = p
                break

    # Journal stats
    wins = losses = 0
    if JOURNAL.exists():
        for line in JOURNAL.read_text().strip().split("\n"):
            if not line: continue
            try:
                t = json.loads(line)
                if t.get("result") == "win":  wins += 1
                if t.get("result") == "loss": losses += 1
            except: pass

    output = {
        "account": acc,
        "ares_position": ares_pos,
        "ares_journal": {"wins": wins, "losses": losses},
        "strategy": "BB+RSI Mean Reversion M1",
    }
    print(json.dumps(output, indent=2, default=str))
    return output

def cmd_close() -> dict:
    """Close any open Ares position."""
    positions = bridge("/positions")
    closed = []
    if isinstance(positions, list):
        for p in positions:
            if MAGIC_COMMENT.split("-")[0] in str(p.get("comment", "")):
                result = bridge("/close", "POST", {"ticket": p["ticket"]})
                closed.append({"ticket": p["ticket"], "result": result})
                tg(f"⚔️ *ARES*: Position `{p['ticket']}` closed by Hermes.")

    if not closed:
        output = {"closed": 0, "reason": "No open Ares positions found."}
    else:
        output = {"closed": len(closed), "positions": closed}

    print(json.dumps(output, indent=2, default=str))
    return output

def cmd_symbols() -> dict:
    output = {
        "symbols": ARES_SYMBOLS,
        "description": "Ares BB+RSI strategy — optimised for low-spread majors",
        "timeframe": "M1 entry, M15 context",
        "strategy": "Mean reversion: price outside Bollinger Band + RSI extreme",
    }
    print(json.dumps(output, indent=2))
    return output

# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print(json.dumps({"error": "Usage: ares_tool.py [analyze|execute|status|close|symbols] [SYMBOL]"}))
        sys.exit(1)

    cmd = args[0].lower()

    if cmd == "analyze":
        if len(args) < 2:
            print(json.dumps({"error": "analyze requires a symbol, e.g.: ares_tool.py analyze EURUSD"}))
            sys.exit(1)
        cmd_analyze(args[1])

    elif cmd == "execute":
        if len(args) < 2:
            print(json.dumps({"error": "execute requires a symbol, e.g.: ares_tool.py execute EURUSD"}))
            sys.exit(1)
        cmd_execute(args[1])

    elif cmd == "status":
        cmd_status()

    elif cmd == "close":
        cmd_close()

    elif cmd == "symbols":
        cmd_symbols()

    else:
        print(json.dumps({"error": f"Unknown command: {cmd}. Use: analyze, execute, status, close, symbols"}))
        sys.exit(1)
