#!/usr/bin/env python3
"""
GENESIS Brain Feed — Hourly Telegram Broadcast
Every 60 minutes: scans all strategies in parallel, reports open trades,
P&L, what each strategy is seeing, and the outlook for the next hour.
"""
import os, json, time, logging, threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError
import subprocess, requests, yaml

# ── Config ─────────────────────────────────────────────────────────────────────
TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID", "")
AGENT    = "/opt/hermes-agent"
PYTHON   = f"{AGENT}/.venv-hermes/bin/python3"
SCAN_TIMEOUT = 55  # seconds per strategy scan

JOURNALS = {
    "GENESIS-v2": "/var/log/hermes/trade_journal.jsonl",
    "ARES-v1":    "/var/log/ares/trade_journal.jsonl",
    "APOLLO-v1":  "/var/log/apollo/trade_journal.jsonl",
    "ATHENA-v1":  "/var/log/athena/trade_journal.jsonl",
    "ARTEMIS-v1": "/var/log/artemis/trade_journal.jsonl",
    "HEPH-v1":    "/var/log/hephaestus/trade_journal.jsonl",
    "ZEUS-v1":    "/var/log/zeus/trade_journal.jsonl",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

def tg(msg: str):
    """Send message, splitting if >4000 chars."""
    for chunk in [msg[i:i+4000] for i in range(0, len(msg), 4000)]:
        try:
            requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                json={"chat_id": CHAT_ID, "text": chunk, "parse_mode": "Markdown"},
                timeout=10)
            time.sleep(0.3)
        except Exception as e:
            log.error(f"tg send error: {e}")

def bridge(path) -> dict:
    try:
        r = requests.get(f"{BRIDGE}{path}", timeout=10)
        return r.json()
    except: return {}

def run_tool(name: str, tool_file: str, cmd: str, symbol: str = "") -> dict:
    """Run a strategy tool and return parsed JSON result with timeout."""
    args = [PYTHON, f"{AGENT}/{tool_file}", cmd]
    if symbol: args.append(symbol)
    try:
        result = subprocess.run(args, capture_output=True, text=True,
                                timeout=SCAN_TIMEOUT, cwd=AGENT)
        if result.stdout.strip():
            return json.loads(result.stdout.strip())
        return {"error": result.stderr.strip()[:100] or "No output"}
    except subprocess.TimeoutExpired:
        return {"error": "timeout"}
    except Exception as e:
        return {"error": str(e)[:100]}

def journal_summary(path: str, hours: int = 1) -> dict:
    """Summarize trades in the last N hours from a journal file."""
    p = Path(path)
    if not p.exists(): return {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0, "open": 0}
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    trades = wins = losses = open_t = 0
    pnl = 0.0
    try:
        for line in p.read_text().strip().split("\n"):
            if not line: continue
            try:
                t = json.loads(line)
                opened = t.get("opened","")
                if opened:
                    try:
                        dt = datetime.fromisoformat(opened.replace("Z","+00:00"))
                        if dt < cutoff: continue
                    except: continue
                trades += 1
                result = t.get("result")
                p_val  = float(t.get("pnl") or 0)
                if result == "win":   wins += 1;     pnl += p_val
                elif result == "loss": losses += 1;  pnl += p_val
                elif result is None:  open_t += 1
            except: continue
    except: pass
    return {"trades": trades, "wins": wins, "losses": losses, "pnl": round(pnl,2), "open": open_t}

def icon(action):
    return "🟢" if action == "trade" else "⚪"

def fmt_strategy_result(name: str, result: dict) -> str:
    action = result.get("action","wait")
    if action == "trade":
        return (f"{icon(action)} *{name}*: SIGNAL {result.get('direction','?')} "
                f"`{result.get('symbol','?')}` "
                f"R:R {result.get('rr_ratio','?')} | "
                f"Vol {result.get('volume','?')} | "
                f"Score {result.get('confidence_score',result.get('confidence','?'))}")
    reason = result.get("reason","")[:80]
    layer  = result.get("layer","")
    prog   = f" [{layer}]" if layer else ""
    return f"{icon(action)} *{name}*: WAIT{prog} — {reason}"

def fmt_heph_status(result: dict) -> str:
    if "error" in result:
        return f"⚙️ *HEPHAESTUS*: {result['error'][:80]}"
    enabled = result.get("enabled", False)
    bl = result.get("buy_level",0); sl = result.get("sell_level",0)
    lots = result.get("total_lots",0); pnl = result.get("unrealized_pnl",0)
    status = "RUNNING" if enabled else f"DISABLED — {result.get('killed_reason','?')}"
    return (f"⚙️ *HEPHAESTUS*: {status} | "
            f"Buy L{bl} Sell L{sl} | {lots}lots | PnL €{pnl:.2f}")

def estimate_next_hour(scan_results: dict) -> list[str]:
    """Generate outlook text based on current scan results + session timing."""
    now_utc  = datetime.now(timezone.utc)
    hr       = now_utc.hour + now_utc.minute/60
    weekday  = now_utc.weekday()  # 0=Mon 4=Fri 5=Sat 6=Sun

    signals  = [(n,r) for n,r in scan_results.items() if r.get("action")=="trade"]
    waits    = [(n,r) for n,r in scan_results.items() if r.get("action")!="trade"]
    lines    = []

    # ── Session context ────────────────────────────────────────────
    is_weekend = weekday >= 5
    in_asia    = 0  <= hr <  5
    in_london  = 7  <= hr < 11
    in_ny      = 12 <= hr < 17
    in_session = not is_weekend and (5 <= hr < 21)

    if is_weekend:
        opens_in = (6 - weekday) * 24 - hr + 22  # hours until Mon 22:00 UTC
        lines.append(f"🌙 *Weekend* — markets closed.")
        lines.append(f"  Forex opens ~{round(opens_in,1)}h from now (Mon 22:00 UTC)")
        lines.append(f"  Hermes resumes scanning at session open.")
    elif in_london:
        lines.append("🏦 *London session active* (07:00–11:00 UTC)")
        lines.append("  Zeus + Apollo most active here. Expect 1–3 scan cycles.")
    elif in_ny:
        lines.append("🗽 *New York session active* (12:00–17:00 UTC)")
        lines.append("  Zeus + Apollo most active. Ares/Athena also scanning.")
    elif in_asia:
        lines.append("🌏 *Asian session* (00:00–05:00 UTC) — lower volatility")
        lines.append("  Ares + Athena may fire on GBPJPY/XAUUSD ranging moves.")
    elif not in_session:
        next_open = 5 - hr if hr < 5 else 29 - hr  # next 05:00 UTC
        lines.append(f"🌙 *Off-hours* — strategies paused.")
        lines.append(f"  Next session opens in ~{abs(round(next_open,1))}h (05:00 UTC)")
        lines.append(f"  London killzone in ~{round(max(0,7-hr),1)}h — Zeus high-probability window")

    lines.append("")

    # ── Live signals ───────────────────────────────────────────────
    if signals:
        lines.append(f"🔥 *{len(signals)} live signal(s) ready:*")
        for name, r in signals:
            lines.append(f"  → {name}: {r.get('direction')} `{r.get('symbol')}` "
                         f"— Hermes will evaluate for execution")

    # ── Almost-ready signals ───────────────────────────────────────
    for name, r in waits:
        reason = r.get("reason","")
        layer  = r.get("layer","")
        score  = r.get("score", r.get("confidence_score", 0)) or 0
        if "cooldown" in reason.lower():
            lines.append(f"⏱ {name}: cooldown — resets shortly")
        elif "2/3" in layer or "3/3" in layer:
            lines.append(f"🔶 {name}: *{layer}* — one more confirmation needed")
        elif isinstance(score, (int, float)) and score > 50:
            lines.append(f"🔶 {name}: score {score}/100 — approaching threshold (65)")

    if not signals and not any(
        "cooldown" in r.get("reason","").lower() or
        r.get("score",0) > 50
        for _, r in waits
    ):
        if in_session:
            lines.append("📊 All strategies in WAIT — market likely in low-conviction state")
            lines.append("  Hermes scanning every 5min cycle. Will fire when conditions align.")
        elif not is_weekend:
            lines.append("📊 Strategies dormant during off-hours — normal behaviour")

    return lines

def build_report(acc, positions, scan_results, heph_result, journal_summaries) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    balance = float(acc.get("balance",0))
    equity  = float(acc.get("equity",0))
    profit  = float(acc.get("profit",0))

    lines = [
        f"🧠 *GENESIS — Hourly Brain Feed*",
        f"📅 {now}",
        f"",
        f"━━━━━ 💰 ACCOUNT ━━━━━",
        f"Balance: `€{balance:,.2f}` | Equity: `€{equity:,.2f}`",
        f"Floating P&L: `€{profit:+.2f}`",
        f"",
    ]

    # ── Open Positions ─────────────────────────────────────────────
    lines.append("━━━━━ 📈 OPEN POSITIONS ━━━━━")
    if isinstance(positions, list) and positions:
        for p in positions:
            pnl_val = float(p.get("profit",0))
            pnl_icon = "🟢" if pnl_val >= 0 else "🔴"
            lines.append(
                f"{pnl_icon} `{p.get('symbol')}` {p.get('orderType')} "
                f"{p.get('lots')}lot | P&L: `€{pnl_val:+.2f}` | [{p.get('comment')}]"
            )
    else:
        lines.append("  No open positions")

    # ── Journal summary (last hour) ────────────────────────────────
    lines.append(f"")
    lines.append("━━━━━ 📒 LAST HOUR TRADES ━━━━━")
    total_trades = total_wins = total_losses = 0
    total_pnl = 0.0
    any_activity = False
    for strat, summ in journal_summaries.items():
        if summ["trades"] > 0:
            any_activity = True
            total_trades += summ["trades"]
            total_wins   += summ["wins"]
            total_losses += summ["losses"]
            total_pnl    += summ["pnl"]
            lines.append(
                f"  `{strat}`: {summ['trades']} trade(s) | "
                f"{summ['wins']}W {summ['losses']}L | "
                f"P&L: `€{summ['pnl']:+.2f}`"
            )
    if not any_activity:
        lines.append("  No completed trades in the last hour")
    else:
        lines.append(f"  *Total:* {total_trades} trades | "
                     f"{total_wins}W {total_losses}L | `€{total_pnl:+.2f}`")

    # ── Strategy Scans ─────────────────────────────────────────────
    lines.append(f"")
    lines.append("━━━━━ 🔬 STRATEGY SCANS ━━━━━")
    for name, result in scan_results.items():
        lines.append(fmt_strategy_result(name, result))
    lines.append(fmt_heph_status(heph_result))

    # ── Next Hour Outlook ──────────────────────────────────────────
    lines.append(f"")
    lines.append("━━━━━ 🔭 NEXT HOUR OUTLOOK ━━━━━")
    for l in estimate_next_hour(scan_results):
        lines.append(l)

    lines.append(f"")
    lines.append("_Next broadcast in ~60 min_")

    return "\n".join(lines)

def run_broadcast():
    log.info("=== Brain Feed broadcast starting ===")
    start = time.time()

    # ── Parallel strategy scans ────────────────────────────────────
    scan_tasks = {
        "ARES":    ("ares_tool.py",        "analyze", "EURUSDxx"),
        "APOLLO":  ("apollo_tool.py",      "analyze", "EURUSDxx"),
        "ATHENA":  ("athena_tool.py",      "analyze", "EURUSDxx"),
        "ARTEMIS": ("artemis_tool.py",     "analyze", "EURUSDxx"),
        "ZEUS":    ("zeus_tool.py",        "analyze", "EURUSDxx"),
    }
    scan_results = {}
    heph_result  = {}

    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = {
            ex.submit(run_tool, name, tool, cmd, sym): name
            for name, (tool, cmd, sym) in scan_tasks.items()
        }
        futures[ex.submit(run_tool, "HEPH", "hephaestus_tool.py", "status", "")] = "HEPH"

        for fut in as_completed(futures, timeout=SCAN_TIMEOUT+10):
            name = futures[fut]
            try:
                result = fut.result(timeout=1)
                if name == "HEPH":
                    heph_result = result
                else:
                    scan_results[name] = result
            except Exception as e:
                if name == "HEPH": heph_result = {"error": str(e)[:60]}
                else: scan_results[name] = {"action":"wait","reason":f"Scan error: {str(e)[:60]}"}

    # ── Account + positions ────────────────────────────────────────
    acc       = bridge("/balance")
    positions = bridge("/positions")
    if not isinstance(positions, list): positions = []

    # ── Journal summaries ──────────────────────────────────────────
    journal_summaries = {tag: journal_summary(path, hours=1)
                         for tag, path in JOURNALS.items()}

    # ── Build and send ─────────────────────────────────────────────
    report = build_report(acc, positions, scan_results, heph_result, journal_summaries)
    tg(report)
    elapsed = round(time.time() - start, 1)
    log.info(f"=== Brain Feed sent ({elapsed}s) ===")

if __name__ == "__main__":
    run_broadcast()
