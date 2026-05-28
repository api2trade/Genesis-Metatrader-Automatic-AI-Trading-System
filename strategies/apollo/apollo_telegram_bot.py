#!/usr/bin/env python3
"""
GENESIS — Apollo Telegram Bot (Strategy C Command Handler)
Same pattern as ares_telegram_bot.py — manual trigger via Telegram.

Commands:
  /apollo_analyze [SYMBOL]  — MA crossover analysis, no trade
  /apollo_execute           — Execute pending signal
  /apollo_scan              — Scan all symbols, show best
  /apollo_skip              — Cancel pending signal
  /apollo_status            — Account + open Apollo position
  /apollo_help              — All commands
"""
import os, json, time, logging, threading
from datetime import datetime, timezone
from pathlib import Path
import yaml

CONFIG_PATH = Path(__file__).parent / "apollo_config.yaml"
with open(CONFIG_PATH) as f:
    CFG = yaml.safe_load(f)

import requests

TG_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID = str(CFG["telegram"]["chat_id"])
BRIDGE     = CFG["bridge"]["url"]
JOURNAL    = Path(CFG["journal"]["path"])
STRATEGY   = CFG["strategy"]["name"]
COMMENT    = CFG["strategy"]["comment"]

logging.basicConfig(
    filename="/var/log/apollo/apollo_bot.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger(__name__)

_pending: dict = {}
_lock = threading.Lock()

def tg_send(text: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10
        )
    except Exception as e:
        log.error(f"tg_send: {e}")

def tg_updates(offset=0):
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
            params={"timeout": 30, "offset": offset}, timeout=40
        )
        return r.json().get("result", [])
    except:
        return []

def bridge(path, method="GET", data=None):
    try:
        url = f"{BRIDGE}{path}"
        r = (requests.post(url, json=data, timeout=15)
             if method == "POST" else requests.get(url, timeout=15))
        return r.json()
    except Exception as e:
        return {"error": str(e)}

def journal_write(entry: dict):
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    with open(JOURNAL, "a") as f:
        f.write(json.dumps(entry) + "\n")

def journal_stats():
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

# ── Commands ───────────────────────────────────────────────────────────────────
def cmd_help():
    fast = CFG["indicators"]["fast_ma_period"]
    slow = CFG["indicators"]["slow_ma_period"]
    tg_send(
        f"🏹 *{STRATEGY} — Strategy C Commands*\n\n"
        f"`/apollo_analyze [SYMBOL]` — MA crossover analysis (no trade)\n"
        f"`/apollo_scan` — Scan all symbols for best signal\n"
        f"`/apollo_execute` — Execute pending signal\n"
        f"`/apollo_skip` — Cancel pending signal\n"
        f"`/apollo_status` — Open position + journal stats\n"
        f"`/apollo_help` — This message\n\n"
        f"📊 Strategy: EMA{fast}/EMA{slow} Golden/Death Cross on M5\n"
        f"🔖 Tag: `{COMMENT}` | Session: GMT "
        f"{CFG['sessions']['allowed'][0]['start']}:00–"
        f"{CFG['sessions']['allowed'][0]['end']}:00"
    )

def cmd_status():
    acc = bridge("/balance")
    if "error" in acc:
        tg_send(f"🔴 *{STRATEGY}*: Bridge unreachable.")
        return
    positions = bridge("/positions")
    pos_str = "None"
    all_str = []
    if isinstance(positions, list):
        for p in positions:
            comment = str(p.get("comment", ""))
            all_str.append(f"`{p.get('symbol')}` {p.get('orderType')} "
                           f"{p.get('lots')}lot P&L:€{p.get('profit',0):.2f} [{comment}]")
            if "APOLLO" in comment.upper():
                pos_str = (f"{p.get('symbol')} {p.get('orderType')} "
                           f"{p.get('lots')}lot | P&L: €{p.get('profit',0):.2f}")

    wins, losses = journal_stats()
    with _lock:
        pend = (f"🟡 {_pending.get('symbol')} {_pending.get('direction')} "
                f"({_pending.get('signal_type','?')})"
                if _pending else "None")

    all_display = "\n".join(all_str) if all_str else "None"
    tg_send(
        f"🏹 *{STRATEGY} — Status*\n\n"
        f"💰 Balance: €{acc.get('balance',0):.2f} | "
        f"Equity: €{acc.get('equity',0):.2f}\n"
        f"📈 Apollo Position: {pos_str}\n"
        f"📋 Pending Signal: {pend}\n"
        f"📒 Journal: {wins}W / {losses}L\n\n"
        f"*All Open Positions:*\n{all_display}"
    )

def cmd_skip():
    with _lock:
        if not _pending:
            tg_send(f"🏹 *{STRATEGY}*: No pending signal to cancel.")
            return
        sym = _pending.get("symbol")
        _pending.clear()
    tg_send(f"⏭ *{STRATEGY}*: Signal for `{sym}` cancelled.")

def cmd_execute():
    with _lock:
        if not _pending:
            tg_send(f"🏹 *{STRATEGY}*: No pending signal.\n"
                    f"Run `/apollo_analyze SYMBOL` or `/apollo_scan` first.")
            return
        signal = dict(_pending)
        _pending.clear()

    sym  = signal.get("symbol")
    dire = signal.get("direction")
    sl   = signal.get("stop_loss")
    tp   = signal.get("take_profit")
    vol  = signal.get("volume", 0.01)

    if not all([sym, dire, sl, tp]):
        tg_send(f"🏹 *{STRATEGY}*: Incomplete signal — cannot execute.")
        return

    positions = bridge("/positions")
    if isinstance(positions, list) and any(
        "APOLLO" in str(p.get("comment","")).upper() for p in positions
    ):
        tg_send(f"⚠️ *{STRATEGY}*: Apollo position already open. Close it first.")
        return

    tg_send(f"🏹 *{STRATEGY}*: Placing order…")
    order = bridge("/market", "POST", {
        "symbol": sym, "volume": vol, "type": dire,
        "stop_loss": sl, "take_profit": tp, "comment": COMMENT
    })

    ticket = order.get("ticket") or order.get("Ticket")
    if ticket:
        now = datetime.now(timezone.utc)
        journal_write({
            "ticket": str(ticket), "symbol": sym, "direction": dire,
            "volume": vol, "sl": sl, "tp": tp,
            "signal_type": signal.get("signal_type"),
            "rr": signal.get("rr_ratio"),
            "opened": now.isoformat(), "result": None, "pnl": None,
            "strategy": "apollo-ma-crossover"
        })
        ind = signal.get("indicators", {})
        tg_send(
            f"✅ *{STRATEGY} TRADE PLACED*\n"
            f"📈 `{sym}` {dire} | {signal.get('signal_type','').replace('_',' ')}\n"
            f"Entry: `{signal.get('entry')}` | SL: `{sl}` | TP: `{tp}`\n"
            f"R:R: `{signal.get('rr_ratio')}` | Vol: `{vol}`\n"
            f"ADX: {ind.get('adx','?')} | ATR: {ind.get('atr','?')}\n"
            f"🔖 Ticket: `{ticket}`"
        )
    else:
        err = order.get("message", str(order))
        tg_send(f"❌ *{STRATEGY}*: Order FAILED — `{err}`")

def cmd_analyze(symbol: str):
    sym = symbol.upper().strip()
    if not sym.endswith("XX"):
        sym = sym + "xx"

    tg_send(f"🏹 *{STRATEGY}*: Analysing `{sym}`… (30–60s)")

    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "apollo_cycle", Path(__file__).parent / "apollo_cycle.py"
        )
        mod = importlib.util.load_from_spec(spec)
        spec.loader.exec_module(mod)
        result = mod.run_analysis(sym)
    except Exception as e:
        log.error(f"Analysis error: {e}", exc_info=True)
        tg_send(f"❌ *{STRATEGY}*: Analysis failed — `{str(e)[:200]}`")
        return

    _send_analysis_result(result)

def cmd_scan():
    tg_send(f"🏹 *{STRATEGY}*: Scanning all symbols… (may take 60–90s)")
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "apollo_cycle", Path(__file__).parent / "apollo_cycle.py"
        )
        mod = importlib.util.load_from_spec(spec)
        spec.loader.exec_module(mod)

        symbols  = CFG["symbols"]
        best     = None
        best_rr  = 0
        scan_lines = []

        for sym in symbols:
            r = mod.run_analysis(sym)
            action = r.get("action", "wait")
            if action == "trade":
                rr = r.get("rr_ratio", 0) or 0
                icon = "🟢"
                scan_lines.append(
                    f"{icon} `{sym}`: {r.get('direction')} "
                    f"{r.get('signal_type','').replace('_',' ')} | "
                    f"R:R {rr} | {r.get('confidence','?')}"
                )
                if rr > best_rr:
                    best_rr = rr
                    best = r
            else:
                scan_lines.append(f"⚪ `{sym}`: {r.get('reason','wait')[:60]}")
            import time; time.sleep(0.5)

        summary = "\n".join(scan_lines)
        tg_send(f"🏹 *{STRATEGY} SCAN RESULTS*\n\n{summary}")

        if best:
            with _lock:
                _pending.clear()
                _pending.update(best)
            _send_analysis_result(best, from_scan=True)
        else:
            tg_send(f"📊 *{STRATEGY}*: No trade signals found across all symbols.")

    except Exception as e:
        log.error(f"Scan error: {e}", exc_info=True)
        tg_send(f"❌ *{STRATEGY}*: Scan failed — `{str(e)[:200]}`")

def _send_analysis_result(result: dict, from_scan: bool = False):
    """Format and send analysis result to Telegram, set pending if trade signal."""
    action = result.get("action", "wait")

    if action != "trade":
        tg_send(
            f"🏹 *{STRATEGY}* — `{result.get('symbol','?')}`\n\n"
            f"📊 Signal: *WAIT*\n"
            f"💡 {result.get('reason','')[:300]}"
        )
        return

    with _lock:
        _pending.clear()
        _pending.update(result)

    conds    = result.get("conditions_met", [])
    cond_str = "\n".join(f"  ✅ {c}" for c in conds) if conds else "  (see reason)"
    warn_str = ""
    if result.get("warnings"):
        warn_str = "\n" + "\n".join(f"  ⚠️ {w}" for w in result["warnings"])

    ind  = result.get("indicators", {})
    fast = CFG["indicators"]["fast_ma_period"]
    slow = CFG["indicators"]["slow_ma_period"]
    scan_note = " _(Best from scan)_" if from_scan else ""

    tg_send(
        f"🏹 *{STRATEGY} ANALYSIS*{scan_note} — `{result.get('symbol')}`\n\n"
        f"📊 Signal: *{result.get('direction')}* — "
        f"{result.get('signal_type','').replace('_',' ')}\n"
        f"Entry: `{result.get('entry')}`\n"
        f"SL:    `{result.get('stop_loss')}` ({result.get('sl_pips','?')} pips)\n"
        f"TP:    `{result.get('take_profit')}` ({result.get('tp_pips','?')} pips)\n"
        f"R:R    `{result.get('rr_ratio')}` | Vol: `{result.get('volume')}`\n"
        f"🎯 Confidence: {result.get('confidence','?')}\n"
        f"ADX: {ind.get('adx','?')} | ATR: {ind.get('atr','?')}\n"
        f"EMA{fast}: {ind.get('fast_ma','?')} | EMA{slow}: {ind.get('slow_ma','?')}\n\n"
        f"📌 *Conditions ({len(conds)} met):*\n{cond_str}{warn_str}\n\n"
        f"💡 {result.get('reason','')[:250]}\n\n"
        f"Reply `/apollo_execute` to trade or `/apollo_skip` to cancel."
    )

# ── Dispatcher ─────────────────────────────────────────────────────────────────
def dispatch(text: str, from_id: str):
    if str(from_id) != TG_CHAT_ID:
        log.warning(f"Unauthorised: {from_id}")
        return
    text  = text.strip()
    lower = text.lower()

    if lower.startswith("/apollo_analyze"):
        parts = text.split(maxsplit=1)
        sym   = parts[1] if len(parts) > 1 else ""
        if not sym:
            tg_send("Usage: `/apollo_analyze EURUSD`")
        else:
            threading.Thread(target=cmd_analyze, args=(sym,), daemon=True).start()
    elif lower == "/apollo_scan":
        threading.Thread(target=cmd_scan, daemon=True).start()
    elif lower == "/apollo_execute":
        threading.Thread(target=cmd_execute, daemon=True).start()
    elif lower == "/apollo_skip":
        cmd_skip()
    elif lower == "/apollo_status":
        threading.Thread(target=cmd_status, daemon=True).start()
    elif lower in ("/apollo_help", "/apollo"):
        cmd_help()

# ── Main loop ──────────────────────────────────────────────────────────────────
def main():
    log.info(f"=== {STRATEGY} Telegram Bot started ===")
    fast = CFG["indicators"]["fast_ma_period"]
    slow = CFG["indicators"]["slow_ma_period"]
    tg_send(
        f"🏹 *{STRATEGY} Bot Online*\n"
        f"Strategy C: EMA{fast}/EMA{slow} Trend Following (M5)\n"
        f"Send `/apollo_help` to see commands.\n\n"
        f"🤖 _Hermes controls this bot autonomously._\n"
        f"_You can also trigger manually via the commands above._"
    )
    offset = 0
    while True:
        try:
            updates = tg_updates(offset)
            for upd in updates:
                offset = upd["update_id"] + 1
                msg    = upd.get("message", {})
                text   = msg.get("text", "")
                chat_id = str(msg.get("chat", {}).get("id", ""))
                if text.startswith("/apollo"):
                    dispatch(text, chat_id)
        except Exception as e:
            log.error(f"Polling error: {e}")
            time.sleep(5)
        time.sleep(1)

if __name__ == "__main__":
    main()
