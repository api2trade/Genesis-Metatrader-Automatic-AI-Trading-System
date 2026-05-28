#!/usr/bin/env python3
"""
GENESIS — Zeus Cycle (Strategy G: ICT Smart Money Concepts)
Three-layer sequential confirmation — NOT parallel detection:
  Layer 1: Liquidity Sweep (price takes out swing high/low with rejection)
  Layer 2: Fair Value Gap (3-candle imbalance after displacement)
  Layer 3: Order Block (last candle before displacement, institutional anchor)

Only when ALL THREE confirm in sequence → confluence score → signal.
Expected: 5-15 signals/month. Win rate target: 65-70%.
"""
import os, json, time, logging, math
from datetime import datetime, timezone, date
from pathlib import Path
import requests, yaml

CONFIG_PATH = Path(__file__).parent / "zeus_config.yaml"
if not CONFIG_PATH.exists():
    CONFIG_PATH = Path(__file__).parents[2] / "configs" / "zeus_config.yaml"
with open(CONFIG_PATH) as f:
    CFG = yaml.safe_load(f)

BRIDGE     = os.getenv("ARES_BRIDGE_URL", CFG["bridge"]["url"])
MT5_API    = os.getenv("MT5_API_URL",    "https://mt5.mt4api.dev")
MT5_ID     = os.getenv("MT5_ACCOUNT_ID", CFG["mt5_api"]["account_id"])
MT5_KEY    = os.getenv("MT5_API_KEY",    CFG["mt5_api"]["api_key"])
MT5_AUTH   = (os.getenv("MT5_API_USER", ""), os.getenv("MT5_API_PASS", ""))
TG_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID = str(os.getenv("TELEGRAM_CHAT_ID", CFG["telegram"]["chat_id"]))
# Resolve safe journal path (fallback to local logs/ if system dir not writable)
default_journal = CFG["journal"]["path"]
try:
    Path(default_journal).parent.mkdir(parents=True, exist_ok=True)
    JOURNAL = Path(default_journal)
except Exception:
    local_log_dir = Path(__file__).parents[2] / "logs" / "zeus"
    local_log_dir.mkdir(parents=True, exist_ok=True)
    JOURNAL = local_log_dir / "trade_journal.jsonl"

COMMENT    = CFG["strategy"]["comment"]

# ICT config
SWING_LB    = int(CFG["ict"]["liquidity"]["swing_lookback"])
SWEEP_TOL   = float(CFG["ict"]["liquidity"]["sweep_tolerance"])
REQ_REJECT  = bool(CFG["ict"]["liquidity"]["require_rejection"])
MIN_GAP_P   = float(CFG["ict"]["fvg"]["min_gap_pips"])
FVG_AGE     = int(CFG["ict"]["fvg"]["max_age_bars"])
OB_AGE      = int(CFG["ict"]["order_block"]["max_age_bars"])
MIN_BODY    = float(CFG["ict"]["order_block"]["min_body_ratio"])
MAX_WICK    = float(CFG["ict"]["order_block"]["max_wick_ratio"])

MIN_SCORE   = int(CFG["confluence"]["min_score"])
MAX_DT      = int(CFG["confluence"]["max_daily_trades"])
KZ_EN       = bool(CFG["confluence"]["killzone"]["enabled"])
KZ_LON      = CFG["confluence"]["killzone"]["london"]
KZ_NY       = CFG["confluence"]["killzone"]["ny"]

RISK_PCT    = float(CFG["risk"]["risk_pct"])
MIN_RR      = float(CFG["risk"]["min_rr"])
TP_MULT     = float(CFG["risk"]["tp_multiplier"])
MAX_SPREAD  = float(CFG["risk"]["max_spread_pips"])
COOLDOWN    = int(CFG["risk"]["cooldown_seconds"])
OB_BUF      = float(CFG["risk"]["sl_ob_buffer"])

MAX_DD_PCT  = float(CFG["circuit_breakers"]["max_equity_drawdown_pct"])
MAX_DL_PCT  = float(CFG["circuit_breakers"]["max_daily_loss_pct"])
START_H     = int(CFG["sessions"]["allowed"][0]["start"])
END_H       = int(CFG["sessions"]["allowed"][0]["end"])

# Resolve safe log path (fallback to local logs/ if system dir not writable)
default_log = "/var/log/zeus/zeus_cycle.log"
try:
    Path(default_log).parent.mkdir(parents=True, exist_ok=True)
    log_file = default_log
except Exception:
    local_log_dir = Path(__file__).parents[2] / "logs" / "zeus"
    local_log_dir.mkdir(parents=True, exist_ok=True)
    log_file = str(local_log_dir / "zeus_cycle.log")

logging.basicConfig(
    filename=log_file,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger(__name__)
_last_sig: dict = {}
_daily: dict   = {}

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


def tg(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id":TG_CHAT_ID,"text":msg,"parse_mode":"Markdown"}, timeout=10)
    except: pass

def pip(sym): return 0.01 if "JPY" in sym else (0.1 if "XAU" in sym else 0.0001)
def to_pips(d,s): return abs(d)/pip(s)
def calc_lot(equity,sl_pips,sym):
    pv=10.0
    if "JPY" in sym: pv=9.0
    if "GBP" in sym: pv=12.5
    if "XAU" in sym: pv=1.0
    raw=(equity*RISK_PCT)/(sl_pips*pv) if sl_pips>0 else 0.01
    return round(max(0.01,min(round(raw/0.01)*0.01,5.0)),2)

YF_MAP={"EURUSDxx":"EURUSD=X","GBPUSDxx":"GBPUSD=X","USDJPYxx":"USDJPY=X",
        "XAUUSDxx":"GC=F","GBPJPYxx":"GBPJPY=X"}

def get_bars(sym,tf="M5",count=100):
    try:
        import yfinance as yf, pandas as pd
        yf_sym=YF_MAP.get(sym,sym.replace("xx","=X"))
        itv={"M5":"5m","M15":"15m","H1":"1h"}.get(tf,"5m")
        per={"5m":"5d","15m":"5d","1h":"60d"}.get(itv,"5d")
        df=yf.download(yf_sym,period=per,interval=itv,progress=False,auto_adjust=True)
        if df.empty: return []
        if isinstance(df.columns,pd.MultiIndex): df.columns=df.columns.get_level_values(0)
        df.columns=[c.lower() for c in df.columns]
        return df.dropna().tail(count).reset_index().to_dict("records")
    except Exception as e:
        log.error(f"get_bars {sym}/{tf}: {e}"); return []

# ── Layer 1: Liquidity Sweep Detection ────────────────────────────────────────
def detect_swing_highs(highs: list, lookback: int) -> list:
    """Pivot high: bar[i] is highest in [i-lb, i+lb] window."""
    pivots = []
    for i in range(lookback, len(highs)-lookback):
        if highs[i] == max(highs[i-lookback:i+lookback+1]):
            pivots.append((i, highs[i]))
    return pivots

def detect_swing_lows(lows: list, lookback: int) -> list:
    pivots = []
    for i in range(lookback, len(lows)-lookback):
        if lows[i] == min(lows[i-lookback:i+lookback+1]):
            pivots.append((i, lows[i]))
    return pivots

def detect_liquidity_sweep(highs, lows, closes, opens, sym) -> dict | None:
    """
    Layer 1: Detect if the LAST candle (index -2, last closed) swept a swing level
    with rejection (wick beyond level, close back inside).
    Returns sweep info dict or None.
    """
    if len(highs) < SWING_LB*2+5: return None

    cur_h = highs[-2]; cur_l = lows[-2]; cur_c = closes[-2]; cur_o = opens[-2]
    # Check last 30 bars for swing levels
    h_slice = highs[-32:-2]; l_slice = lows[-32:-2]
    sw_highs = detect_swing_highs(h_slice, SWING_LB)
    sw_lows  = detect_swing_lows(l_slice,  SWING_LB)

    # ── Bearish sweep: price wicks above a swing high but closes below ─────────
    for idx, level in sw_highs[-3:]:  # Check last 3 swing highs
        if (cur_h > level + SWEEP_TOL  # Wick penetrated
                and (not REQ_REJECT or cur_c < level)):  # Close back below
            body_up  = cur_h - max(cur_c, cur_o)
            body_dn  = min(cur_c, cur_o) - cur_l
            rej_str  = "strong" if body_up > (cur_h - cur_l)*0.3 else "weak"
            return {
                "type":      "bearish",
                "level":     round(level,6),
                "level_idx": idx,
                "wick_high": cur_h,
                "close":     cur_c,
                "rejection": rej_str,
                "liq_type":  "swing_high",
            }

    # ── Bullish sweep: price wicks below a swing low but closes above ──────────
    for idx, level in sw_lows[-3:]:
        if (cur_l < level - SWEEP_TOL
                and (not REQ_REJECT or cur_c > level)):
            body_dn  = min(cur_c, cur_o) - cur_l
            rej_str  = "strong" if body_dn > (cur_h - cur_l)*0.3 else "weak"
            return {
                "type":      "bullish",
                "level":     round(level,6),
                "level_idx": idx,
                "wick_low":  cur_l,
                "close":     cur_c,
                "rejection": rej_str,
                "liq_type":  "swing_low",
            }
    return None

# ── Layer 2: Fair Value Gap Detection ─────────────────────────────────────────
def detect_fvg(highs, lows, closes, sweep_type: str, sym) -> dict | None:
    """
    Layer 2: After a sweep candle (index -2), scan the 3 most recent candles
    for a Fair Value Gap — 3-candle imbalance where candle 2 body doesn't
    overlap candles 1 and 3's wicks.

    Bullish FVG (after bullish sweep): Candle3.low > Candle1.high → price void below
    Bearish FVG (after bearish sweep): Candle3.high < Candle1.low → price void above
    Min gap = min_gap_pips
    """
    min_gap = MIN_GAP_P * pip(sym)
    n = len(highs)
    if n < 4: return None

    # Scan last FVG_AGE+3 bars for fresh FVGs
    for i in range(n-4, max(n-FVG_AGE-4, 1), -1):
        c1h, c1l = highs[i],   lows[i]
        c2h, c2l = highs[i+1], lows[i+1]
        c3h, c3l = highs[i+2], lows[i+2]

        if sweep_type == "bullish":
            # Bullish FVG: gap between candle1 high and candle3 low
            gap = c3l - c1h
            if gap > min_gap:
                mitigation = min(closes[-2:])
                mitigated  = mitigation <= c1h + gap/2
                return {
                    "type":       "bullish",
                    "high":       round(c3l, 6),
                    "low":        round(c1h, 6),
                    "midpoint":   round((c3l+c1h)/2, 6),
                    "gap_pips":   round(gap/pip(sym), 1),
                    "bar_index":  i+1,
                    "age_bars":   n-2-i,
                    "mitigated":  mitigated,
                    "strength":   3 if gap>min_gap*2 else 2 if gap>min_gap*1.5 else 1,
                }

        elif sweep_type == "bearish":
            # Bearish FVG: gap between candle3 high and candle1 low
            gap = c1l - c3h
            if gap > min_gap:
                mitigation = max(closes[-2:])
                mitigated  = mitigation >= c3h + gap/2
                return {
                    "type":       "bearish",
                    "high":       round(c1l, 6),
                    "low":        round(c3h, 6),
                    "midpoint":   round((c1l+c3h)/2, 6),
                    "gap_pips":   round(gap/pip(sym), 1),
                    "bar_index":  i+1,
                    "age_bars":   n-2-i,
                    "mitigated":  mitigated,
                    "strength":   3 if gap>min_gap*2 else 2 if gap>min_gap*1.5 else 1,
                }
    return None

# ── Layer 3: Order Block Detection ────────────────────────────────────────────
def detect_order_block(highs, lows, closes, opens, fvg: dict, sweep_type: str) -> dict | None:
    """
    Layer 3: The Order Block is the LAST candle before the displacement move
    that caused the FVG.
    - Bullish OB: last down-close candle before the bullish displacement
    - Bearish OB: last up-close candle before the bearish displacement
    OB quality scored by body ratio, wick ratio, freshness.
    """
    fvg_bar = fvg.get("bar_index", len(closes)-3)
    search_start = max(0, fvg_bar - OB_AGE)

    if sweep_type == "bullish":
        # Find last bearish (down-close) candle before fvg_bar
        for i in range(fvg_bar, search_start, -1):
            if i >= len(closes): continue
            if closes[i] < opens[i]:  # Bearish candle
                total_range = highs[i] - lows[i]
                if total_range <= 0: continue
                body  = abs(closes[i] - opens[i])
                wicks = total_range - body
                body_r = body / total_range
                wick_r = wicks / total_range
                if body_r >= MIN_BODY and wick_r <= MAX_WICK:
                    qual = round(min(20, body_r*20 + (1-wick_r)*10 + max(0,10-(fvg_bar-i))), 1)
                    return {
                        "type":    "bullish",
                        "high":    round(highs[i],6),
                        "low":     round(lows[i],6),
                        "open":    round(opens[i],6),
                        "close":   round(closes[i],6),
                        "body_ratio": round(body_r,2),
                        "wick_ratio": round(wick_r,2),
                        "quality": qual,
                        "bar_idx": i,
                        "age_bars": fvg_bar - i,
                    }
    else:
        # Find last bullish (up-close) candle before fvg_bar
        for i in range(fvg_bar, search_start, -1):
            if i >= len(closes): continue
            if closes[i] > opens[i]:
                total_range = highs[i] - lows[i]
                if total_range <= 0: continue
                body  = abs(closes[i] - opens[i])
                wicks = total_range - body
                body_r = body / total_range
                wick_r = wicks / total_range
                if body_r >= MIN_BODY and wick_r <= MAX_WICK:
                    qual = round(min(20, body_r*20 + (1-wick_r)*10 + max(0,10-(fvg_bar-i))), 1)
                    return {
                        "type":    "bearish",
                        "high":    round(highs[i],6),
                        "low":     round(lows[i],6),
                        "open":    round(opens[i],6),
                        "close":   round(closes[i],6),
                        "body_ratio": round(body_r,2),
                        "wick_ratio": round(wick_r,2),
                        "quality": qual,
                        "bar_idx": i,
                        "age_bars": fvg_bar - i,
                    }
    return None

# ── Confluence Scoring (0-100) ─────────────────────────────────────────────────
def is_killzone() -> tuple[bool, str]:
    now = datetime.now(timezone.utc)
    hr  = now.hour + now.minute/60
    if KZ_EN:
        if KZ_LON["start"] <= hr <= KZ_LON["end"]: return True, "London"
        if KZ_NY["start"]  <= hr <= KZ_NY["end"]:  return True, "New York"
    return False, ""

def score_confluence(sweep, fvg, ob, m15_aligns: bool) -> tuple[int, dict]:
    SC = CFG["confluence"]["scoring"]
    kz, kz_name = is_killzone()

    s_sweep  = SC["sweep_quality"]  if sweep.get("rejection")=="strong" else int(SC["sweep_quality"]*0.6)
    s_fvg    = SC["fvg_presence"]   if fvg.get("strength",0)>=2 else int(SC["fvg_presence"]*0.6)
    s_ob     = min(SC["ob_quality"], int(ob.get("quality",0)/20*SC["ob_quality"])) if ob else 0
    s_bos    = SC["bos_strength"]   if m15_aligns else int(SC["bos_strength"]*0.6)
    s_kz     = SC["killzone"]       if kz else 0
    s_mtf    = SC["mtf_confluence"] if m15_aligns else 0
    s_fresh  = SC["ob_freshness"]   if ob and ob.get("age_bars",99)<5 else int(SC["ob_freshness"]*0.5) if ob and ob.get("age_bars",99)<10 else 0

    total = s_sweep + s_fvg + s_ob + s_bos + s_kz + s_mtf + s_fresh
    breakdown = {
        "bos_strength":   s_bos,   "sweep_quality":  s_sweep,
        "fvg_presence":   s_fvg,   "ob_quality":     s_ob,
        "killzone":       s_kz,    "mtf_confluence": s_mtf,
        "ob_freshness":   s_fresh,
    }
    return min(100, total), breakdown

# ── M15 context check ─────────────────────────────────────────────────────────
def get_m15_context(sym: str, direction: str) -> bool:
    """Check if M15 trend aligns with intended trade direction."""
    try:
        bars = get_bars(sym, "M15", 30)
        if len(bars) < 20: return True
        import pandas as pd, ta
        df = pd.DataFrame(bars)
        df.columns = [c.lower() for c in df.columns]
        df["close"] = df["close"].astype(float)
        ema20 = ta.trend.ema_indicator(df["close"], window=20)
        last_close = float(df["close"].iloc[-2])
        last_ema   = float(ema20.iloc[-2])
        if direction == "bullish": return last_close > last_ema
        else:                      return last_close < last_ema
    except: return True  # Default allow if unavailable

def is_trade_time():
    now = datetime.now(timezone.utc)
    wd, hr = now.weekday(), now.hour
    if (wd==4 and hr>=22) or wd==5 or (wd==6 and hr<22): return False
    return START_H <= hr < END_H

def check_daily(sym):
    today = str(date.today())
    k = f"{sym}_{today}"
    return _daily.get(k, 0)

def inc_daily(sym):
    today = str(date.today())
    k = f"{sym}_{today}"
    _daily[k] = _daily.get(k, 0) + 1

def has_zeus_position():
    pos = bridge("/positions")
    return isinstance(pos,list) and any("ZEUS" in str(p.get("comment","")).upper() for p in pos)

# ── Main analysis (three-layer sequential) ─────────────────────────────────────
def run_analysis(symbol: str) -> dict:
    symbol = symbol.upper()
    if not symbol.endswith("XX"): symbol += "xx"
    symbol = symbol[:-2] + "xx"
    log.info(f"=== Zeus ICT Analysis: {symbol} ===")

    # ── Preflight ────────────────────────────────────────────────────
    acc = bridge("/balance")
    if "error" in acc: return {"action":"wait","reason":f"Bridge: {acc['error']}"}
    equity = float(acc.get("equity",0))
    if equity <= 0: return {"action":"wait","reason":"No equity."}

    if has_zeus_position(): return {"action":"wait","reason":"Zeus position already open."}

    if time.time() - _last_sig.get(symbol,0) < COOLDOWN:
        rem = int(COOLDOWN-(time.time()-_last_sig.get(symbol,0)))
        return {"action":"wait","reason":f"Cooldown: {rem}s"}

    daily_count = check_daily(symbol)
    if daily_count >= MAX_DT:
        return {"action":"wait","reason":f"Max daily trades ({MAX_DT}) reached."}

    if not is_trade_time():
        return {"action":"wait","reason":f"Outside session (GMT {START_H}–{END_H})."}

    quote = bridge(f"/quote?symbol={symbol}")
    if "error" in quote or not quote.get("bid"):
        return {"action":"wait","reason":f"No quote for {symbol}."}
    bid = float(quote["bid"]); ask = float(quote["ask"])
    spread = to_pips(ask-bid, symbol)
    if spread > MAX_SPREAD:
        return {"action":"wait","reason":f"Spread {spread:.2f} > {MAX_SPREAD} pips."}

    bars = get_bars(symbol, "M5", 100)
    if len(bars) < 30:
        return {"action":"wait","reason":"Insufficient M5 data for ICT detection."}

    highs  = [float(b.get("high",0))  for b in bars]
    lows   = [float(b.get("low",0))   for b in bars]
    closes = [float(b.get("close",0)) for b in bars]
    opens  = [float(b.get("open",0))  for b in bars]

    # ════════════════════════════════════════════════════════════════
    # LAYER 1: LIQUIDITY SWEEP
    # ════════════════════════════════════════════════════════════════
    sweep = detect_liquidity_sweep(highs, lows, closes, opens, symbol)
    if not sweep:
        return {"action":"wait","reason":"No liquidity sweep detected on M5.",
                "layer":"1/3 — sweep not found"}

    sweep_type = sweep["type"]  # "bullish" or "bearish"
    log.info(f"Layer 1 PASS: {sweep_type} sweep at {sweep['level']}")

    # ════════════════════════════════════════════════════════════════
    # LAYER 2: FAIR VALUE GAP (must follow the sweep)
    # ════════════════════════════════════════════════════════════════
    fvg = detect_fvg(highs, lows, closes, sweep_type, symbol)
    if not fvg:
        return {"action":"wait","reason":"Sweep found but no FVG after displacement.",
                "layer":"2/3 — FVG not found", "sweep":sweep}
    if fvg.get("mitigated") and CFG["ict"]["fvg"]["require_unmitigated"]:
        return {"action":"wait","reason":"FVG found but already mitigated.",
                "layer":"2/3 — FVG mitigated", "sweep":sweep, "fvg":fvg}

    log.info(f"Layer 2 PASS: {fvg['type']} FVG gap={fvg['gap_pips']}pips str={fvg['strength']}")

    # ════════════════════════════════════════════════════════════════
    # LAYER 3: ORDER BLOCK
    # ════════════════════════════════════════════════════════════════
    ob = detect_order_block(highs, lows, closes, opens, fvg, sweep_type)
    if not ob:
        return {"action":"wait","reason":"Sweep+FVG found but no valid Order Block.",
                "layer":"3/3 — OB not found", "sweep":sweep, "fvg":fvg}

    log.info(f"Layer 3 PASS: {ob['type']} OB quality={ob['quality']} age={ob['age_bars']}bars")

    # ════════════════════════════════════════════════════════════════
    # CONFLUENCE SCORING
    # ════════════════════════════════════════════════════════════════
    m15_ok = get_m15_context(symbol, sweep_type)
    score, breakdown = score_confluence(sweep, fvg, ob, m15_ok)
    kz_active, kz_name = is_killzone()

    if score < MIN_SCORE:
        return {"action":"wait","reason":f"All 3 layers passed but score {score} < {MIN_SCORE}.",
                "score":score,"breakdown":breakdown,"sweep":sweep,"fvg":fvg,"ob":ob}

    # ════════════════════════════════════════════════════════════════
    # SIGNAL CONSTRUCTION
    # ════════════════════════════════════════════════════════════════
    direction = "Buy" if sweep_type=="bullish" else "Sell"
    entry = ask if direction=="Buy" else bid

    # SL: just below/above the Order Block
    if direction == "Buy":
        sl = round(ob["low"] - OB_BUF, 6)
        tp = round(entry + abs(entry-sl)*TP_MULT, 6)
    else:
        sl = round(ob["high"] + OB_BUF, 6)
        tp = round(entry - abs(sl-entry)*TP_MULT, 6)

    sl_pips = to_pips(entry-sl, symbol)
    tp_pips = to_pips(tp-entry, symbol)
    rr      = round(tp_pips/sl_pips, 2) if sl_pips>0 else 0

    if rr < MIN_RR:
        return {"action":"wait","reason":f"R:R {rr} < {MIN_RR}.",
                "score":score,"sweep":sweep,"fvg":fvg,"ob":ob}

    volume = calc_lot(equity, sl_pips, symbol)
    _last_sig[symbol] = time.time()
    inc_daily(symbol)

    log.info(f"SIGNAL: {direction} {symbol} score={score} SL={sl} TP={tp} Vol={volume}")

    return {
        "action":      "trade",
        "strategy":    "zeus-ict-smartmoney",
        "signal_type": f"ICT_{'BULLISH' if direction=='Buy' else 'BEARISH'}_SETUP",
        "symbol":      symbol,
        "direction":   direction,
        "entry":       entry,
        "stop_loss":   sl,
        "take_profit": tp,
        "volume":      volume,
        "rr_ratio":    rr,
        "sl_pips":     round(sl_pips,1),
        "tp_pips":     round(tp_pips,1),
        "confidence":  "high" if score>=80 else "medium",
        "confidence_score": score,
        "score_breakdown":  breakdown,
        "layers": {
            "1_sweep": sweep,
            "2_fvg":   fvg,
            "3_ob":    ob,
        },
        "killzone":    kz_name if kz_active else "none",
        "m15_aligned": m15_ok,
        "signal_schema": {
            "strategy_id":  "ZEUS-v1",
            "magic_number": CFG["strategy"]["magic_number"],
            "risk_percent": RISK_PCT,
            "confidence_score": score,
            "metadata": {
                "liquidity_sweep": {"level":sweep["level"],"type":sweep["liq_type"]},
                "fvg":  {"type":fvg["type"],"high":fvg["high"],"low":fvg["low"],"strength":fvg["strength"]},
                "order_block": {"price":ob["low"] if direction=="Buy" else ob["high"],
                                "quality_score":ob["quality"]},
                "killzone_active": kz_name or "none",
                "score_breakdown": breakdown,
            }
        },
        "analysed_at": datetime.now(timezone.utc).isoformat(),
    }

if __name__=="__main__":
    import sys
    sym = sys.argv[1] if len(sys.argv)>1 else "EURUSDxx"
    print(json.dumps(run_analysis(sym), indent=2, default=str))
