#!/usr/bin/env python3
"""GENESIS — Zeus Tool + Telegram Bot (Strategy G: ICT Smart Money)
CLI for Hermes autonomous use. Bot for monitoring — Hermes runs, you watch.
"""
import sys, os, json, time, logging, threading
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
from pathlib import Path
from datetime import datetime, timezone
import yaml, requests

CONFIG_PATH = Path(__file__).parent / "zeus_config.yaml"
if not CONFIG_PATH.exists():
    CONFIG_PATH = Path(__file__).parents[2] / "configs" / "zeus_config.yaml"
with open(CONFIG_PATH) as f: CFG = yaml.safe_load(f)

TG_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID = str(CFG["telegram"]["chat_id"])
STRATEGY   = CFG["strategy"]["name"]
COMMENT    = CFG["strategy"]["comment"]
SYMBOLS    = CFG["symbols"]
BRIDGE_URL = CFG["bridge"]["url"]
# Resolve safe journal path (fallback to local logs/ if system dir not writable)
default_journal = CFG["journal"]["path"]
try:
    Path(default_journal).parent.mkdir(parents=True, exist_ok=True)
    JOURNAL = Path(default_journal)
except Exception:
    local_log_dir = Path(__file__).parents[2] / "logs" / "zeus"
    local_log_dir.mkdir(parents=True, exist_ok=True)
    JOURNAL = local_log_dir / "trade_journal.jsonl"

# Resolve safe log path (fallback to local logs/ if system dir not writable)
default_log = "/var/log/zeus/zeus_bot.log"
try:
    Path(default_log).parent.mkdir(parents=True, exist_ok=True)
    log_file = default_log
except Exception:
    local_log_dir = Path(__file__).parents[2] / "logs" / "zeus"
    local_log_dir.mkdir(parents=True, exist_ok=True)
    log_file = str(local_log_dir / "zeus_bot.log")

logging.basicConfig(
    filename=log_file,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger(__name__)
_pending: dict = {}
_lock = threading.Lock()

def tg_send(text):
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id":TG_CHAT_ID,"text":text,"parse_mode":"Markdown"},timeout=10)
    except: pass

def tg_updates(offset=0):
    try:
        r=requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
            params={"timeout":30,"offset":offset},timeout=40)
        return r.json().get("result",[])
    except: return []

def bridge(path,method="GET",data=None):
    try:
        url=f"{BRIDGE_URL}{path}"
        r=requests.post(url,json=data,timeout=15) if method=="POST" else requests.get(url,timeout=15)
        return r.json()
    except Exception as e: return {"error":str(e)}

def _load():
    import importlib.util
    spec=importlib.util.spec_from_file_location("zeus_cycle",Path(__file__).parent/"zeus_cycle.py")
    mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod

def do_analyze(sym):
    try: return _load().run_analysis(sym)
    except Exception as e: return {"action":"wait","reason":f"Error: {str(e)[:200]}"}

def do_execute(sym):
    s=sym.upper(); s=(s+"xx") if not s.endswith("XX") else s
    result=do_analyze(s)
    if result.get("action")!="trade":
        return {"executed":False,"reason":result.get("reason"),"layer":result.get("layer"),
                "score":result.get("score")}
    order=bridge("/market","POST",{"symbol":result["symbol"],"volume":result["volume"],
        "type":result["direction"],"stop_loss":result["stop_loss"],
        "take_profit":result["take_profit"],"comment":COMMENT})
    ticket=order.get("ticket") or order.get("Ticket")
    if ticket:
        
        with open(JOURNAL,"a") as f:
            f.write(json.dumps({"ticket":str(ticket),"symbol":result["symbol"],
                "direction":result["direction"],"volume":result["volume"],
                "sl":result["stop_loss"],"tp":result["take_profit"],
                "entry":result["entry"],"rr":result["rr_ratio"],
                "score":result["confidence_score"],"signal_type":result.get("signal_type"),
                "killzone":result.get("killzone"),"opened":datetime.now(timezone.utc).isoformat(),
                "result":None,"pnl":None,"strategy":"zeus-ict-smartmoney",
                "triggered_by":"hermes-autonomous"})+"\n")
        layers = result.get("layers",{})
        sweep  = layers.get("1_sweep",{}); fvg = layers.get("2_fvg",{}); ob = layers.get("3_ob",{})
        tg_send(
            f"⚡ *ZEUS TRADE — Hermes Triggered*\n"
            f"`{result['symbol']}` {result['direction']} | {result.get('signal_type','').replace('_',' ')}\n"
            f"Entry: `{result['entry']}` SL: `{result['stop_loss']}` TP: `{result['take_profit']}`\n"
            f"R:R: `{result['rr_ratio']}` | Vol: `{result['volume']}`\n"
            f"🎯 Score: `{result['confidence_score']}/100` | {result.get('killzone','no KZ')}\n"
            f"Layer 1: {sweep.get('type','?')} sweep @ {sweep.get('level','?')}\n"
            f"Layer 2: {fvg.get('type','?')} FVG {fvg.get('gap_pips','?')}pips\n"
            f"Layer 3: OB quality {ob.get('quality','?')}/20\n"
            f"🔖 Ticket: `{ticket}`"
        )
        return {"executed":True,"ticket":str(ticket),"score":result["confidence_score"],
                "direction":result["direction"],"rr":result["rr_ratio"]}
    else:
        err=order.get("message",str(order))
        tg_send(f"⚠️ *ZEUS*: Order FAILED — `{err}`")
        return {"executed":False,"reason":f"Order failed: {err}"}

def do_scan():
    best=None; best_sc=0; results={}
    for sym in SYMBOLS:
        r=do_analyze(sym)
        results[sym]={"action":r.get("action"),"score":r.get("confidence_score",0),
                      "layer":r.get("layer",""),"reason":r.get("reason","")[:80]}
        if r.get("action")=="trade":
            sc=r.get("confidence_score",0)
            if sc>best_sc: best_sc=sc; best=r
        time.sleep(0.5)
    return {"best_signal":best,"scan_results":results,
            "signals_found":sum(1 for r in results.values() if r["action"]=="trade")}

def do_status():
    acc=bridge("/balance"); pos=bridge("/positions")
    zeus_pos=None; all_pos=[]
    if isinstance(pos,list):
        for p in pos:
            c=str(p.get("comment",""))
            all_pos.append({"ticket":p.get("ticket"),"symbol":p.get("symbol"),
                "type":p.get("orderType"),"profit":p.get("profit"),"comment":c})
            if "ZEUS" in c.upper(): zeus_pos=p
    w=l=0
    if JOURNAL.exists():
        for line in JOURNAL.read_text().strip().split("\n"):
            if not line: continue
            try:
                t=json.loads(line)
                if t.get("result")=="win": w+=1
                if t.get("result")=="loss": l+=1
            except: pass
    return {"account":acc,"zeus_position":zeus_pos,"all_open":all_pos,
            "journal":{"wins":w,"losses":l},"strategy":"ICT Smart Money M5"}

def do_close():
    pos=bridge("/positions"); closed=[]
    if isinstance(pos,list):
        for p in pos:
            if "ZEUS" in str(p.get("comment","")).upper():
                bridge("/close","POST",{"ticket":p["ticket"]}); closed.append(p["ticket"])
                tg_send(f"⚡ *ZEUS*: Position `{p['ticket']}` closed by Hermes.")
    return {"closed":len(closed),"tickets":closed} if closed else {"closed":0,"reason":"No Zeus positions."}

def send_result(result, scan=False):
    if result.get("action")!="trade":
        layer = result.get("layer","?")
        tg_send(f"⚡ *{STRATEGY}* — `{result.get('symbol','?')}`\n\nSignal: *WAIT*\n"
                f"Progress: {layer}\n💡 {result.get('reason','')[:300]}")
        return
    with _lock: _pending.clear(); _pending.update(result)
    layers = result.get("layers",{}); sc=result.get("score_breakdown",{})
    sweep=layers.get("1_sweep",{}); fvg=layers.get("2_fvg",{}); ob=layers.get("3_ob",{})
    kz_name=result.get("killzone","none")
    tg_send(
        f"⚡ *{STRATEGY} SIGNAL*{'_(scan best)_' if scan else ''} — `{result.get('symbol')}`\n\n"
        f"Signal: *{result.get('direction')}* | {result.get('signal_type','').replace('_',' ')}\n"
        f"Entry: `{result.get('entry')}` SL: `{result.get('stop_loss')}` TP: `{result.get('take_profit')}`\n"
        f"R:R: `{result.get('rr_ratio')}` | Vol: `{result.get('volume')}`\n"
        f"🎯 *Score: {result.get('confidence_score',0)}/100* | {'🕐 '+kz_name if kz_name!='none' else 'No KZ'}\n\n"
        f"✅ *Three-Layer Confirmation:*\n"
        f"  Layer 1 Sweep: {sweep.get('type','?').upper()} @ {sweep.get('level','?')} ({sweep.get('rejection','?')})\n"
        f"  Layer 2 FVG: {fvg.get('type','?').upper()} {fvg.get('gap_pips','?')}pips str={fvg.get('strength','?')}\n"
        f"  Layer 3 OB:  Quality {ob.get('quality','?')}/20 | Age {ob.get('age_bars','?')}bars\n\n"
        f"📊 *Score Breakdown:*\n"
        f"  BOS:{sc.get('bos_strength',0)} Sweep:{sc.get('sweep_quality',0)} "
        f"FVG:{sc.get('fvg_presence',0)} OB:{sc.get('ob_quality',0)}\n"
        f"  KZ:{sc.get('killzone',0)} MTF:{sc.get('mtf_confluence',0)} Fresh:{sc.get('ob_freshness',0)}\n\n"
        f"`/zeus_execute` to trade | `/zeus_skip` to cancel"
    )

def dispatch(text, from_id):
    if str(from_id)!=TG_CHAT_ID: return
    lower=text.lower().strip()
    def bg(fn,*args): threading.Thread(target=fn,args=args,daemon=True).start()

    if lower.startswith("/zeus_analyze"):
        parts=text.split(maxsplit=1); sym=parts[1] if len(parts)>1 else ""
        if not sym: tg_send("Usage: `/zeus_analyze EURUSD`"); return
        def run():
            tg_send(f"⚡ *{STRATEGY}*: Running 3-layer ICT analysis on `{sym.upper()}`… (30–60s)")
            send_result(do_analyze(sym))
        bg(run)
    elif lower=="/zeus_scan":
        def run():
            tg_send(f"⚡ *{STRATEGY}*: Scanning {len(SYMBOLS)} symbols (60–90s)…")
            r=do_scan()
            lines=[]
            for s,v in r["scan_results"].items():
                if v["action"]=="trade": lines.append(f"⚡ `{s}`: Score {v['score']}/100")
                else: lines.append(f"⚪ `{s}`: {v.get('layer','')} — {v['reason'][:50]}")
            tg_send("⚡ *ZEUS SCAN*\n\n"+"\n".join(lines))
            if r["best_signal"]: send_result(r["best_signal"],scan=True)
            else: tg_send(f"📊 No ICT setups found. Remember: 5–15 signals/month is normal.")
        bg(run)
    elif lower=="/zeus_execute":
        def run():
            with _lock:
                if not _pending:
                    tg_send(f"⚡ No pending. Run `/zeus_scan` or `/zeus_analyze SYMBOL` first."); return
                sig=dict(_pending); _pending.clear()
            tg_send("⚡ Placing Zeus order…")
            r=do_execute(sig.get("symbol","").replace("xx",""))
            if not r.get("executed"): tg_send(f"❌ Failed: {r.get('reason')}")
        bg(run)
    elif lower=="/zeus_skip":
        with _lock:
            if not _pending: tg_send("⚡ No pending signal."); return
            sym=_pending.get("symbol"); _pending.clear()
        tg_send(f"⏭ *{STRATEGY}*: Signal for `{sym}` cancelled.")
    elif lower=="/zeus_status":
        def run():
            r=do_status(); acc=r.get("account",{}); zp=r.get("zeus_position"); j=r.get("journal",{})
            all_s="\n".join(f"`{p['symbol']}` {p['type']} €{p.get('profit',0):.2f} [{p['comment']}]"
                            for p in r.get("all_open",[]))
            tg_send(f"⚡ *{STRATEGY} — Status*\n\n"
                f"💰 Balance: €{acc.get('balance',0):.2f} | Equity: €{acc.get('equity',0):.2f}\n"
                f"📈 Zeus Position: {zp.get('symbol','None') if zp else 'None'}\n"
                f"📒 Journal: {j.get('wins',0)}W / {j.get('losses',0)}L\n\n"
                f"*All Open:*\n{all_s or 'None'}")
        bg(run)
    elif lower in ("/zeus_help","/zeus"):
        tg_send(
            f"⚡ *{STRATEGY} — Strategy G Commands*\n\n"
            f"🤖 _Hermes runs Zeus autonomously._\n\n"
            f"`/zeus_analyze [SYMBOL]` — 3-layer ICT analysis\n"
            f"`/zeus_scan` — Scan all {len(SYMBOLS)} symbols\n"
            f"`/zeus_execute` — Execute pending signal\n"
            f"`/zeus_skip` — Cancel pending\n"
            f"`/zeus_status` — Position + journal\n"
            f"`/zeus_help` — This message\n\n"
            f"📊 Three-Layer: Sweep → FVG → Order Block\n"
            f"🕐 Killzones: London 08–11 | NY 13–16 GMT\n"
            f"🔖 Tag: `{COMMENT}` | Risk: 0.75%\n"
            f"⏱ Expected: 5–15 signals/month"
        )

def cli():
    args=sys.argv[1:]
    if not args: print(json.dumps({"error":"Usage: zeus_tool.py [analyze|execute|scan|status|close|symbols] [SYMBOL]"})); sys.exit(1)
    cmd=args[0].lower()
    sym=args[1] if len(args)>1 else "EURUSDxx"
    if   cmd=="analyze": print(json.dumps(do_analyze(sym),indent=2,default=str))
    elif cmd=="execute": print(json.dumps(do_execute(sym),indent=2,default=str))
    elif cmd=="scan":    print(json.dumps(do_scan(),indent=2,default=str))
    elif cmd=="status":  print(json.dumps(do_status(),indent=2,default=str))
    elif cmd=="close":   print(json.dumps(do_close(),indent=2,default=str))
    elif cmd=="symbols": print(json.dumps({"symbols":SYMBOLS,"strategy":"ICT Smart Money","comment":COMMENT},indent=2))
    else: print(json.dumps({"error":f"Unknown: {cmd}"}))

def bot():
    log.info(f"=== {STRATEGY} Telegram Bot started ===")
    tg_send(
        f"⚡ *{STRATEGY} Bot Online*\n"
        f"Strategy G: ICT Smart Money (Sweep → FVG → Order Block)\n"
        f"Entry: M5 | Context: M15 | Killzones: London + NY\n"
        f"Min Score: {CFG['confluence']['min_score']}/100 | Risk: 0.75%\n\n"
        f"🤖 _Hermes controls this bot autonomously._\n"
        f"_Expected: 5–15 signals/month. Quality over quantity._\n"
        f"Send `/zeus_help` for commands."
    )
    offset=0
    while True:
        try:
            for upd in tg_updates(offset):
                offset=upd["update_id"]+1
                msg=upd.get("message",{}); text=msg.get("text","")
                chat_id=str(msg.get("chat",{}).get("id",""))
                if text.startswith("/zeus"): dispatch(text,chat_id)
        except Exception as e: log.error(f"Poll: {e}"); time.sleep(5)
        time.sleep(1)

if __name__=="__main__":
    if Path(sys.argv[0]).name.startswith("zeus_telegram"):
        bot()
    else:
        cli()
