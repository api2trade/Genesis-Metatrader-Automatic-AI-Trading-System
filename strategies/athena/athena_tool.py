#!/usr/bin/env python3
"""
GENESIS — Athena CLI Tool (Strategy D: BB+RSI Mean Reversion M5)
Hermes calls this autonomously — same pattern as ares_tool.py / apollo_tool.py.

Usage:
  python3 athena_tool.py analyze EURUSD     # BB+RSI analysis, no trade
  python3 athena_tool.py execute EURUSD     # Analyze + execute if signal found
  python3 athena_tool.py scan               # Scan all symbols, return best signal
  python3 athena_tool.py status             # Open Athena position + journal stats
  python3 athena_tool.py close              # Close open Athena position
  python3 athena_tool.py symbols            # List Athena symbols
"""
import sys, os, json, time
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))

from athena_cycle import (
    run_analysis, bridge, JOURNAL, MAGIC_COMMENT,
    tg, CFG, _last_signal_time, pip_size
)
from datetime import datetime, timezone
from pathlib import Path

ATHENA_SYMBOLS = CFG["symbols"]

def journal_write(entry: dict):
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    with open(JOURNAL, "a") as f:
        f.write(json.dumps(entry) + "\n")

def journal_stats():
    wins = losses = total_pnl = 0.0
    if JOURNAL.exists():
        for line in JOURNAL.read_text().strip().split("\n"):
            if not line: continue
            try:
                t = json.loads(line)
                if t.get("result") == "win":  wins += 1
                if t.get("result") == "loss": losses += 1
                total_pnl += float(t.get("pnl") or 0)
            except: pass
    return int(wins), int(losses), round(total_pnl, 2)

def cmd_analyze(symbol: str) -> dict:
    sym = symbol.upper()
    if not sym.endswith("XX"):
        sym = sym + "xx"
    result = run_analysis(sym)
    print(json.dumps(result, indent=2, default=str))
    return result

def cmd_execute(symbol: str) -> dict:
    sym = symbol.upper()
    if not sym.endswith("XX"):
        sym = sym + "xx"

    result = run_analysis(sym)

    if result.get("action") != "trade":
        out = {
            "executed":       False,
            "reason":         result.get("reason","No signal"),
            "confidence":     result.get("confidence","none"),
            "conditions_met": result.get("conditions_met",[]),
        }
        print(json.dumps(out, indent=2))
        return out

    order = bridge("/market", "POST", {
        "symbol":      result["symbol"],
        "volume":      result["volume"],
        "type":        result["direction"],
        "stop_loss":   result["stop_loss"],
        "take_profit": result["take_profit"],
        "comment":     MAGIC_COMMENT,   # "ATHENA-v1"
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
            "signal_type":  result.get("signal_type"),
            "confidence":   result.get("confidence"),
            "opened":       now.isoformat(),
            "result":       None,
            "pnl":          None,
            "strategy":     "athena-bb-rsi-m5",
            "triggered_by": "hermes-autonomous",
            "conditions":   result.get("conditions_met",[]),
        })

        ind = result.get("indicators",{})
        bb_period   = CFG["indicators"]["bb_period"]
        rsi_period  = CFG["indicators"]["rsi_period"]
        tg(
            f"🌿 *ATHENA TRADE — Hermes Triggered*\n"
            f"📊 `{result['symbol']}` {result['direction']} "
            f"| {result.get('signal_type','').replace('_',' ')}\n"
            f"Entry: `{result['entry']}` | SL: `{result['stop_loss']}` "
            f"| TP: `{result['take_profit']}`\n"
            f"R:R: `{result['rr_ratio']}` | Vol: `{result['volume']}` "
            f"| Confidence: {result.get('confidence','?')}\n"
            f"🎯 BB({bb_period},2.0)+RSI({rsi_period}) on M5\n"
            f"RSI: {ind.get('rsi','?')} | ATR: {ind.get('atr','?')} "
            f"| ADX: {ind.get('adx','?')}\n"
            f"📌 {', '.join(result.get('conditions_met',[])[:3])}\n"
            f"🔖 Ticket: `{ticket}`"
        )

        out = {
            "executed":    True,
            "ticket":      str(ticket),
            "symbol":      result["symbol"],
            "direction":   result["direction"],
            "signal_type": result.get("signal_type"),
            "volume":      result["volume"],
            "sl":          result["stop_loss"],
            "tp":          result["take_profit"],
            "rr":          result["rr_ratio"],
            "confidence":  result.get("confidence"),
            "reason":      result["reason"],
        }
    else:
        err = order.get("message", str(order))
        tg(f"⚠️ *ATHENA*: Order FAILED — `{err}`")
        out = {"executed":False,"reason":f"Order failed: {err}"}

    print(json.dumps(out, indent=2, default=str))
    return out

def cmd_scan() -> dict:
    """Scan ALL symbols, return the best BB+RSI signal. Hermes calls this first."""
    best       = None
    best_score = 0
    results    = {}

    for sym in ATHENA_SYMBOLS:
        r      = run_analysis(sym)
        action = r.get("action","wait")
        results[sym] = {
            "action":      action,
            "signal_type": r.get("signal_type","none"),
            "confidence":  r.get("confidence","none"),
            "rr":          r.get("rr_ratio"),
            "reason":      r.get("reason","")[:100],
        }
        if action == "trade":
            conf_score = {"high":3,"medium":2,"low":1}.get(r.get("confidence","low"),1)
            score      = conf_score + (r.get("rr_ratio") or 0) * 0.5
            score     += len(r.get("conditions_met",[])) * 0.2
            if score > best_score:
                best_score = score
                best = r
        time.sleep(0.5)

    out = {
        "best_signal":   best,
        "scan_results":  results,
        "scanned":       len(ATHENA_SYMBOLS),
        "signals_found": sum(1 for r in results.values() if r["action"]=="trade"),
    }
    print(json.dumps(out, indent=2, default=str))
    return out

def cmd_status() -> dict:
    acc       = bridge("/balance")
    positions = bridge("/positions")

    athena_pos  = None
    all_pos_str = []
    if isinstance(positions, list):
        for p in positions:
            c = str(p.get("comment",""))
            all_pos_str.append({
                "ticket":  p.get("ticket"),
                "symbol":  p.get("symbol"),
                "type":    p.get("orderType"),
                "lots":    p.get("lots"),
                "profit":  p.get("profit"),
                "comment": c,
            })
            if "ATHENA" in c.upper():
                athena_pos = p

    wins, losses, pnl = journal_stats()
    out = {
        "account":         acc,
        "athena_position": athena_pos,
        "all_open":        all_pos_str,
        "athena_journal":  {"wins": wins, "losses": losses, "total_pnl": pnl},
        "strategy":        "BB+RSI Mean Reversion M5",
        "comment_tag":     MAGIC_COMMENT,
    }
    print(json.dumps(out, indent=2, default=str))
    return out

def cmd_close() -> dict:
    positions = bridge("/positions")
    closed = []
    if isinstance(positions, list):
        for p in positions:
            if "ATHENA" in str(p.get("comment","")).upper():
                res = bridge("/close","POST",{"ticket": p["ticket"]})
                closed.append({"ticket": p["ticket"], "result": res})
                tg(f"🌿 *ATHENA*: Position `{p['ticket']}` closed by Hermes.")
    out = ({"closed": len(closed),"positions": closed}
           if closed else {"closed":0,"reason":"No open Athena positions."})
    print(json.dumps(out, indent=2, default=str))
    return out

def cmd_symbols() -> dict:
    bb  = CFG["indicators"]["bb_period"]
    dev = CFG["indicators"]["bb_deviation"]
    rsi = CFG["indicators"]["rsi_period"]
    out = {
        "symbols":     ATHENA_SYMBOLS,
        "strategy":    "BB+RSI Mean Reversion",
        "timeframe":   f"{CFG['indicators']['signal_timeframe']} entry, "
                       f"{CFG['indicators']['trend_timeframe']} context",
        "indicators":  f"BB({bb},{dev}) + RSI({rsi})",
        "comment_tag": MAGIC_COMMENT,
        "risk":        f"{CFG['risk']['risk_pct']*100}% per trade",
    }
    print(json.dumps(out, indent=2))
    return out

# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print(json.dumps({"error":"Usage: athena_tool.py [analyze|execute|scan|status|close|symbols] [SYMBOL]"}))
        sys.exit(1)
    cmd = args[0].lower()
    if cmd == "analyze":
        if len(args)<2: print(json.dumps({"error":"analyze requires a symbol"})); sys.exit(1)
        cmd_analyze(args[1])
    elif cmd == "execute":
        if len(args)<2: print(json.dumps({"error":"execute requires a symbol"})); sys.exit(1)
        cmd_execute(args[1])
    elif cmd == "scan":
        cmd_scan()
    elif cmd == "status":
        cmd_status()
    elif cmd == "close":
        cmd_close()
    elif cmd == "symbols":
        cmd_symbols()
    else:
        print(json.dumps({"error":f"Unknown: {cmd}"})); sys.exit(1)
