#!/usr/bin/env python3
"""
GENESIS — Hephaestus (Strategy F: Grid + Martingale)

⚠️ EXTREME RISK WARNING ⚠️
Grid/Martingale strategies can produce 90%+ win rates but carry the risk of
CATASTROPHIC, UNLIMITED DRAWDOWN if price trends strongly without reversal.
Circuit breakers in this code REDUCE but DO NOT ELIMINATE this risk.

NEVER run on live account without:
- Backtesting over trending AND ranging regimes
- max_grid_levels ≤ 5 and initial_lot = 0.01
- Monitoring at least daily
- Setting confirm_risk_acknowledged: true only after understanding the above

Architecture: This runs as a STATE MACHINE, not a signal generator.
- State is persisted in /var/log/hephaestus/grid_state.json
- Hermes calls hephaestus_tool.py to check status / start / stop
- The tool is NOT meant to run continuously — it's polled by Hermes
"""
import os, json, time, logging, math
from datetime import datetime, timezone, date
from pathlib import Path
import requests, yaml

CONFIG_PATH = Path(__file__).parent / "hephaestus_config.yaml"
if not CONFIG_PATH.exists():
    CONFIG_PATH = Path(__file__).parents[2] / "configs" / "hephaestus_config.yaml"
with open(CONFIG_PATH) as f:
    CFG = yaml.safe_load(f)

# ── Risk gate — hard stop if user hasn't acknowledged ─────────────────────────
if not CFG["strategy"].get("confirm_risk_acknowledged"):
    raise RuntimeError(
        "HEPHAESTUS BLOCKED: Set confirm_risk_acknowledged: true in hephaestus_config.yaml "
        "after reading the risk warning. This strategy can blow your account."
    )

BRIDGE     = os.getenv("ARES_BRIDGE_URL", CFG["bridge"]["url"])
TG_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID = str(CFG["telegram"]["chat_id"])
# Resolve safe journal path (fallback to local logs/ if system dir not writable)
default_journal = CFG["journal"]["path"]
try:
    Path(default_journal).parent.mkdir(parents=True, exist_ok=True)
    JOURNAL = Path(default_journal)
except Exception:
    local_log_dir = Path(__file__).parents[2] / "logs" / "hephaestus"
    local_log_dir.mkdir(parents=True, exist_ok=True)
    JOURNAL = local_log_dir / "trade_journal.jsonl"
STATE_FILE = Path("/var/log/hephaestus/grid_state.json")

STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
COMMENT    = CFG["strategy"]["comment"]

SYMBOL       = CFG["symbol"]
DIRECTION    = CFG["direction"]
INIT_LOT     = float(CFG["grid"]["initial_lot"])
MULTIPLIER   = float(CFG["grid"]["martingale_multiplier"])
MAX_LOT      = float(CFG["grid"]["max_lot_per_order"])
SPACING      = int(CFG["grid"]["grid_spacing_pips"])
MAX_LEVELS   = int(CFG["grid"]["max_grid_levels"])
TP_PIPS      = int(CFG["grid"]["take_profit_pips"])
BASKET_TP    = int(CFG["grid"]["basket_tp_pips"])

MAX_DD_PCT   = float(CFG["circuit_breakers"]["max_equity_drawdown_pct"])
MAX_DL_PCT   = float(CFG["circuit_breakers"]["max_daily_loss_pct"])
MAX_CONSEC   = int(CFG["circuit_breakers"]["max_consecutive_losses"])
COOLDOWN     = int(CFG["circuit_breakers"]["cooldown_after_reset_sec"])
MAX_SPREAD   = float(CFG["circuit_breakers"]["max_spread_pips"])
MAX_LOTS     = float(CFG["circuit_breakers"]["max_total_lots"])
START_H      = int(CFG["sessions"]["allowed"][0]["start"])
END_H        = int(CFG["sessions"]["allowed"][0]["end"])

# Resolve safe log path (fallback to local logs/ if system dir not writable)
default_log = "/var/log/hephaestus/hephaestus_cycle.log"
try:
    Path(default_log).parent.mkdir(parents=True, exist_ok=True)
    log_file = default_log
except Exception:
    local_log_dir = Path(__file__).parents[2] / "logs" / "hephaestus"
    local_log_dir.mkdir(parents=True, exist_ok=True)
    log_file = str(local_log_dir / "hephaestus_cycle.log")

logging.basicConfig(
    filename=log_file,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger(__name__)

# ── State management ───────────────────────────────────────────────────────────
DEFAULT_STATE = {
    "enabled":          True,
    "buy_level":        0,          # Current martingale level for buys (0=initial)
    "sell_level":       0,
    "buy_tickets":      [],         # Open buy position tickets
    "sell_tickets":     [],         # Open sell position tickets
    "consec_buy_loss":  0,
    "consec_sell_loss": 0,
    "peak_equity":      0.0,
    "day_start_bal":    0.0,
    "last_day":         str(date.today()),
    "last_reset_ts":    0,
    "total_cycles":     0,
    "killed_reason":    None,
}

def load_state() -> dict:
    if STATE_FILE.exists():
        try: return json.loads(STATE_FILE.read_text())
        except: pass
    return dict(DEFAULT_STATE)

def save_state(s: dict):
    STATE_FILE.write_text(json.dumps(s, default=str))

def bridge(path, method="GET", data=None) -> dict:
    """
    Calls API2TRADE REST API directly. No local bridge required.
    Replaces the old localhost:8000 bridge.
    """
    try:
        base = {"id": MT5_ID}
        ep_map = {
            "/balance":   "AccountSummary",
            "/positions": "OpenedOrders",
            "/history":   "ClosedOrders",
        }
        if path.startswith("/quote"):
            sym = path.split("symbol=")[-1]
            # Try API2TRADE live quote first
            try:
                r = requests.get(f"{MT5_API}/Quote",
                    params={**base, "symbol": sym},
                    auth=MT5_AUTH, timeout=8)
                if r.status_code == 200 and r.text.strip():
                    raw = r.json()
                    bid = float(raw.get("Bid", raw.get("bid", 0)))
                    ask = float(raw.get("Ask", raw.get("ask", 0)))
                    if bid > 0 and ask > 0:
                        return {"bid": bid, "ask": ask}
            except Exception:
                pass
            # Fallback: yfinance last close (same source as bar data)
            # Fallback 2: open.er-api.com (free, no key, Forex pairs)
            try:
                FOREX_BASE = {
                    "EURUSDxx": ("EUR","USD"), "GBPUSDxx": ("GBP","USD"),
                    "USDJPYxx": ("USD","JPY"), "GBPJPYxx": ("GBP","JPY"),
                    "EURGBPxx": ("EUR","GBP"), "EURUSD":   ("EUR","USD"),
                    "GBPUSD":   ("GBP","USD"),
                }
                if sym in FOREX_BASE:
                    base_ccy, quote_ccy = FOREX_BASE[sym]
                    r2 = requests.get(
                        f"https://open.er-api.com/v6/latest/{base_ccy}",
                        timeout=8
                    )
                    if r2.status_code == 200:
                        rates = r2.json().get("rates", {})
                        price = float(rates.get(quote_ccy, 0))
                        if price > 0:
                            pip = 0.01 if "JPY" in sym else 0.0001
                            spread = pip * 1.5
                            log.info(f"Quote via open.er-api: {sym} = {price}")
                            return {"bid": round(price - spread/2, 6), "ask": round(price + spread/2, 6)}
            except Exception as e:
                log.warning(f"open.er-api quote failed for {sym}: {e}")
            # Fallback 3: Frankfurter (ECB rates, XAU support)
            try:
                FRANK_MAP = {
                    "EURUSDxx": "EUR/USD", "GBPUSDxx": "GBP/USD",
                    "USDJPYxx": "USD/JPY", "XAUUSDxx": "XAU/USD",
                    "EURUSD": "EUR/USD", "XAUUSD": "XAU/USD",
                }
                if sym in FRANK_MAP:
                    pair = FRANK_MAP[sym]
                    base_c, quote_c = pair.split("/")
                    r3 = requests.get(
                        f"https://api.frankfurter.app/latest?from={base_c}&to={quote_c}",
                        timeout=8
                    )
                    if r3.status_code == 200:
                        price = float(r3.json().get("rates", {}).get(quote_c, 0))
                        if price > 0:
                            pip = 0.01 if "JPY" in sym else (0.1 if "XAU" in sym else 0.0001)
                            spread = pip * 1.5
                            log.info(f"Quote via frankfurter: {sym} = {price}")
                            return {"bid": round(price - spread/2, 6), "ask": round(price + spread/2, 6)}
            except Exception as e:
                log.warning(f"frankfurter quote failed for {sym}: {e}")
            return {"error": f"No quote available for {sym}"}
        if path == "/market" and data:
            params = {
                **base,
                "symbol":     data.get("symbol"),
                "operation":  data.get("type"),
                "volume":     data.get("volume"),
                "stoploss":   data.get("stop_loss"),
                "takeprofit": data.get("take_profit"),
                "comment":    data.get("comment", "GENESIS"),
            }
            r = requests.get(f"{MT5_API}/OrderSendSafe",
                params=params, auth=MT5_AUTH, timeout=15)
            raw = r.json()
            ticket = raw.get("ticket") or raw.get("Ticket") or raw.get("integerResponse")
            return {"ticket": ticket} if ticket else raw
        if path == "/close" and data:
            ticket = data.get("ticket")
            lots = float(data.get("lots", 0.01))
            r = requests.get(f"{MT5_API}/OrderCloseSafe",
                params={**base, "ticket": ticket, "lots": lots},
                auth=MT5_AUTH, timeout=15)
            return {"message": "ok"} if r.status_code == 200 else r.json()
        if path == "/modify" and data:
            params = {**base, "ticket": data.get("ticket")}
            if data.get("stop_loss"):   params["stoploss"]   = data["stop_loss"]
            if data.get("take_profit"): params["takeprofit"] = data["take_profit"]
            r = requests.get(f"{MT5_API}/OrderModifySafe",
                params=params, auth=MT5_AUTH, timeout=10)
            return {"ok": True} if r.status_code == 200 else r.json()
        cloud_ep = ep_map.get(path, path.lstrip("/"))
        r = requests.get(f"{MT5_API}/{cloud_ep}",
            params=base, auth=MT5_AUTH, timeout=10)
        raw = r.json()
        if path == "/balance" and isinstance(raw, dict):
            return {
                "balance": float(raw.get("Balance", raw.get("balance", 0))),
                "equity":  float(raw.get("Equity",  raw.get("equity",  0))),
                "margin":  float(raw.get("Margin",  raw.get("margin",  0))),
                "profit":  float(raw.get("Profit",  raw.get("profit",  0))),
            }
        if path == "/positions" and isinstance(raw, list):
            return [{
                "ticket":    p.get("Ticket",  p.get("ticket", 0)),
                "symbol":    p.get("Symbol",  p.get("symbol", "")),
                "orderType": p.get("Type",    p.get("orderType", "")),
                "lots":      float(p.get("Volume", p.get("lots", 0))),
                "openPrice": float(p.get("Price",  p.get("openPrice", 0))),
                "profit":    float(p.get("Profit", p.get("profit", 0))),
                "comment":   p.get("Comment", p.get("comment", "")),
            } for p in raw]
        return raw
    except Exception as e:
        log.error(f"API2TRADE {path}: {e}")
        return {"error": str(e)}


def tg(msg: str):
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=10)
    except: pass

def pip(sym): return 0.01 if "JPY" in sym else (0.1 if "XAU" in sym else 0.0001)
def to_pips(diff, sym): return abs(diff) / pip(sym)

def is_trade_time():
    now = datetime.now(timezone.utc)
    wd, hr = now.weekday(), now.hour
    if (wd==4 and hr>=22) or wd==5 or (wd==6 and hr<22): return False
    return START_H <= hr < END_H

def lot_for_level(level: int) -> float:
    """Martingale lot: initial × multiplier^level, capped at MAX_LOT."""
    lot = INIT_LOT * (MULTIPLIER ** level)
    return round(min(lot, MAX_LOT), 2)

def total_exposure(positions: list) -> float:
    return sum(float(p.get("lots",0)) for p in positions
               if COMMENT.split("-")[0] in str(p.get("comment","")))

def get_heph_positions(positions: list, direction: str = None) -> list:
    tag = COMMENT.split("-")[0]
    res = [p for p in (positions if isinstance(positions,list) else [])
           if tag in str(p.get("comment","")).upper()]
    if direction:
        res = [p for p in res if p.get("orderType","").lower()==direction.lower()]
    return res

# ── Circuit breaker evaluation ─────────────────────────────────────────────────
def check_circuit_breakers(state: dict, acc: dict) -> tuple[bool, str]:
    """Returns (killed, reason). Updates state in-place if kill triggered."""
    equity  = float(acc.get("equity", 0))
    balance = float(acc.get("balance", 0))

    # Reset daily tracking if new day
    today = str(date.today())
    if state["last_day"] != today:
        state["last_day"]      = today
        state["day_start_bal"] = balance
        log.info("New day — daily loss counter reset.")

    if state["peak_equity"] < equity:
        state["peak_equity"] = equity

    # 1. Equity drawdown from peak
    if state["peak_equity"] > 0:
        dd_pct = (state["peak_equity"] - equity) / state["peak_equity"] * 100
        if dd_pct >= MAX_DD_PCT:
            return True, f"EQUITY DRAWDOWN {dd_pct:.2f}% ≥ {MAX_DD_PCT}% — EMERGENCY STOP"

    # 2. Daily loss
    if state["day_start_bal"] > 0:
        daily_loss_pct = (state["day_start_bal"] - balance) / state["day_start_bal"] * 100
        if daily_loss_pct >= MAX_DL_PCT:
            return True, f"DAILY LOSS {daily_loss_pct:.2f}% ≥ {MAX_DL_PCT}% — STOPPED FOR DAY"

    # 3. Consecutive losses
    if state["consec_buy_loss"] >= MAX_CONSEC or state["consec_sell_loss"] >= MAX_CONSEC:
        return True, f"MAX CONSECUTIVE LOSSES ({MAX_CONSEC}) reached — RESET GRID"

    return False, ""

def emergency_stop(state: dict, reason: str, positions: list) -> dict:
    """Close ALL Hephaestus positions and disable strategy."""
    log.error(f"EMERGENCY STOP: {reason}")
    tg(f"🚨 *HEPHAESTUS EMERGENCY STOP*\n`{reason}`\nClosing all grid positions now.")
    closed = 0
    for p in get_heph_positions(positions):
        r = bridge("/close","POST",{"ticket": p["ticket"]})
        if r.get("ticket") or not r.get("error"): closed += 1
    state["enabled"]       = False
    state["killed_reason"] = reason
    state["buy_level"]     = 0
    state["sell_level"]    = 0
    state["buy_tickets"]   = []
    state["sell_tickets"]  = []
    save_state(state)
    tg(f"🚨 *HEPHAESTUS*: {closed} positions closed. Strategy DISABLED.")
    return state

def reset_grid(state: dict, positions: list, reason: str = "basket TP hit") -> dict:
    """Close all positions, reset levels, apply cooldown."""
    log.info(f"Grid reset: {reason}")
    closed = 0
    total_pnl = 0.0
    for p in get_heph_positions(positions):
        pnl = float(p.get("profit",0))
        r   = bridge("/close","POST",{"ticket": p["ticket"]})
        if not r.get("error"):
            closed += 1
            total_pnl += pnl
            _journal(p, pnl, "reset")
    state.update({
        "buy_level":0,"sell_level":0,
        "buy_tickets":[],"sell_tickets":[],
        "consec_buy_loss":0,"consec_sell_loss":0,
        "last_reset_ts": time.time(),
        "total_cycles": state.get("total_cycles",0) + 1,
    })
    save_state(state)
    tg(f"🔄 *HEPHAESTUS GRID RESET* ({reason})\n"
       f"Closed {closed} positions | Cycle PnL: €{total_pnl:.2f}\n"
       f"Total cycles: {state['total_cycles']} | Cooldown: {COOLDOWN}s")
    return state

def _journal(p, pnl, result):
    
    with open(JOURNAL,"a") as f:
        f.write(json.dumps({
            "ticket":str(p.get("ticket")),"symbol":p.get("symbol"),
            "type":p.get("orderType"),"lots":p.get("lots"),
            "pnl":round(pnl,2),"result":result,
            "ts":datetime.now(timezone.utc).isoformat(),"strategy":"hephaestus-grid"
        })+"\n")

# ── Core cycle tick ────────────────────────────────────────────────────────────
def run_cycle() -> dict:
    """
    Main Hephaestus logic tick. Called by hephaestus_tool.py on schedule.
    Returns status dict describing current grid state and any actions taken.
    """
    state     = load_state()
    actions   = []

    if not state["enabled"]:
        return {"status":"disabled","reason":state.get("killed_reason","unknown"),"state":state}

    # ── Account ─────────────────────────────────────────────────────
    acc = bridge("/balance")
    if "error" in acc:
        return {"status":"error","reason":f"Bridge: {acc['error']}"}
    equity  = float(acc.get("equity",0))
    balance = float(acc.get("balance",0))

    # Initialize peak/day_start
    if state["peak_equity"] == 0: state["peak_equity"] = equity
    if state["day_start_bal"] == 0: state["day_start_bal"] = balance

    # ── Circuit breakers ─────────────────────────────────────────────
    positions = bridge("/positions")
    if not isinstance(positions,list): positions = []

    killed, kill_reason = check_circuit_breakers(state, acc)
    if killed:
        state = emergency_stop(state, kill_reason, positions)
        return {"status":"emergency_stop","reason":kill_reason}

    # ── Session check ────────────────────────────────────────────────
    if not is_trade_time():
        save_state(state)
        return {"status":"outside_session","equity":equity,"state":state}

    # ── Cooldown check ───────────────────────────────────────────────
    if time.time() - state["last_reset_ts"] < COOLDOWN:
        remaining = int(COOLDOWN - (time.time()-state["last_reset_ts"]))
        save_state(state)
        return {"status":"cooldown","remaining_seconds":remaining}

    # ── Quote + spread ───────────────────────────────────────────────
    quote = bridge(f"/quote?symbol={SYMBOL}")
    if "error" in quote or not quote.get("bid"):
        return {"status":"no_quote"}
    bid = float(quote["bid"]); ask = float(quote["ask"])
    spread_pips = to_pips(ask-bid, SYMBOL)
    if spread_pips > MAX_SPREAD:
        return {"status":"spread_too_wide","spread":spread_pips}

    # ── Exposure check ───────────────────────────────────────────────
    heph_pos   = get_heph_positions(positions)
    total_lots = total_exposure(positions)
    if total_lots >= MAX_LOTS:
        tg(f"⚠️ *HEPHAESTUS*: Max exposure {total_lots:.2f}lots ≥ {MAX_LOTS}. No new levels.")
        save_state(state)
        return {"status":"max_exposure","lots":total_lots}

    # ── Check basket TP ──────────────────────────────────────────────
    basket_pnl = sum(float(p.get("profit",0)) for p in heph_pos)
    basket_tp_eur = BASKET_TP * pip(SYMBOL) * 100000 * INIT_LOT  # Approx EUR value
    if heph_pos and basket_pnl >= basket_tp_eur:
        state = reset_grid(state, heph_pos, f"basket TP hit €{basket_pnl:.2f}")
        save_state(state)
        return {"status":"basket_tp_hit","pnl":basket_pnl}

    # ── Check individual position outcomes ───────────────────────────
    for p in heph_pos:
        ticket = str(p.get("ticket"))
        pnl    = float(p.get("profit",0))
        tp_eur = TP_PIPS * pip(SYMBOL) * 100000 * float(p.get("lots",0.01))
        # Close if individual TP hit
        if pnl >= tp_eur:
            r = bridge("/close","POST",{"ticket": p["ticket"]})
            if not r.get("error"):
                _journal(p, pnl, "win")
                dir_ = p.get("orderType","Buy")
                if dir_ == "Buy":
                    state["consec_buy_loss"] = 0
                    if ticket in [str(t) for t in state["buy_tickets"]]:
                        state["buy_tickets"] = [t for t in state["buy_tickets"] if str(t)!=ticket]
                        if state["buy_level"] > 0: state["buy_level"] -= 1
                else:
                    state["consec_sell_loss"] = 0
                    if ticket in [str(t) for t in state["sell_tickets"]]:
                        state["sell_tickets"] = [t for t in state["sell_tickets"] if str(t)!=ticket]
                        if state["sell_level"] > 0: state["sell_level"] -= 1
                actions.append(f"Closed TP {dir_} ticket {ticket} P&L €{pnl:.2f}")
                log.info(f"TP hit: {dir_} ticket {ticket} PnL={pnl:.2f}")

    # ── Open new grid level if no position in that direction ──────────
    def open_level(direction: str):
        level = state[f"{direction.lower()}_level"]
        if level >= MAX_LEVELS:
            tg(f"⚠️ *HEPHAESTUS*: Max levels ({MAX_LEVELS}) reached for {direction}. Waiting.")
            return None
        lot = lot_for_level(level)
        sl_price = (round(bid - SPACING*2*pip(SYMBOL),6) if direction=="Buy"
                    else round(ask + SPACING*2*pip(SYMBOL),6))
        tp_price = (round(ask + TP_PIPS*pip(SYMBOL),6) if direction=="Buy"
                    else round(bid - TP_PIPS*pip(SYMBOL),6))
        order = bridge("/market","POST",{
            "symbol":SYMBOL,"volume":lot,"type":direction,
            "stop_loss":sl_price,"take_profit":tp_price,"comment":COMMENT
        })
        ticket = order.get("ticket") or order.get("Ticket")
        if ticket:
            state[f"{direction.lower()}_tickets"].append(str(ticket))
            state[f"{direction.lower()}_level"] = level + 1
            _journal({"ticket":ticket,"symbol":SYMBOL,"orderType":direction,"lots":lot,"profit":0}, 0, "open")
            log.info(f"Grid {direction} Level {level} opened: lot={lot} ticket={ticket}")
            actions.append(f"Opened {direction} Level {level} lot={lot} ticket={ticket}")
            tg(f"🔩 *HEPHAESTUS*: {direction} Level {level+1} | Lot {lot} | Ticket `{ticket}`")
            return ticket
        else:
            log.error(f"Order failed: {order}")
            return None

    # Open buys if no active buy position
    active_buys  = get_heph_positions(heph_pos, "Buy")
    active_sells = get_heph_positions(heph_pos, "Sell")

    if DIRECTION in ("buy_only","both") and not active_buys:
        open_level("Buy")

    if DIRECTION in ("sell_only","both") and not active_sells:
        open_level("Sell")

    save_state(state)

    return {
        "status":      "running",
        "equity":      equity,
        "basket_pnl":  round(basket_pnl,2),
        "total_lots":  round(total_lots,2),
        "buy_level":   state["buy_level"],
        "sell_level":  state["sell_level"],
        "spread":      round(spread_pips,2),
        "actions":     actions,
        "open_positions": len(heph_pos),
        "state":       {k:v for k,v in state.items() if k not in ("buy_tickets","sell_tickets")},
    }

def get_status() -> dict:
    """Status snapshot — does NOT modify state or open orders."""
    state = load_state()
    acc   = bridge("/balance")
    pos   = bridge("/positions")
    heph  = get_heph_positions(pos if isinstance(pos,list) else [])
    pnl   = sum(float(p.get("profit",0)) for p in heph)
    lots  = sum(float(p.get("lots",0)) for p in heph)
    w=l=0
    if JOURNAL.exists():
        for line in JOURNAL.read_text().strip().split("\n"):
            if not line: continue
            try:
                t=json.loads(line)
                if t.get("result")=="win": w+=1
                if t.get("result")=="loss": l+=1
            except: pass
    return {
        "enabled":       state["enabled"],
        "killed_reason": state.get("killed_reason"),
        "buy_level":     state["buy_level"],
        "sell_level":    state["sell_level"],
        "open_positions": len(heph),
        "total_lots":    round(lots,2),
        "unrealized_pnl": round(pnl,2),
        "peak_equity":   state["peak_equity"],
        "total_cycles":  state["total_cycles"],
        "account":       acc,
        "journal":       {"wins":w,"losses":l},
        "circuit_breakers": {
            "max_dd_pct":    MAX_DD_PCT,
            "max_daily_loss":MAX_DL_PCT,
            "max_levels":    MAX_LEVELS,
            "max_lots":      MAX_LOTS,
        }
    }

def emergency_kill() -> dict:
    """Force-kill from external call (Hermes or manual)."""
    state = load_state()
    pos   = bridge("/positions")
    state = emergency_stop(state, "Manual kill via hephaestus_tool.py kill", 
                           pos if isinstance(pos,list) else [])
    return {"killed":True,"state":state}

def enable_strategy() -> dict:
    state = load_state()
    state["enabled"]        = True
    state["killed_reason"]  = None
    state["last_reset_ts"]  = 0
    save_state(state)
    tg(f"✅ *HEPHAESTUS*: Strategy RE-ENABLED by Hermes.")
    return {"enabled":True}

if __name__ == "__main__":
    import sys
    print(json.dumps(run_cycle(), indent=2, default=str))
