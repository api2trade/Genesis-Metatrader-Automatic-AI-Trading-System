#!/usr/bin/env python3
"""
GENESIS — Ares Telegram Bot (Strategy B Command Handler)
Listens for your manual commands. Ares NEVER trades on its own.

Commands:
  /ares_analyze [SYMBOL]  — Run full analysis, no trade placed
  /ares_execute           — Execute the last analysis signal (Account B only)
  /ares_skip              — Cancel the pending signal
  /ares_status            — Account B open position + journal stats
  /ares_help              — Show all commands
"""
import os, json, time, logging, requests, threading
from datetime import datetime, timezone
from pathlib import Path
import yaml

# ── Config ────────────────────────────────────────────────────────────────────
CONFIG_PATH = Path(__file__).parent / "ares_config.yaml"
with open(CONFIG_PATH) as f:
    CFG = yaml.safe_load(f)

TG_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID = str(CFG["telegram"]["chat_id"])
BRIDGE     = CFG["bridge"]["url"]
JOURNAL    = Path(CFG["journal"]["path"])
STRATEGY   = CFG["strategy"]["name"]

logging.basicConfig(
    filename="/var/log/ares/ares_bot.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger(__name__)

# ── Pending signal state (in-memory, one at a time) ───────────────────────────
_pending: dict = {}   # Holds last analysis result awaiting /ares_execute
_lock = threading.Lock()

# ── Telegram helpers ──────────────────────────────────────────────────────────
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
            params={"timeout": 30, "offset": offset},
            timeout=40
        )
        return r.json().get("result", [])
    except:
        return []

# ── Bridge helper (Account B) ─────────────────────────────────────────────────
def bridge(path, method="GET", data=None):
    try:
        url = f"{BRIDGE}{path}"
        r = (requests.post(url, json=data, timeout=15)
             if method == "POST" else requests.get(url, timeout=15))
        return r.json()
    except Exception as e:
        return {"error": str(e)}

# ── Journal helpers ───────────────────────────────────────────────────────────
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

# ── Command handlers ──────────────────────────────────────────────────────────
def cmd_help():
    tg_send(
        f"⚔️ *{STRATEGY} — Strategy B Commands*\n\n"
        f"`/ares_analyze [SYMBOL]` — Full analysis (no trade)\n"
        f"`/ares_execute` — Execute pending signal on Account B\n"
        f"`/ares_skip` — Cancel pending signal\n"
        f"`/ares_status` — Account B position + P&L\n"
        f"`/ares_help` — This message\n\n"
        f"⚠️ _Ares NEVER trades automatically. YOU must always confirm._"
    )

def cmd_status():
    acc = bridge("/balance")
    if "error" in acc:
        tg_send(f"🔴 *{STRATEGY}*: Account B bridge unreachable.\n`{acc['error']}`")
        return

    pos_data = bridge("/positions")
    pos_str = "None"
    if isinstance(pos_data, list) and pos_data:
        p = pos_data[0]
        pos_str = (f"{p.get('symbol')} {p.get('orderType')} "
                   f"{p.get('lots')}lot | P&L: €{p.get('profit', 0):.2f}")

    wins, losses = journal_stats()
    with _lock:
        pending_str = (f"🟡 Pending: {_pending.get('symbol')} {_pending.get('direction')}"
                       if _pending else "None")

    tg_send(
        f"⚔️ *{STRATEGY} — Account B Status*\n\n"
        f"💰 Balance: €{acc.get('balance', 0):.2f}\n"
        f"📊 Equity: €{acc.get('equity', 0):.2f}\n"
        f"📈 Open: {pos_str}\n"
        f"📋 Pending Signal: {pending_str}\n"
        f"📒 Journal: {wins}W / {losses}L"
    )

def cmd_skip():
    with _lock:
        if not _pending:
            tg_send(f"⚔️ *{STRATEGY}*: No pending signal to cancel.")
            return
        sym = _pending.get("symbol")
        _pending.clear()
    tg_send(f"⏭ *{STRATEGY}*: Signal for `{sym}` cancelled.")

def cmd_execute():
    with _lock:
        if not _pending:
            tg_send(
                f"⚔️ *{STRATEGY}*: No pending signal.\n"
                f"Run `/ares_analyze [SYMBOL]` first."
            )
            return
        signal = dict(_pending)
        _pending.clear()

    sym  = signal.get("symbol")
    dire = signal.get("direction")
    sl   = signal.get("stop_loss")
    tp   = signal.get("take_profit")
    vol  = signal.get("volume", 0.1)

    if not all([sym, dire, sl, tp]):
        tg_send(f"⚔️ *{STRATEGY}*: Pending signal is incomplete — cannot execute.")
        return

    # Check Account B still has no open positions
    positions = bridge("/positions")
    if isinstance(positions, list) and positions:
        tg_send(
            f"⚠️ *{STRATEGY}*: Account B already has an open position.\n"
            f"Close it first before executing a new trade."
        )
        return

    tg_send(f"⚔️ *{STRATEGY}*: Placing order on Account B…")
    order = bridge("/market", "POST", {
        "symbol": sym, "volume": vol, "type": dire,
        "stop_loss": sl, "take_profit": tp,
        "comment": CFG["strategy"]["comment"]
    })
    log.info(f"Execute order: {order}")

    ticket = order.get("ticket") or order.get("Ticket")
    if ticket:
        now = datetime.now(timezone.utc)
        journal_write({
            "ticket": str(ticket), "symbol": sym, "direction": dire,
            "volume": vol, "sl": sl, "tp": tp,
            "opened": now.isoformat(), "result": None, "pnl": None,
            "strategy": "ares"
        })
        tg_send(
            f"✅ *{STRATEGY} TRADE PLACED*\n"
            f"📈 `{sym}` {dire} | Vol: {vol}\n"
            f"SL: {sl} | TP: {tp}\n"
            f"🎯 Confidence: {signal.get('confidence', '?')}\n"
            f"💡 {signal.get('reason', '')[:200]}\n"
            f"🔖 Ticket: `{ticket}`"
        )
    else:
        err = order.get("message", str(order))
        tg_send(f"❌ *{STRATEGY}*: Order FAILED — `{err}`")

def cmd_analyze(symbol: str):
    """
    Trigger Ares analysis for a given symbol.
    Imports ares_cycle.py to run the analysis without placing any trade.
    Stores the result in _pending for /ares_execute to act on.
    """
    symbol = symbol.upper().strip()
    # Ensure symbol has broker suffix
    if not symbol.endswith("xx") and not symbol.endswith("XX"):
        symbol = symbol + "xx"

    tg_send(f"⚔️ *{STRATEGY}*: Analysing `{symbol}`… (this may take 30–60s)")

    try:
        # Import the analysis function from ares_cycle
        import importlib.util, sys
        spec = importlib.util.spec_from_file_location(
            "ares_cycle",
            Path(__file__).parent / "ares_cycle.py"
        )
        mod = importlib.util.load_from_spec(spec)
        spec.loader.exec_module(mod)

        result = mod.run_analysis(symbol)  # Returns signal dict or None
    except Exception as e:
        log.error(f"Analysis error: {e}", exc_info=True)
        tg_send(f"❌ *{STRATEGY}*: Analysis failed — `{str(e)[:200]}`")
        return

    if not result:
        tg_send(
            f"⚔️ *{STRATEGY}* — `{symbol}`\n\n"
            f"📊 Signal: *NO TRADE*\n"
            f"Conditions not met for a strict entry."
        )
        return

    with _lock:
        _pending.clear()
        _pending.update(result)

    action = result.get("action", "wait")
    if action != "trade":
        tg_send(
            f"⚔️ *{STRATEGY}* — `{symbol}`\n\n"
            f"📊 Signal: *WAIT*\n"
            f"💡 {result.get('reason', '')[:300]}"
        )
        return

    conditions = result.get("conditions_met", [])
    cond_str = "\n".join(f"  ✅ {c}" for c in conditions) if conditions else "  (see reason)"
    warnings  = result.get("warnings", [])
    warn_str  = ("\n" + "\n".join(f"  ⚠️ {w}" for w in warnings)) if warnings else ""

    rr = result.get("rr_ratio", "?")
    tg_send(
        f"⚔️ *{STRATEGY} ANALYSIS* — `{symbol}`\n\n"
        f"📊 Signal: *{result.get('direction')}*\n"
        f"Entry: `{result.get('entry')}`\n"
        f"SL:    `{result.get('stop_loss')}` ({result.get('sl_pips', '?')} pips)\n"
        f"TP:    `{result.get('take_profit')}` ({result.get('tp_pips', '?')} pips)\n"
        f"R:R    `{rr}`\n"
        f"Vol:   `{result.get('volume')} lot`\n"
        f"🎯 Confidence: {result.get('confidence', '?')}\n\n"
        f"📌 *Conditions met ({len(conditions)}/{CFG['strictness']['min_confluence_count']} required):*\n"
        f"{cond_str}{warn_str}\n\n"
        f"💡 {result.get('reason', '')[:300]}\n\n"
        f"Reply `/ares_execute` to place on Account B, or `/ares_skip` to cancel."
    )

# ── Dispatcher ────────────────────────────────────────────────────────────────
def dispatch(text: str, from_id: str):
    """Only accept commands from the authorised chat."""
    if str(from_id) != TG_CHAT_ID:
        log.warning(f"Ignored message from unauthorised ID: {from_id}")
        return

    text = text.strip()
    lower = text.lower()

    if lower.startswith("/ares_analyze"):
        parts = text.split(maxsplit=1)
        sym = parts[1] if len(parts) > 1 else ""
        if not sym:
            tg_send("Usage: `/ares_analyze EURUSD`")
        else:
            # Run in thread so bot stays responsive
            threading.Thread(target=cmd_analyze, args=(sym,), daemon=True).start()

    elif lower == "/ares_execute":
        threading.Thread(target=cmd_execute, daemon=True).start()

    elif lower == "/ares_skip":
        cmd_skip()

    elif lower == "/ares_status":
        threading.Thread(target=cmd_status, daemon=True).start()

    elif lower in ("/ares_help", "/ares"):
        cmd_help()

# ── Main polling loop ─────────────────────────────────────────────────────────
def main():
    log.info(f"=== {STRATEGY} Telegram Bot started ===")
    tg_send(
        f"⚔️ *{STRATEGY} Bot Online*\n"
        f"Strategy B: BB+RSI Mean Reversion (M1)\n"
        f"Send `/ares_help` to see commands.\n\n"
        f"🤖 _Hermes controls this bot autonomously._\n"
        f"_You can also trigger manually via the commands above._"
    )

    offset = 0
    while True:
        try:
            updates = tg_updates(offset)
            for upd in updates:
                offset = upd["update_id"] + 1
                msg = upd.get("message", {})
                text = msg.get("text", "")
                chat_id = str(msg.get("chat", {}).get("id", ""))
                if text.startswith("/ares"):
                    dispatch(text, chat_id)
        except Exception as e:
            log.error(f"Polling error: {e}")
            time.sleep(5)
        time.sleep(1)

if __name__ == "__main__":
    main()
