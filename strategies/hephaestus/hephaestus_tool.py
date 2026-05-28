#!/usr/bin/env python3
"""GENESIS — Hephaestus Tool + Telegram Bot (Strategy F: Grid+Martingale)
CLI for Hermes. Bot for manual monitoring and override.
"""
import sys, os, json, time, logging, threading
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))

from pathlib import Path
from datetime import datetime, timezone
import yaml, requests

CONFIG_PATH = Path(__file__).parent / "hephaestus_config.yaml"
if not CONFIG_PATH.exists():
    CONFIG_PATH = Path(__file__).parents[2] / "configs" / "hephaestus_config.yaml"
with open(CONFIG_PATH) as f:
    CFG = yaml.safe_load(f)

TG_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID = str(CFG["telegram"]["chat_id"])
STRATEGY   = CFG["strategy"]["name"]
COMMENT    = CFG["strategy"]["comment"]

# Resolve safe log path (fallback to local logs/ if system dir not writable)
default_log = "/var/log/hephaestus/hephaestus_bot.log"
try:
    Path(default_log).parent.mkdir(parents=True, exist_ok=True)
    log_file = default_log
except Exception:
    local_log_dir = Path(__file__).parents[2] / "logs" / "hephaestus"
    local_log_dir.mkdir(parents=True, exist_ok=True)
    log_file = str(local_log_dir / "hephaestus_bot.log")

logging.basicConfig(
    filename=log_file,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger(__name__)
_lock = threading.Lock()

def tg_send(text):
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "Markdown"}, timeout=10)
    except: pass

def tg_updates(offset=0):
    try:
        r = requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
            params={"timeout":30,"offset":offset}, timeout=40)
        return r.json().get("result",[])
    except: return []

def _load():
    import importlib.util
    spec = importlib.util.spec_from_file_location("hephaestus_cycle",
           Path(__file__).parent/"hephaestus_cycle.py")
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

# ── Commands ───────────────────────────────────────────────────────────────────
def do_run_tick():
    try: return _load().run_cycle()
    except Exception as e: return {"error":str(e)}

def do_status():
    try: return _load().get_status()
    except Exception as e: return {"error":str(e)}

def do_kill():
    try: return _load().emergency_kill()
    except Exception as e: return {"error":str(e)}

def do_enable():
    try: return _load().enable_strategy()
    except Exception as e: return {"error":str(e)}

def send_status_tg():
    r = do_status()
    if "error" in r:
        tg_send(f"🔴 *{STRATEGY}*: {r['error']}"); return
    enabled = r.get("enabled")
    acc     = r.get("account",{})
    j       = r.get("journal",{})
    cb      = r.get("circuit_breakers",{})
    killed  = r.get("killed_reason")
    tg_send(
        f"{'🔩' if enabled else '💀'} *{STRATEGY} — Grid Status*\n\n"
        f"Status: {'🟢 RUNNING' if enabled else f'🔴 DISABLED — {killed}'}\n"
        f"💰 Balance: €{acc.get('balance',0):.2f} | Equity: €{acc.get('equity',0):.2f}\n"
        f"📊 Open Positions: {r.get('open_positions',0)} | "
        f"Lots: {r.get('total_lots',0)} | PnL: €{r.get('unrealized_pnl',0):.2f}\n"
        f"📈 Buy Level: {r.get('buy_level',0)}/{cb.get('max_levels','?')} | "
        f"Sell Level: {r.get('sell_level',0)}/{cb.get('max_levels','?')}\n"
        f"🎯 Cycles: {r.get('total_cycles',0)} | "
        f"Journal: {j.get('wins',0)}W / {j.get('losses',0)}L\n\n"
        f"🛡 Circuit Breakers:\n"
        f"  Max DD: {cb.get('max_dd_pct','?')}% | Daily Loss: {cb.get('max_daily_loss','?')}%\n"
        f"  Max Levels: {cb.get('max_levels','?')} | Max Lots: {cb.get('max_lots','?')}"
    )

def dispatch(text, from_id):
    if str(from_id) != TG_CHAT_ID: return
    lower = text.lower().strip()

    def bg(fn, *args): threading.Thread(target=fn, args=args, daemon=True).start()

    if lower == "/heph_status":
        bg(send_status_tg)
    elif lower == "/heph_tick":
        # BLOCKED — only Hermes can run ticks via CLI
        tg_send(
            f"🔒 *{STRATEGY}*: `/heph_tick` is reserved for Hermes only.\n"
            f"Hermes runs grid ticks autonomously via CLI.\n"
            f"_You will see every action here automatically._"
        )
    elif lower == "/heph_kill":
        def run():
            tg_send(f"🚨 *{STRATEGY}*: EMERGENCY KILL initiated…")
            r = do_kill()
            tg_send(f"🚨 *{STRATEGY}*: {'All positions closed. DISABLED.' if r.get('killed') else 'Kill failed — check logs.'}")
        bg(run)
    elif lower == "/heph_enable":
        def run():
            r = do_enable()
            tg_send(f"✅ *{STRATEGY}*: {'RE-ENABLED. Hermes will resume grid ticks.' if r.get('enabled') else 'Enable failed.'}")
        bg(run)
    elif lower in ("/heph_help", "/heph"):
        tg_send(
            f"🔩 *{STRATEGY} — Strategy F (Monitor & Override)*\n\n"
            f"🤖 *Hermes runs this strategy autonomously.*\n"
            f"_You will receive automatic updates for every grid action._\n\n"
            f"`/heph_status` — Full grid status + circuit breaker state\n"
            f"`/heph_kill` — 🚨 EMERGENCY: close all positions + disable\n"
            f"`/heph_enable` — Re-enable after kill\n"
            f"`/heph_help` — This message\n\n"
            f"⚠️ Max {CFG['grid']['max_grid_levels']} levels | "
            f"{CFG['circuit_breakers']['max_equity_drawdown_pct']}% equity DD kill\n"
            f"🔖 Tag: `{COMMENT}` | Pair: `{CFG['symbol']}`"
        )

# ── CLI mode (Hermes calls this) ───────────────────────────────────────────────
def cli():
    args = sys.argv[1:]
    if not args:
        print(json.dumps({"error":"Usage: hephaestus_tool.py [tick|status|kill|enable|symbols]"}))
        sys.exit(1)
    cmd = args[0].lower()
    if   cmd=="tick":    print(json.dumps(do_run_tick(), indent=2, default=str))
    elif cmd=="status":  print(json.dumps(do_status(),   indent=2, default=str))
    elif cmd=="kill":    print(json.dumps(do_kill(),     indent=2, default=str))
    elif cmd=="enable":  print(json.dumps(do_enable(),   indent=2, default=str))
    elif cmd=="symbols": print(json.dumps({"symbol":CFG["symbol"],"comment":COMMENT,"strategy":"Grid+Martingale"},indent=2))
    else: print(json.dumps({"error":f"Unknown: {cmd}"}))

# ── Bot mode ───────────────────────────────────────────────────────────────────
def bot():
    log.info(f"=== {STRATEGY} Telegram Bot started ===")
    tg_send(
        f"🔩 *{STRATEGY} Bot Online*\n"
        f"Strategy F: Grid + Martingale on `{CFG['symbol']}`\n"
        f"Max Levels: {CFG['grid']['max_grid_levels']} | "
        f"DD Kill: {CFG['circuit_breakers']['max_equity_drawdown_pct']}%\n\n"
        f"🤖 *Hermes runs this strategy. You cannot trigger it.*\n"
        f"_You will see every grid action, level open, and TP here automatically._\n\n"
        f"✅ Your controls: `/heph_status` `/heph_kill` `/heph_enable`\n"
        f"⚠️ Use `/heph_kill` anytime to emergency stop all grid positions."
    )
    offset = 0
    while True:
        try:
            for upd in tg_updates(offset):
                offset  = upd["update_id"]+1
                msg     = upd.get("message",{})
                text    = msg.get("text","")
                chat_id = str(msg.get("chat",{}).get("id",""))
                if text.startswith("/heph"): dispatch(text, chat_id)
        except Exception as e:
            log.error(f"Poll error: {e}"); time.sleep(5)
        time.sleep(1)

if __name__ == "__main__":
    if Path(sys.argv[0]).name.startswith("hephaestus_telegram"):
        bot()
    else:
        cli()
