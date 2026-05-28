#!/usr/bin/env python3
"""
GENESIS Autonomous Strategy Execution Engine
Runs every 5 minutes. Scans all strategies, executes signals, manages risk.
No AI/LLM cost — the strategies decide everything algorithmically.
Calls API2TRADE directly — no local bridge required.
"""
import json, subprocess, logging, requests, os
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

# ── Config ────────────────────────────────────────────────────────────────────
PY        = "/opt/hermes-agent/.venv-hermes/bin/python3"
AGENT     = "/opt/hermes-agent"
TOKEN     = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT      = os.getenv("TELEGRAM_CHAT_ID", "")
LOG       = Path("/var/log/hermes/autonomous.log")
SNAP      = Path("/var/log/hermes/position_snapshot.json")

# API2TRADE — direct REST calls, no local bridge needed
MT5_API   = os.getenv("MT5_API_URL", "https://mt5.mt4api.dev")
MT5_ID    = os.getenv("MT5_ACCOUNT_UUID", "")
MT5_KEY   = os.getenv("MT5_API_KEY", "")
MT5_AUTH  = (os.getenv("MT5_API_USER", ""),
             os.getenv("MT5_API_PASS", ""))  # Set in .env

MAX_TOTAL_POSITIONS = int(os.getenv("MAX_POSITIONS", 4))
MAX_PER_STRATEGY    = 1

logging.basicConfig(
    filename=str(LOG), level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("genesis")

# ── Strategies: (name, short, symbols_to_scan) ───────────────────────────────
STRATEGIES = [
    ("ARES",      "ares",      ["EURUSDxx", "GBPUSDxx"]),
    ("APOLLO",    "apollo",    ["EURUSDxx", "GBPUSDxx", "XAUUSDxx"]),
    ("ATHENA",    "athena",    ["EURUSDxx", "GBPUSDxx"]),
    ("ARTEMIS",   "artemis",   ["EURUSDxx", "GBPUSDxx", "GBPJPYxx"]),
    ("ZEUS",      "zeus",      ["EURUSDxx", "GBPUSDxx", "XAUUSDxx"]),
]

def tg(msg):
    """Send Telegram message."""
    if not TOKEN: return
    try:
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": CHAT, "text": msg, "parse_mode": "Markdown"},
            timeout=10)
    except Exception as e:
        log.warning(f"Telegram failed: {e}")

def api(endpoint, method="GET", data=None):
    """
    Direct API2TRADE REST call. No local bridge needed.
    GET  requests use query params with id=MT5_ID.
    POST-style order operations use GET with params (API2TRADE convention).
    """
    try:
        base_params = {"id": MT5_ID}

        # Map local-bridge-style paths to API2TRADE endpoints
        ep_map = {
            "/balance":   "AccountSummary",
            "/positions": "OpenedOrders",
            "/history":   "ClosedOrders",
            "/symbols":   "SymbolList",
        }

        # Handle /quote?symbol=... path
        if endpoint.startswith("/quote"):
            sym = endpoint.split("symbol=")[-1]
            r = requests.get(f"{MT5_API}/Quote",
                params={**base_params, "symbol": sym},
                auth=MT5_AUTH, timeout=10)
            raw = r.json()
            return {
                "bid": float(raw.get("Bid", raw.get("bid", 0))),
                "ask": float(raw.get("Ask", raw.get("ask", 0))),
                "symbol": sym,
            }

        # Map /market POST to OrderSendSafe GET
        if endpoint == "/market" and data:
            params = {
                **base_params,
                "symbol":     data.get("symbol"),
                "operation":  data.get("type"),
                "volume":     data.get("volume"),
                "stoploss":   data.get("stop_loss"),
                "takeprofit": data.get("take_profit"),
                "comment":    data.get("comment", "GENESIS-v2"),
            }
            r = requests.get(f"{MT5_API}/OrderSendSafe",
                params=params, auth=MT5_AUTH, timeout=15)
            raw = r.json()
            ticket = raw.get("ticket") or raw.get("Ticket") or raw.get("integerResponse")
            return {"ticket": ticket} if ticket else raw

        # Map /close POST to OrderCloseSafe GET
        if endpoint == "/close" and data:
            # Need lots — fetch from open positions
            ticket = data.get("ticket")
            lots = 0.01
            pos_raw = requests.get(f"{MT5_API}/OpenedOrders",
                params=base_params, auth=MT5_AUTH, timeout=10).json()
            for p in (pos_raw if isinstance(pos_raw, list) else []):
                if str(p.get("Ticket", p.get("ticket", ""))) == str(ticket):
                    lots = float(p.get("Volume", p.get("lots", 0.01)))
                    break
            r = requests.get(f"{MT5_API}/OrderCloseSafe",
                params={**base_params, "ticket": ticket, "lots": lots},
                auth=MT5_AUTH, timeout=15)
            return {"message": "ok"} if r.status_code == 200 else r.json()

        # Map /modify POST to OrderModifySafe GET
        if endpoint == "/modify" and data:
            params = {**base_params, "ticket": data.get("ticket")}
            if data.get("stop_loss"):   params["stoploss"]   = data["stop_loss"]
            if data.get("take_profit"): params["takeprofit"] = data["take_profit"]
            r = requests.get(f"{MT5_API}/OrderModifySafe",
                params=params, auth=MT5_AUTH, timeout=10)
            return {"ok": True} if r.status_code == 200 else r.json()

        # Standard GET endpoints
        cloud_ep = ep_map.get(endpoint, endpoint.lstrip("/"))
        r = requests.get(f"{MT5_API}/{cloud_ep}",
            params=base_params, auth=MT5_AUTH, timeout=10)
        raw = r.json()

        # Normalise balance response
        if endpoint == "/balance" and isinstance(raw, dict):
            return {
                "balance": float(raw.get("Balance", raw.get("balance", 0))),
                "equity":  float(raw.get("Equity",  raw.get("equity",  0))),
                "margin":  float(raw.get("Margin",  raw.get("margin",  0))),
                "profit":  float(raw.get("Profit",  raw.get("profit",  0))),
            }

        # Normalise positions response
        if endpoint == "/positions" and isinstance(raw, list):
            return [{
                "ticket":    p.get("Ticket",    p.get("ticket", 0)),
                "symbol":    p.get("Symbol",    p.get("symbol", "")),
                "orderType": p.get("Type",      p.get("orderType", "")),
                "lots":      float(p.get("Volume", p.get("lots", 0))),
                "openPrice": float(p.get("Price",  p.get("openPrice", 0))),
                "profit":    float(p.get("Profit", p.get("profit", 0))),
                "comment":   p.get("Comment",   p.get("comment", "")),
            } for p in raw]

        return raw

    except Exception as e:
        log.error(f"API2TRADE {endpoint}: {e}")
        return {}

def run_tool(tool, cmd, sym=""):
    """Run a strategy tool as subprocess. Path: strategies/{tool}/{tool}_tool.py"""
    tool_path = f"{AGENT}/strategies/{tool}/{tool}_tool.py"
    args = [PY, tool_path, cmd]
    if sym: args.append(sym)
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=50,
                           cwd=f"{AGENT}/strategies/{tool}")
        out = r.stdout.strip()
        return json.loads(out) if out else {"error": r.stderr.strip()[:120]}
    except Exception as e:
        return {"error": str(e)[:80]}

def pip_val(sym):
    if "JPY" in sym: return 0.01
    if "XAU" in sym: return 0.1
    return 0.0001

def already_has_position(open_pos, strategy_tag):
    """Check if a strategy already has an open position."""
    return any(strategy_tag.lower() in str(p.get("comment","")).lower() for p in open_pos)

def execute_signal(tool, sym, result, open_pos, balance):
    """Execute a trade signal with risk validation."""
    direction = result.get("direction", "")
    sl = result.get("stop_loss")
    tp = result.get("take_profit")
    vol = result.get("volume", 0.1)

    if not all([direction, sl, tp, vol]):
        log.warning(f"{tool}/{sym}: Signal missing fields: {result}")
        return False

    # Risk check: SL distance reasonable?
    q = api(f"/quote?symbol={sym}")
    price = float(q.get("ask" if direction=="Buy" else "bid", 0))
    if price <= 0:
        log.warning(f"{tool}/{sym}: Cannot get quote")
        return False

    pip = pip_val(sym)
    sl_pips = abs(price - float(sl)) / pip
    if sl_pips > 150:
        log.warning(f"{tool}/{sym}: SL too wide ({sl_pips:.0f} pips), skipping")
        return False
    if sl_pips < 3:
        log.warning(f"{tool}/{sym}: SL too tight ({sl_pips:.0f} pips), skipping")
        return False

    # Execute
    order = api("/market", "POST", {
        "symbol": sym,
        "volume": vol,
        "type": direction,
        "stop_loss": float(sl),
        "take_profit": float(tp),
        "comment": f"{tool.upper()}-v1"
    })

    ticket = order.get("ticket") or order.get("Ticket")
    if ticket:
        rr = result.get("rr_ratio", "?")
        msg = (
            f"🟢 *TRADE OPENED*\n"
            f"Strategy: {tool.upper()}\n"
            f"{sym} {direction} {vol}lot\n"
            f"Entry: {price:.5f} | SL: {sl} | TP: {tp}\n"
            f"R:R = {rr} | SL = {sl_pips:.0f}pips\n"
            f"Balance: €{balance:,.2f}"
        )
        tg(msg)
        log.info(f"OPENED: {tool}/{sym} {direction} {vol}lot ticket={ticket} SL={sl} TP={tp}")
        return True
    else:
        err = order.get("message", str(order))[:100]
        log.error(f"Order failed {tool}/{sym}: {err}")
        return False

def scan_and_execute():
    now = datetime.now(timezone.utc)
    log.info(f"=== Autonomous cycle {now.strftime('%Y-%m-%d %H:%M')} UTC ===")

    # Account state
    acc      = api("/balance")
    pos_data = api("/positions")
    balance  = float(acc.get("balance", 0))
    equity   = float(acc.get("equity", 0))
    open_pos = pos_data if isinstance(pos_data, list) else []
    n_open   = len(open_pos)

    log.info(f"Balance=€{balance:.2f} Equity=€{equity:.2f} OpenPositions={n_open}")

    if n_open >= MAX_TOTAL_POSITIONS:
        log.info(f"Max positions reached ({n_open}/{MAX_TOTAL_POSITIONS}). Skipping scans.")
        return

    # Position snapshot for trade-monitor (detect closes)
    curr_snap = {str(p.get("ticket")): p for p in open_pos}
    prev_snap = {}
    if SNAP.exists():
        try: prev_snap = json.loads(SNAP.read_text())
        except: pass

    # Detect closed positions and notify
    for ticket, p in prev_snap.items():
        if ticket not in curr_snap:
            hist = api("/history")
            pnl  = None
            if isinstance(hist, list):
                for h in reversed(hist):
                    if str(h.get("ticket")) == ticket:
                        pnl = float(h.get("profit", h.get("pnl", 0)))
                        break
            icon = "🟢" if (pnl or 0) >= 0 else "🔴"
            tg(f"{icon} *TRADE CLOSED*\n"
               f"{p.get('symbol')} {p.get('orderType','').upper()} "
               f"{p.get('lots')}lot [{p.get('comment')}]\n"
               f"Result: €{pnl:+.2f}" if pnl is not None else "Result: see MT5")
            log.info(f"CLOSED: ticket={ticket} {p.get('symbol')} pnl={pnl}")

    SNAP.parent.mkdir(parents=True, exist_ok=True)
    SNAP.write_text(json.dumps(curr_snap))

    # Slots available
    slots = MAX_TOTAL_POSITIONS - n_open
    log.info(f"Available slots: {slots}")

    # Parallel scans
    scan_tasks = []
    for strat_name, tool, symbols in STRATEGIES:
        if already_has_position(open_pos, strat_name):
            log.info(f"{strat_name}: position already open, skipping scan")
            continue
        for sym in symbols:
            scan_tasks.append((strat_name, tool, sym))

    if not scan_tasks:
        log.info("No scan tasks — all strategies have open positions")
        return

    results = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(run_tool, tool, "analyze", sym): (strat, sym)
                   for strat, tool, sym in scan_tasks}
        for fut in as_completed(futures, timeout=60):
            strat, sym = futures[fut]
            try:
                r = fut.result(timeout=1)
                key = f"{strat}/{sym}"
                results[key] = r
                action = r.get("action", "?")
                reason = r.get("reason", "")[:70]
                log.info(f"  {key}: {action} | {reason}")
            except Exception as e:
                log.warning(f"  Scan error: {e}")

    # Find signals
    signals = [(k, v) for k, v in results.items() if v.get("action") == "trade"]
    log.info(f"Signals found: {len(signals)}")

    # Execute best signals (up to available slots)
    executed = 0
    for key, result in signals:
        if executed >= slots:
            break
        strat_name, sym = key.split("/", 1)
        tool = strat_name.lower()

        # Double-check position not opened by another signal in this cycle
        if already_has_position(open_pos, strat_name):
            continue

        log.info(f"Executing: {key} {result.get('direction')}")
        success = execute_signal(tool, sym, result, open_pos, balance)
        if success:
            executed += 1
            # Refresh positions so next iteration sees the new position
            pos_data = api("/positions")
            open_pos = pos_data if isinstance(pos_data, list) else open_pos

    if executed == 0 and not signals:
        log.info("No signals this cycle — all strategies waiting")

    # Run Hephaestus grid tick
    heph_r = run_tool("hephaestus", "tick")  # path: strategies/hephaestus/hephaestus_tool.py
    log.info(f"Hephaestus tick: {str(heph_r)[:80]}")

    log.info(f"=== Cycle complete. Executed {executed} trade(s) ===")

if __name__ == "__main__":
    try:
        scan_and_execute()
    except Exception as e:
        log.error(f"CRASH: {e}", exc_info=True)
        tg(f"🚨 *GENESIS AUTONOMOUS CRASH*: {str(e)[:200]}")
