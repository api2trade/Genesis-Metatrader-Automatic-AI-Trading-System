#!/usr/bin/env python3
"""GENESIS — Artemis Tool + Telegram Bot (Strategy E: Ichimoku H1)
Combined into one file for efficiency. Hermes calls artemis_tool.py CLI.
Bot listens for /artemis_* commands.
"""
import sys, os, json, time, logging, threading
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))

from datetime import datetime, timezone
from pathlib import Path
import yaml, requests

CONFIG_PATH = Path(__file__).parent / "artemis_config.yaml"
if not CONFIG_PATH.exists():
    CONFIG_PATH = Path(__file__).parents[2] / "configs" / "artemis_config.yaml"
with open(CONFIG_PATH) as f:
    CFG = yaml.safe_load(f)

TG_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID = str(CFG["telegram"]["chat_id"])
BRIDGE_URL = CFG["bridge"]["url"]
# Resolve safe journal path (fallback to local logs/ if system dir not writable)
default_journal = CFG["journal"]["path"]
try:
    Path(default_journal).parent.mkdir(parents=True, exist_ok=True)
    JOURNAL = Path(default_journal)
except Exception:
    local_log_dir = Path(__file__).parents[2] / "logs" / "artemis"
    local_log_dir.mkdir(parents=True, exist_ok=True)
    JOURNAL = local_log_dir / "trade_journal.jsonl"
STRATEGY   = CFG["strategy"]["name"]
COMMENT    = CFG["strategy"]["comment"]
SYMBOLS    = CFG["symbols"]

logging.basicConfig(
    filename=f"/var/log/artemis/artemis_bot.log",
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger(__name__)
_pending: dict = {}
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

def bridge(path, method="GET", data=None):
    try:
        url = f"{BRIDGE_URL}{path}"
        r = requests.post(url,json=data,timeout=15) if method=="POST" else requests.get(url,timeout=15)
        return r.json()
    except Exception as e: return {"error": str(e)}

def journal_write(entry):
    
    with open(JOURNAL,"a") as f: f.write(json.dumps(entry)+"\n")

def journal_stats():
    w=l=0; pnl=0.0
    if JOURNAL.exists():
        for line in JOURNAL.read_text().strip().split("\n"):
            if not line: continue
            try:
                t=json.loads(line)
                if t.get("result")=="win": w+=1
                if t.get("result")=="loss": l+=1
                pnl+=float(t.get("pnl") or 0)
            except: pass
    return w,l,round(pnl,2)

def _load_cycle():
    import importlib.util
    spec = importlib.util.spec_from_file_location("artemis_cycle", Path(__file__).parent/"artemis_cycle.py")
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

def _analyze(sym):
    try: return _load_cycle().run_analysis(sym)
    except Exception as e: return {"action":"wait","reason":f"Error: {str(e)[:200]}"}

# ── Core commands ──────────────────────────────────────────────────────────────
def do_analyze(symbol):
    sym = symbol.upper(); sym = (sym+"xx") if not sym.endswith("XX") else sym
    return _analyze(sym)

def do_execute(symbol):
    sym = symbol.upper(); sym = (sym+"xx") if not sym.endswith("XX") else sym
    result = _analyze(sym)
    if result.get("action") != "trade":
        return {"executed":False,"reason":result.get("reason"),"conditions_met":result.get("conditions_met",[])}
    order = bridge("/market","POST",{
        "symbol":result["symbol"],"volume":result["volume"],"type":result["direction"],
        "stop_loss":result["stop_loss"],"take_profit":result["take_profit"],"comment":COMMENT
    })
    ticket = order.get("ticket") or order.get("Ticket")
    if ticket:
        journal_write({"ticket":str(ticket),"symbol":result["symbol"],"direction":result["direction"],
            "volume":result["volume"],"sl":result["stop_loss"],"tp":result["take_profit"],
            "entry":result["entry"],"rr":result["rr_ratio"],"signal_type":result.get("signal_type"),
            "opened":datetime.now(timezone.utc).isoformat(),"result":None,"pnl":None,
            "strategy":"artemis-ichimoku-h1","triggered_by":"hermes-autonomous"})
        ind=result.get("indicators",{})
        tg_send(f"🏹 *ARTEMIS TRADE — Hermes Triggered*\n"
            f"`{result['symbol']}` {result['direction']} | {result.get('signal_type','').replace('_',' ')}\n"
            f"Entry: `{result['entry']}` SL: `{result['stop_loss']}` TP: `{result['take_profit']}`\n"
            f"R:R: `{result['rr_ratio']}` | Vol: `{result['volume']}`\n"
            f"Cloud: {ind.get('cloud_color','?').upper()} | RSI: {ind.get('rsi','?')} | ADX: {ind.get('adx','?')}\n"
            f"Ticket: `{ticket}`")
        return {"executed":True,"ticket":str(ticket),"direction":result["direction"],
                "rr":result["rr_ratio"],"confidence":result.get("confidence")}
    else:
        err=order.get("message",str(order)); tg_send(f"⚠️ *ARTEMIS*: Order FAILED — `{err}`")
        return {"executed":False,"reason":f"Order failed: {err}"}

def do_scan():
    best=None; best_rr=0; results={}
    for sym in SYMBOLS:
        r=_analyze(sym); results[sym]={"action":r.get("action"),"rr":r.get("rr_ratio"),"reason":r.get("reason","")[:80]}
        if r.get("action")=="trade":
            rr=r.get("rr_ratio") or 0
            if rr > best_rr: best_rr=rr; best=r
        time.sleep(0.5)
    return {"best_signal":best,"scan_results":results,"signals_found":sum(1 for r in results.values() if r["action"]=="trade")}

def do_status():
    acc=bridge("/balance"); pos=bridge("/positions")
    artemis_pos=None; all_pos=[]
    if isinstance(pos,list):
        for p in pos:
            c=str(p.get("comment",""))
            all_pos.append({"ticket":p.get("ticket"),"symbol":p.get("symbol"),"type":p.get("orderType"),"profit":p.get("profit"),"comment":c})
            if "ARTEMIS" in c.upper(): artemis_pos=p
    w,l,pnl=journal_stats()
    return {"account":acc,"artemis_position":artemis_pos,"all_open":all_pos,
            "journal":{"wins":w,"losses":l,"pnl":pnl},"strategy":"Ichimoku Kumo Breakout H1"}

def do_close():
    pos=bridge("/positions"); closed=[]
    if isinstance(pos,list):
        for p in pos:
            if "ARTEMIS" in str(p.get("comment","")).upper():
                bridge("/close","POST",{"ticket":p["ticket"]}); closed.append(p["ticket"])
                tg_send(f"🎯 *ARTEMIS*: Position `{p['ticket']}` closed by Hermes.")
    return {"closed":len(closed),"tickets":closed} if closed else {"closed":0,"reason":"No open Artemis positions."}

# ── Telegram formatting ────────────────────────────────────────────────────────
def send_result(result, scan=False):
    if result.get("action")!="trade":
        tg_send(f"🎯 *{STRATEGY}* — `{result.get('symbol','?')}`\n\nSignal: *WAIT*\n💡 {result.get('reason','')[:300]}")
        return
    with _lock: _pending.clear(); _pending.update(result)
    ind  = result.get("indicators",{})
    cmet = result.get("conditions_met",[])
    tg_send(
        f"🎯 *{STRATEGY} SIGNAL*{'_(scan best)_' if scan else ''} — `{result.get('symbol')}`\n\n"
        f"Signal: *{result.get('direction')}* — {result.get('signal_type','').replace('_',' ')}\n"
        f"Entry: `{result.get('entry')}` SL: `{result.get('stop_loss')}` TP: `{result.get('take_profit')}`\n"
        f"R:R: `{result.get('rr_ratio')}` | Vol: `{result.get('volume')}`\n"
        f"Confidence: {result.get('confidence','?')}\n"
        f"Cloud: {ind.get('cloud_color','?').upper()} → {ind.get('future_cloud','?').upper()}\n"
        f"RSI: {ind.get('rsi','?')} | ADX: {ind.get('adx','?')} | ATR: {ind.get('atr','?')}\n\n"
        f"📌 *Conditions ({len(cmet)} met):*\n"
        + "\n".join(f"  ✅ {c}" for c in cmet[:5]) +
        f"\n\n`/artemis_execute` to trade | `/artemis_skip` to cancel"
    )

def send_status(r):
    acc=r.get("account",{}); ap=r.get("artemis_position"); j=r.get("journal",{})
    all_s="\n".join(f"`{p['symbol']}` {p['type']} €{p.get('profit',0):.2f} [{p['comment']}]" for p in r.get("all_open",[]))
    tg_send(
        f"🎯 *{STRATEGY} — Status*\n\n"
        f"💰 Balance: €{acc.get('balance',0):.2f} | Equity: €{acc.get('equity',0):.2f}\n"
        f"📈 Artemis Position: {ap.get('symbol','None') if ap else 'None'}\n"
        f"📒 Journal: {j.get('wins',0)}W / {j.get('losses',0)}L | PnL: €{j.get('pnl',0)}\n\n"
        f"*All Open:*\n{all_s or 'None'}"
    )

# ── Telegram dispatcher ────────────────────────────────────────────────────────
def dispatch(text, from_id):
    if str(from_id)!=TG_CHAT_ID: return
    lower=text.lower().strip()

    def bg(fn, *args): threading.Thread(target=fn,args=args,daemon=True).start()

    if lower.startswith("/artemis_analyze"):
        parts=text.split(maxsplit=1)
        sym=parts[1] if len(parts)>1 else ""
        if not sym: tg_send("Usage: `/artemis_analyze EURUSD`"); return
        def run():
            tg_send(f"🎯 *{STRATEGY}*: Analysing `{sym.upper()}` on H1… (30–60s)")
            send_result(do_analyze(sym))
        bg(run)
    elif lower=="/artemis_scan":
        def run():
            tg_send(f"🎯 *{STRATEGY}*: Scanning {len(SYMBOLS)} symbols on H1…")
            r=do_scan()
            lines=[f"{'🟢' if v['action']=='trade' else '⚪'} `{s}`: {v['reason'][:60]}"
                   for s,v in r["scan_results"].items()]
            tg_send("🎯 *ARTEMIS SCAN*\n\n"+"\n".join(lines))
            if r["best_signal"]: send_result(r["best_signal"],scan=True)
            else: tg_send(f"📊 No signals found across {len(SYMBOLS)} symbols.")
        bg(run)
    elif lower=="/artemis_execute":
        def run():
            with _lock:
                if not _pending:
                    tg_send(f"🎯 No pending signal. Run `/artemis_scan` or `/artemis_analyze SYMBOL` first."); return
                sig=dict(_pending); _pending.clear()
            tg_send(f"🎯 Placing Artemis order…")
            r=do_execute(sig.get("symbol","").replace("xx",""))
            if not r.get("executed"): tg_send(f"❌ Failed: {r.get('reason')}")
        bg(run)
    elif lower=="/artemis_skip":
        with _lock:
            if not _pending: tg_send(f"🎯 No pending signal."); return
            sym=_pending.get("symbol"); _pending.clear()
        tg_send(f"⏭ *{STRATEGY}*: Signal for `{sym}` cancelled.")
    elif lower=="/artemis_status":
        bg(lambda: send_status(do_status()))
    elif lower in ("/artemis_help","/artemis"):
        tg_send(
            f"🎯 *{STRATEGY} — Strategy E Commands*\n\n"
            f"`/artemis_analyze [SYMBOL]` — Ichimoku H1 analysis\n"
            f"`/artemis_scan` — Scan all {len(SYMBOLS)} symbols\n"
            f"`/artemis_execute` — Execute pending signal\n"
            f"`/artemis_skip` — Cancel pending signal\n"
            f"`/artemis_status` — Position + journal\n"
            f"`/artemis_help` — This message\n\n"
            f"📊 Strategy: Ichimoku Kumo Breakout | H1\n"
            f"🔖 Tag: `{COMMENT}` | Risk: 0.75%"
        )

# ── CLI mode (called by Hermes via ares_tool.py pattern) ──────────────────────
def cli():
    args=sys.argv[1:]
    if not args: print(json.dumps({"error":"Usage: artemis_tool.py [analyze|execute|scan|status|close|symbols] [SYMBOL]"})); sys.exit(1)
    cmd=args[0].lower()
    if   cmd=="analyze": print(json.dumps(do_analyze(args[1] if len(args)>1 else "EURUSDxx"),indent=2,default=str))
    elif cmd=="execute": print(json.dumps(do_execute(args[1] if len(args)>1 else "EURUSDxx"),indent=2,default=str))
    elif cmd=="scan":    print(json.dumps(do_scan(),indent=2,default=str))
    elif cmd=="status":  print(json.dumps(do_status(),indent=2,default=str))
    elif cmd=="close":   print(json.dumps(do_close(),indent=2,default=str))
    elif cmd=="symbols": print(json.dumps({"symbols":SYMBOLS,"strategy":"Ichimoku Kumo Breakout H1","comment":COMMENT},indent=2))
    else: print(json.dumps({"error":f"Unknown: {cmd}"}))

# ── Bot mode ───────────────────────────────────────────────────────────────────
def bot():
    log.info(f"=== {STRATEGY} Telegram Bot started ===")
    tg_send(
        f"🎯 *{STRATEGY} Bot Online*\n"
        f"Strategy E: Ichimoku Kumo Breakout (H1)\n"
        f"Tenkan(9) / Kijun(26) / Senkou B(52)\n"
        f"Send `/artemis_help` to see commands.\n\n"
        f"🤖 _Hermes controls this bot autonomously._\n"
        f"_Manual override available via commands above._"
    )
    offset=0
    while True:
        try:
            for upd in tg_updates(offset):
                offset=upd["update_id"]+1
                msg=upd.get("message",{}); text=msg.get("text","")
                chat_id=str(msg.get("chat",{}).get("id",""))
                if text.startswith("/artemis"): dispatch(text,chat_id)
        except Exception as e: log.error(f"Poll error: {e}"); time.sleep(5)
        time.sleep(1)

if __name__=="__main__":
    # If called as artemis_tool.py → CLI mode
    # If called as artemis_telegram_bot.py → bot mode
    if Path(sys.argv[0]).name.startswith("artemis_telegram"):
        bot()
    else:
        cli()
