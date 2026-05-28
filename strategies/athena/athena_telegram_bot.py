#!/usr/bin/env python3
"""
GENESIS — Athena Telegram Bot (Strategy D Command Handler)
Same pattern as ares/apollo bots. Hermes controls autonomously.

Commands:
  /athena_analyze [SYMBOL]  — BB+RSI analysis on M5, no trade
  /athena_scan              — Scan all symbols, queue best signal
  /athena_execute           — Execute pending signal
  /athena_skip              — Cancel pending signal
  /athena_status            — Account + open Athena position + journal
  /athena_help              — All commands
"""
import os, json, time, logging, threading
from datetime import datetime, timezone
from pathlib import Path
import yaml, requests

CONFIG_PATH = Path(__file__).parent / "athena_config.yaml"
if not CONFIG_PATH.exists():
    CONFIG_PATH = Path(__file__).parents[2] / "configs" / "athena_config.yaml"
with open(CONFIG_PATH) as f:
    CFG = yaml.safe_load(f)

TG_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID = str(CFG["telegram"]["chat_id"])
BRIDGE     = CFG["bridge"]["url"]
# Resolve safe journal path (fallback to local logs/ if system dir not writable)
default_journal = CFG["journal"]["path"]
try:
    Path(default_journal).parent.mkdir(parents=True, exist_ok=True)
    JOURNAL = Path(default_journal)
except Exception:
    local_log_dir = Path(__file__).parents[2] / "logs" / "athena"
    local_log_dir.mkdir(parents=True, exist_ok=True)
    JOURNAL = local_log_dir / "trade_journal.jsonl"
STRATEGY   = CFG["strategy"]["name"]
COMMENT    = CFG["strategy"]["comment"]

# Resolve safe log path (fallback to local logs/ if system dir not writable)
default_log = "/var/log/athena/athena_bot.log"
try:
    Path(default_log).parent.mkdir(parents=True, exist_ok=True)
    log_file = default_log
except Exception:
    local_log_dir = Path(__file__).parents[2] / "logs" / "athena"
    local_log_dir.mkdir(parents=True, exist_ok=True)
    log_file = str(local_log_dir / "athena_bot.log")

logging.basicConfig(
    filename=log_file,
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

def bridge_call(path, method="GET", data=None):
    try:
        url = f"{BRIDGE}{path}"
        r   = (requests.post(url, json=data, timeout=15)
               if method == "POST" else requests.get(url, timeout=15))
        return r.json()
    except Exception as e:
        return {"error": str(e)}

def journal_stats():
    wins = losses = 0
    pnl  = 0.0
    if JOURNAL.exists():
        for line in JOURNAL.read_text().strip().split("\n"):
            if not line: continue
            try:
                t = json.loads(line)
                if t.get("result") == "win":  wins += 1
                if t.get("result") == "loss": losses += 1
                pnl += float(t.get("pnl") or 0)
            except: pass
    return wins, losses, round(pnl, 2)

def cmd_help():
    bb  = CFG["indicators"]["bb_period"]
    dev = CFG["indicators"]["bb_deviation"]
    rsi = CFG["indicators"]["rsi_period"]
    tf  = CFG["indicators"]["signal_timeframe"]
    tg_send(
        f"🌿 *{STRATEGY} — Strategy D Commands*\n\n"
        f"`/athena_analyze [SYMBOL]` — BB+RSI analysis (no trade)\n"
        f"`/athena_scan` — Scan all {len(CFG['symbols'])} symbols\n"
        f"`/athena_execute` — Execute pending signal\n"
        f"`/athena_skip` — Cancel pending signal\n"
        f"`/athena_status` — Position + journal stats\n"
        f"`/athena_help` — This message\n\n"
        f"📊 Strategy: BB({bb},{dev})+RSI({rsi}) on {tf}\n"
        f"🎯 Risk: {CFG['risk']['risk_pct']*100}% | ATR-based SL/TP\n"
        f"🔖 Tag: `{COMMENT}`"
    )

def cmd_status():
    acc = bridge_call("/balance")
    if "error" in acc:
        tg_send(f"🔴 *{STRATEGY}*: Bridge unreachable."); return
    positions = bridge_call("/positions")
    pos_str = "None"
    all_str = []
    if isinstance(positions, list):
        for p in positions:
            c = str(p.get("comment",""))
            all_str.append(f"`{p.get('symbol')}` {p.get('orderType')} "
                           f"{p.get('lots')}lot €{p.get('profit',0):.2f} [{c}]")
            if "ATHENA" in c.upper():
                pos_str = f"{p.get('symbol')} {p.get('orderType')} | €{p.get('profit',0):.2f}"
    wins, losses, pnl = journal_stats()
    with _lock:
        pend = (f"🟡 {_pending.get('symbol')} {_pending.get('direction')} "
                f"({_pending.get('signal_type','?')})"
                if _pending else "None")
    tg_send(
        f"🌿 *{STRATEGY} — Status*\n\n"
        f"💰 Balance: €{acc.get('balance',0):.2f} | Equity: €{acc.get('equity',0):.2f}\n"
        f"📈 Athena Position: {pos_str}\n"
        f"📋 Pending: {pend}\n"
        f"📒 Journal: {wins}W / {losses}L | PnL: €{pnl}\n\n"
        f"*All Open:*\n" + ("\n".join(all_str) if all_str else "None")
    )

def cmd_skip():
    with _lock:
        if not _pending:
            tg_send(f"🌿 *{STRATEGY}*: No pending signal."); return
        sym = _pending.get("symbol")
        _pending.clear()
    tg_send(f"⏭ *{STRATEGY}*: Signal for `{sym}` cancelled.")

def cmd_execute():
    with _lock:
        if not _pending:
            tg_send(f"🌿 *{STRATEGY}*: No pending signal.\nRun `/athena_analyze SYMBOL` or `/athena_scan` first.")
            return
        signal = dict(_pending)
        _pending.clear()

    sym  = signal.get("symbol")
    dire = signal.get("direction")
    sl   = signal.get("stop_loss")
    tp   = signal.get("take_profit")
    vol  = signal.get("volume", 0.01)

    if not all([sym, dire, sl, tp]):
        tg_send(f"🌿 *{STRATEGY}*: Incomplete signal — cannot execute."); return

    positions = bridge_call("/positions")
    if isinstance(positions, list) and any(
        "ATHENA" in str(p.get("comment","")).upper() for p in positions
    ):
        tg_send(f"⚠️ *{STRATEGY}*: Athena position already open."); return

    tg_send(f"🌿 *{STRATEGY}*: Placing order…")
    order = bridge_call("/market","POST",{
        "symbol": sym,"volume": vol,"type": dire,
        "stop_loss": sl,"take_profit": tp,"comment": COMMENT
    })

    ticket = order.get("ticket") or order.get("Ticket")
    if ticket:
        now = datetime.now(timezone.utc)
        Path(JOURNAL).parent.mkdir(parents=True, exist_ok=True)
        with open(JOURNAL,"a") as f:
            f.write(json.dumps({
                "ticket": str(ticket),"symbol": sym,"direction": dire,
                "volume": vol,"sl": sl,"tp": tp,
                "signal_type": signal.get("signal_type"),
                "confidence": signal.get("confidence"),
                "rr": signal.get("rr_ratio"),
                "opened": now.isoformat(),"result": None,"pnl": None,
                "strategy": "athena-bb-rsi-m5"
            }) + "\n")
        ind = signal.get("indicators",{})
        tg_send(
            f"✅ *{STRATEGY} TRADE PLACED*\n"
            f"📊 `{sym}` {dire} | {signal.get('signal_type','').replace('_',' ')}\n"
            f"Entry: `{signal.get('entry')}` | SL: `{sl}` | TP: `{tp}`\n"
            f"R:R: `{signal.get('rr_ratio')}` | Vol: `{vol}` "
            f"| Confidence: {signal.get('confidence','?')}\n"
            f"RSI: {ind.get('rsi','?')} | ATR: {ind.get('atr','?')}\n"
            f"🔖 Ticket: `{ticket}`"
        )
    else:
        err = order.get("message",str(order))
        tg_send(f"❌ *{STRATEGY}*: Order FAILED — `{err}`")

def _run_analysis(sym):
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "athena_cycle", Path(__file__).parent / "athena_cycle.py"
        )
        mod = importlib.util.load_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.run_analysis(sym)
    except Exception as e:
        log.error(f"Analysis error: {e}", exc_info=True)
        return {"action":"wait","reason":f"Error: {str(e)[:200]}"}

def cmd_analyze(symbol: str):
    sym = symbol.upper().strip()
    if not sym.endswith("XX"):
        sym = sym + "xx"
    tg_send(f"🌿 *{STRATEGY}*: Analysing `{sym}` on M5… (30–60s)")
    result = _run_analysis(sym)
    _format_and_send(result)

def cmd_scan():
    tg_send(f"🌿 *{STRATEGY}*: Scanning {len(CFG['symbols'])} symbols… (60–90s)")
    symbols  = CFG["symbols"]
    best     = None
    best_rr  = 0
    lines    = []

    for sym in symbols:
        r      = _run_analysis(sym)
        action = r.get("action","wait")
        if action == "trade":
            rr   = r.get("rr_ratio",0) or 0
            conf = r.get("confidence","?")
            lines.append(f"🟢 `{sym}`: {r.get('direction')} "
                         f"{r.get('signal_type','').replace('_',' ')} "
                         f"R:R {rr} | {conf}")
            if rr > best_rr:
                best_rr = rr
                best    = r
        else:
            lines.append(f"⚪ `{sym}`: {r.get('reason','')[:60]}")
        time.sleep(0.5)

    tg_send(f"🌿 *{STRATEGY} SCAN*\n\n" + "\n".join(lines))

    if best:
        with _lock:
            _pending.clear()
            _pending.update(best)
        _format_and_send(best, from_scan=True)
    else:
        tg_send(f"📊 *{STRATEGY}*: No trade signals found.")

def _format_and_send(result: dict, from_scan: bool = False):
    action = result.get("action","wait")
    if action != "trade":
        tg_send(
            f"🌿 *{STRATEGY}* — `{result.get('symbol','?')}`\n\n"
            f"Signal: *WAIT*\n💡 {result.get('reason','')[:300]}"
        )
        return

    with _lock:
        _pending.clear()
        _pending.update(result)

    ind      = result.get("indicators",{})
    conds    = result.get("conditions_met",[])
    cond_str = "\n".join(f"  ✅ {c}" for c in conds) if conds else "  BB + RSI conditions met"
    scan_tag = " _(Best from scan)_" if from_scan else ""

    tg_send(
        f"🌿 *{STRATEGY} ANALYSIS*{scan_tag} — `{result.get('symbol')}`\n\n"
        f"Signal: *{result.get('direction')}* — "
        f"{result.get('signal_type','').replace('_',' ')}\n"
        f"Entry: `{result.get('entry')}`\n"
        f"SL:    `{result.get('stop_loss')}` ({result.get('sl_pips','?')} pips)\n"
        f"TP:    `{result.get('take_profit')}` ({result.get('tp_pips','?')} pips)\n"
        f"R:R:   `{result.get('rr_ratio')}` | Vol: `{result.get('volume')}`\n"
        f"🎯 Confidence: {result.get('confidence','?')}\n"
        f"RSI: {ind.get('rsi','?')} | ATR: {ind.get('atr','?')} "
        f"| ADX: {ind.get('adx','?')}\n\n"
        f"📌 *Conditions ({len(conds)} fired):*\n{cond_str}\n\n"
        f"💡 {result.get('reason','')[:250]}\n\n"
        f"Reply `/athena_execute` to trade or `/athena_skip` to cancel."
    )

def dispatch(text: str, from_id: str):
    if str(from_id) != TG_CHAT_ID:
        return
    lower = text.lower().strip()
    if lower.startswith("/athena_analyze"):
        parts = text.split(maxsplit=1)
        sym   = parts[1] if len(parts) > 1 else ""
        if not sym:
            tg_send("Usage: `/athena_analyze EURUSD`")
        else:
            threading.Thread(target=cmd_analyze, args=(sym,), daemon=True).start()
    elif lower == "/athena_scan":
        threading.Thread(target=cmd_scan, daemon=True).start()
    elif lower == "/athena_execute":
        threading.Thread(target=cmd_execute, daemon=True).start()
    elif lower == "/athena_skip":
        cmd_skip()
    elif lower == "/athena_status":
        threading.Thread(target=cmd_status, daemon=True).start()
    elif lower in ("/athena_help", "/athena"):
        cmd_help()

def main():
    log.info(f"=== {STRATEGY} Telegram Bot started ===")
    bb  = CFG["indicators"]["bb_period"]
    dev = CFG["indicators"]["bb_deviation"]
    rsi = CFG["indicators"]["rsi_period"]
    tf  = CFG["indicators"]["signal_timeframe"]
    tg_send(
        f"🌿 *{STRATEGY} Bot Online*\n"
        f"Strategy D: BB({bb},{dev})+RSI({rsi}) Mean Reversion on {tf}\n"
        f"Send `/athena_help` to see commands.\n\n"
        f"🤖 _Hermes controls this bot autonomously._\n"
        f"_You can also trigger manually via the commands above._"
    )
    offset = 0
    while True:
        try:
            updates = tg_updates(offset)
            for upd in updates:
                offset  = upd["update_id"] + 1
                msg     = upd.get("message",{})
                text    = msg.get("text","")
                chat_id = str(msg.get("chat",{}).get("id",""))
                if text.startswith("/athena"):
                    dispatch(text, chat_id)
        except Exception as e:
            log.error(f"Polling error: {e}")
            time.sleep(5)
        time.sleep(1)

if __name__ == "__main__":
    main()
