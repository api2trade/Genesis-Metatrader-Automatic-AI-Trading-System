#!/usr/bin/env python3
"""
GENESIS — Apollo CLI Tool (Strategy C: MA Crossover)
Hermes calls this autonomously — same pattern as ares_tool.py.

Usage:
  python3 apollo_tool.py analyze EURUSD      # MA crossover analysis, no trade
  python3 apollo_tool.py execute EURUSD      # Analyze + execute if signal found
  python3 apollo_tool.py status              # Open Apollo position + journal stats
  python3 apollo_tool.py close               # Close open Apollo position
  python3 apollo_tool.py scan                # Scan all Apollo symbols, return best signal
  python3 apollo_tool.py symbols             # List Apollo symbols
"""
import sys, os, json, time
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))

from apollo_cycle import (
    run_analysis, bridge, JOURNAL, MAGIC_COMMENT,
    pip_size, tg, CFG, _last_signal_time
)
from datetime import datetime, timezone
from pathlib import Path

APOLLO_SYMBOLS = CFG["symbols"]

def journal_write(entry: dict):
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    with open(JOURNAL, "a") as f:
        f.write(json.dumps(entry) + "\n")

def journal_stats() -> tuple[int, int]:
    wins = losses = 0
    if JOURNAL.exists():
        for line in JOURNAL.read_text().strip().split("\n"):
            if not line: continue
            try:
                t = json.loads(line)
                if t.get("result") == "win":  wins += 1
                if t.get("result") == "loss": losses += 1
            except: pass
    return wins, losses

def cmd_analyze(symbol: str) -> dict:
    sym = symbol.upper()
    if not sym.endswith("XX"):
        sym = sym + "xx"
    result = run_analysis(sym)
    print(json.dumps(result, indent=2, default=str))
    return result

def cmd_execute(symbol: str) -> dict:
    """Analyze + execute if valid signal. Hermes calls when it sees fit."""
    sym = symbol.upper()
    if not sym.endswith("XX"):
        sym = sym + "xx"

    result = run_analysis(sym)

    if result.get("action") != "trade":
        out = {
            "executed":        False,
            "reason":          result.get("reason", "No signal"),
            "conditions_met":  result.get("conditions_met", []),
            "signal_type":     result.get("signal_type", "none"),
        }
        print(json.dumps(out, indent=2))
        return out

    order = bridge("/market", "POST", {
        "symbol":      result["symbol"],
        "volume":      result["volume"],
        "type":        result["direction"],
        "stop_loss":   result["stop_loss"],
        "take_profit": result["take_profit"],
        "comment":     MAGIC_COMMENT,   # "APOLLO-v1"
    })

    ticket = order.get("ticket") or order.get("Ticket")
    now    = datetime.now(timezone.utc)

    if ticket:
        journal_write({
            "ticket":       str(ticket),
            "symbol":       result["symbol"],
            "direction":    result["direction"],
            "volume":       result["volume"],
            "sl":           result["stop_loss"],
            "tp":           result["take_profit"],
            "entry":        result["entry"],
            "rr":           result["rr_ratio"],
            "signal_type":  result["signal_type"],
            "opened":       now.isoformat(),
            "result":       None,
            "pnl":          None,
            "strategy":     "apollo-ma-crossover",
            "triggered_by": "hermes-autonomous",
            "conditions":   result.get("conditions_met", []),
        })

        ind = result.get("indicators", {})
        tg(
            f"🏹 *APOLLO TRADE — Hermes Triggered*\n"
            f"📈 `{result['symbol']}` {result['direction']} "
            f"| {result['signal_type'].replace('_',' ')}\n"
            f"Entry: `{result['entry']}` | SL: `{result['stop_loss']}` "
            f"| TP: `{result['take_profit']}`\n"
            f"R:R: `{result['rr_ratio']}` | Vol: `{result['volume']}`\n"
            f"🎯 Strategy: EMA{CFG['indicators']['fast_ma_period']}/"
            f"EMA{CFG['indicators']['slow_ma_period']} Crossover (M5)\n"
            f"ADX: {ind.get('adx','?')} | ATR: {ind.get('atr','?')}\n"
            f"📌 {', '.join(result.get('conditions_met',[])[:3])}"
        )

        out = {
            "executed":    True,
            "ticket":      str(ticket),
            "symbol":      result["symbol"],
            "direction":   result["direction"],
            "signal_type": result["signal_type"],
            "volume":      result["volume"],
            "sl":          result["stop_loss"],
            "tp":          result["take_profit"],
            "rr":          result["rr_ratio"],
            "reason":      result["reason"],
        }
    else:
        err = order.get("message", str(order))
        tg(f"⚠️ *APOLLO*: Order FAILED — `{err}`")
        out = {"executed": False, "reason": f"Order failed: {err}"}

    print(json.dumps(out, indent=2, default=str))
    return out

def cmd_status() -> dict:
    acc      = bridge("/balance")
    positions = bridge("/positions")

    apollo_pos = None
    all_positions = []
    if isinstance(positions, list):
        for p in positions:
            comment = str(p.get("comment", ""))
            all_positions.append({
                "ticket":  p.get("ticket"),
                "symbol":  p.get("symbol"),
                "type":    p.get("orderType"),
                "lots":    p.get("lots"),
                "profit":  p.get("profit"),
                "comment": comment,
            })
            if "APOLLO" in comment.upper():
                apollo_pos = p

    wins, losses = journal_stats()

    out = {
        "account":         acc,
        "apollo_position": apollo_pos,
        "all_open":        all_positions,
        "apollo_journal":  {"wins": wins, "losses": losses},
        "strategy":        "MA Crossover EMA9/EMA21 (M5)",
        "comment_tag":     MAGIC_COMMENT,
    }
    print(json.dumps(out, indent=2, default=str))
    return out

def cmd_close() -> dict:
    positions = bridge("/positions")
    closed = []
    if isinstance(positions, list):
        for p in positions:
            if "APOLLO" in str(p.get("comment", "")).upper():
                res = bridge("/close", "POST", {"ticket": p["ticket"]})
                closed.append({"ticket": p["ticket"], "result": res})
                tg(f"🏹 *APOLLO*: Position `{p['ticket']}` closed by Hermes.")
    out = ({"closed": len(closed), "positions": closed}
           if closed else {"closed": 0, "reason": "No open Apollo positions."})
    print(json.dumps(out, indent=2, default=str))
    return out

def cmd_scan() -> dict:
    """
    Scan ALL Apollo symbols and return the best signal found.
    Hermes uses this to find the strongest crossover opportunity across all pairs.
    """
    best   = None
    best_score = 0
    results = {}

    for sym in APOLLO_SYMBOLS:
        result = run_analysis(sym)
        results[sym] = {
            "action":      result.get("action"),
            "signal_type": result.get("signal_type", "none"),
            "confidence":  result.get("confidence", "none"),
            "rr":          result.get("rr_ratio"),
            "reason":      result.get("reason", "")[:100],
        }
        if result.get("action") == "trade":
            score = (2 if result.get("confidence") == "high" else 1)
            score += (result.get("rr_ratio") or 0) * 0.5
            score += len(result.get("conditions_met", [])) * 0.3
            if score > best_score:
                best_score = score
                best = result
        time.sleep(0.5)  # Rate limit yfinance

    out = {
        "best_signal":  best,
        "scan_results": results,
        "scanned":      len(APOLLO_SYMBOLS),
        "signals_found": sum(1 for r in results.values() if r["action"] == "trade"),
    }
    print(json.dumps(out, indent=2, default=str))
    return out

def cmd_symbols() -> dict:
    out = {
        "symbols":     APOLLO_SYMBOLS,
        "strategy":    "MA Crossover Trend Following",
        "timeframe":   f"{CFG['indicators']['signal_timeframe']} entry, {CFG['indicators']['trend_timeframe']} context",
        "indicators":  f"EMA{CFG['indicators']['fast_ma_period']}/EMA{CFG['indicators']['slow_ma_period']} crossover",
        "comment_tag": MAGIC_COMMENT,
    }
    print(json.dumps(out, indent=2))
    return out

# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print(json.dumps({"error": "Usage: apollo_tool.py [analyze|execute|status|close|scan|symbols] [SYMBOL]"}))
        sys.exit(1)

    cmd = args[0].lower()

    if cmd == "analyze":
        if len(args) < 2:
            print(json.dumps({"error": "analyze requires a symbol"})); sys.exit(1)
        cmd_analyze(args[1])
    elif cmd == "execute":
        if len(args) < 2:
            print(json.dumps({"error": "execute requires a symbol"})); sys.exit(1)
        cmd_execute(args[1])
    elif cmd == "status":
        cmd_status()
    elif cmd == "close":
        cmd_close()
    elif cmd == "scan":
        cmd_scan()
    elif cmd == "symbols":
        cmd_symbols()
    else:
        print(json.dumps({"error": f"Unknown command: {cmd}"})); sys.exit(1)
