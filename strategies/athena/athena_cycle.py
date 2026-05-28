#!/usr/bin/env python3
"""
GENESIS — Athena Cycle (Strategy D: BB+RSI Mean Reversion on M5)
Complements Ares (M1 strict) with a faster, simpler 2-condition entry on M5.

Differences from Ares:
  - Timeframe: M5 (vs Ares M1) — catches intraday mean reversion moves
  - Entry: Pure BB + RSI only (no ADX gate, no MACD requirement)
  - Risk: 0.5% per trade (vs 1%) — more frequent signals, smaller size
  - Context: H4 SMA50 (vs Ares M15) — broader trend filter
  - SL/TP: ATR-based dynamic (vs Ares fixed pips)

run_analysis(symbol) → signal dict. Never places trades directly.
"""
import os, json, time, logging, math
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

CONFIG_PATH = Path(__file__).parent / "athena_config.yaml"
if not CONFIG_PATH.exists():
    CONFIG_PATH = Path(__file__).parents[2] / "configs" / "athena_config.yaml"
with open(CONFIG_PATH) as f:
    CFG = yaml.safe_load(f)

BRIDGE     = os.getenv("ARES_BRIDGE_URL", CFG["bridge"]["url"])
MT5_API    = os.getenv("MT5_API_URL",     "https://api.api2trade.com")
MT5_ID     = os.getenv("MT5_ACCOUNT_ID",  CFG["mt5_api"]["account_id"])
MT5_KEY    = os.getenv("MT5_API_KEY",     CFG["mt5_api"]["api_key"])
MT5_AUTH   = (os.getenv("MT5_API_USER", ""), os.getenv("MT5_API_PASS", ""))
TG_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID = str(CFG["telegram"]["chat_id"])
CACHE_FILE = Path(CFG["cache"]["path"])
# Resolve safe journal path (fallback to local logs/ if system dir not writable)
default_journal = CFG["journal"]["path"]
try:
    Path(default_journal).parent.mkdir(parents=True, exist_ok=True)
    JOURNAL = Path(default_journal)
except Exception:
    local_log_dir = Path(__file__).parents[2] / "logs" / "athena"
    local_log_dir.mkdir(parents=True, exist_ok=True)
    JOURNAL = local_log_dir / "trade_journal.jsonl"


BB_PERIOD    = int(CFG["indicators"]["bb_period"])
BB_DEV       = float(CFG["indicators"]["bb_deviation"])
RSI_PERIOD   = int(CFG["indicators"]["rsi_period"])
RSI_OS       = float(CFG["indicators"]["rsi_oversold"])
RSI_OB       = float(CFG["indicators"]["rsi_overbought"])
SIG_TF       = CFG["indicators"]["signal_timeframe"]
TREND_TF     = CFG["indicators"]["trend_timeframe"]
TREND_MA_PER = int(CFG["indicators"]["trend_ma_period"])
ATR_PERIOD   = int(CFG["indicators"]["atr_period"])

RISK_PCT        = float(CFG["risk"]["risk_pct"])
MIN_RR          = float(CFG["risk"]["min_rr_ratio"])
SL_ATR_MULT     = float(CFG["risk"]["sl_atr_multiplier"])
TP_ATR_MULT     = float(CFG["risk"]["tp_atr_multiplier"])
MAX_SPREAD      = float(CFG["risk"]["max_spread_pips"])
BLOCK_NEWS_MINS = int(CFG["risk"]["block_news_minutes"])

REQUIRE_CLOSE   = bool(CFG["strictness"]["require_band_close"])
REQUIRE_CTX     = bool(CFG["strictness"]["require_h4_context"])
COOLDOWN_SECS   = int(CFG["strictness"]["cooldown_seconds"])
MAX_PER_HOUR    = int(CFG["strictness"]["max_signals_per_hour"])

START_HOUR      = int(CFG["sessions"]["allowed"][0]["start"])
END_HOUR        = int(CFG["sessions"]["allowed"][0]["end"])
MAGIC_COMMENT   = CFG["strategy"]["comment"]

# Resolve safe log path (fallback to local logs/ if system dir not writable)
default_log = "/var/log/athena/athena_cycle.log"
try:
    Path(default_log).parent.mkdir(parents=True, exist_ok=True)
    log_file = default_log
except Exception:
    local_log_dir = Path(__file__).parents[2] / "logs" / "athena"
    local_log_dir.mkdir(parents=True, exist_ok=True)
    log_file = str(local_log_dir / "athena_cycle.log")

logging.basicConfig(
    filename=log_file,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger(__name__)

_last_signal_time: dict = {}
_signals_this_hour: dict = {}

# ── Helpers ────────────────────────────────────────────────────────────────────
def load_cache() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text()) if CACHE_FILE.exists() else {}
    except:
        return {}

def save_cache(c):
    CACHE_FILE.write_text(json.dumps(c))

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
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=10
        )
    except:
        pass

def pip_size(symbol: str) -> float:
    if "JPY" in symbol.upper(): return 0.01
    if "XAU" in symbol.upper(): return 0.1
    return 0.0001

def price_to_pips(diff: float, symbol: str) -> float:
    return abs(diff) / pip_size(symbol)

def calculate_lot(equity: float, sl_pips: float, symbol: str) -> float:
    risk_eur = equity * RISK_PCT
    pip_val  = 10.0
    if "JPY" in symbol.upper(): pip_val = 9.0
    if "GBP" in symbol.upper(): pip_val = 12.5
    if "XAU" in symbol.upper(): pip_val = 1.0
    raw = risk_eur / (sl_pips * pip_val) if sl_pips > 0 else 0.01
    return round(max(0.01, min(round(raw / 0.01) * 0.01, 5.0)), 2)

# ── Market data ────────────────────────────────────────────────────────────────
YF_MAP = {
    "EURUSDxx": "EURUSD=X", "GBPUSDxx": "GBPUSD=X", "USDJPYxx": "USDJPY=X",
    "XAUUSDxx": "GC=F",     "GBPJPYxx": "GBPJPY=X",
}
YF_TF  = {"M1":"1m","M5":"5m","M15":"15m","H1":"1h","H4":"4h","D1":"1d"}

def get_bars(symbol: str, tf: str = "M5", count: int = 120) -> list:
    try:
        import yfinance as yf, pandas as pd
        yf_sym   = YF_MAP.get(symbol, symbol.replace("xx","=X"))
        interval = YF_TF.get(tf, "5m")
        period   = {"1m":"5d","5m":"5d","15m":"5d","1h":"60d","4h":"60d","1d":"365d"}.get(interval,"5d")
        df = yf.download(yf_sym, period=period, interval=interval,
                         progress=False, auto_adjust=True)
        if df.empty: return []
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.columns = [c.lower() for c in df.columns]
        return df.dropna().tail(count).reset_index().to_dict("records")
    except Exception as e:
        log.error(f"get_bars {symbol}/{tf}: {e}")
        return []

# ── Core indicators ────────────────────────────────────────────────────────────
def compute_indicators(bars: list) -> dict:
    """
    Compute BB(20,2), RSI(14), ATR(14), OBV, Stochastic.
    Uses index -2 (last CLOSED candle, not the forming one).
    """
    if len(bars) < BB_PERIOD + 5:
        return {}
    try:
        import pandas as pd, ta

        df = pd.DataFrame(bars)
        df.columns = [c.lower() for c in df.columns]
        for col in ["close","high","low","open"]:
            df[col] = df[col].astype(float)
        if "volume" not in df.columns:
            df["volume"] = 1.0
        df["volume"] = df["volume"].astype(float)

        # Bollinger Bands
        bb_upper = ta.volatility.bollinger_hband(df["close"], window=BB_PERIOD, window_dev=BB_DEV)
        bb_lower = ta.volatility.bollinger_lband(df["close"], window=BB_PERIOD, window_dev=BB_DEV)
        bb_mid   = ta.volatility.bollinger_mavg(df["close"],  window=BB_PERIOD)
        bb_pct   = ta.volatility.bollinger_pband(df["close"], window=BB_PERIOD, window_dev=BB_DEV)
        bb_width = ta.volatility.bollinger_wband(df["close"], window=BB_PERIOD, window_dev=BB_DEV)

        # RSI
        rsi = ta.momentum.rsi(df["close"], window=RSI_PERIOD)

        # Stochastic (additional confirmation)
        stoch_k = ta.momentum.stoch(df["high"], df["low"], df["close"], window=14)
        stoch_d = ta.momentum.stoch_signal(df["high"], df["low"], df["close"], window=14)

        # ATR
        atr = ta.volatility.average_true_range(df["high"], df["low"], df["close"], window=ATR_PERIOD)

        # ADX (soft bonus — not a gate for Athena)
        adx     = ta.trend.adx(df["high"], df["low"], df["close"], window=14)
        adx_pos = ta.trend.adx_pos(df["high"], df["low"], df["close"], window=14)
        adx_neg = ta.trend.adx_neg(df["high"], df["low"], df["close"], window=14)

        # OBV direction (volume confirmation)
        obv = ta.volume.on_balance_volume(df["close"], df["volume"])

        # MACD (soft)
        macd_hist = ta.trend.macd_diff(df["close"])

        def safe(s, i=-2):
            try:
                v = float(s.iloc[i])
                return None if math.isnan(v) else round(v, 6)
            except:
                return None

        # OBV trend: is OBV rising or falling over last 3 candles?
        obv_now  = safe(obv, -2)
        obv_prev = safe(obv, -5)
        obv_rising = (obv_now or 0) > (obv_prev or 0)

        return {
            "bb_upper":   safe(bb_upper),
            "bb_lower":   safe(bb_lower),
            "bb_middle":  safe(bb_mid),
            "bb_pct":     safe(bb_pct),
            "bb_width":   safe(bb_width),
            "rsi":        safe(rsi),
            "stoch_k":    safe(stoch_k),
            "stoch_d":    safe(stoch_d),
            "atr":        safe(atr),
            "adx":        safe(adx),
            "adx_plus":   safe(adx_pos),
            "adx_minus":  safe(adx_neg),
            "obv_rising": obv_rising,
            "macd_hist":  safe(macd_hist),
            "close":      round(float(df["close"].iloc[-2]), 6),
            "high":       round(float(df["high"].iloc[-2]), 6),
            "low":        round(float(df["low"].iloc[-2]), 6),
        }
    except Exception as e:
        log.error(f"compute_indicators: {e}")
        return {}

def get_h4_context(symbol: str) -> float | None:
    """H4 SMA50 for broad trend direction."""
    cache = load_cache()
    key   = f"athena_h4sma_{symbol}"
    now   = time.time()
    if key in cache and now - cache[key].get("ts", 0) < 900:  # 15min cache
        return cache[key].get("val")
    try:
        import pandas as pd, ta
        bars = get_bars(symbol, "H4", TREND_MA_PER + 10)
        if len(bars) < TREND_MA_PER: return None
        df = pd.DataFrame(bars)
        df.columns = [c.lower() for c in df.columns]
        df["close"] = df["close"].astype(float)
        sma = ta.trend.sma_indicator(df["close"], window=TREND_MA_PER)
        val = round(float(sma.iloc[-2]), 6)
        cache[key] = {"ts": now, "val": val}
        save_cache(cache)
        return val
    except Exception as e:
        log.error(f"get_h4_context {symbol}: {e}")
        return None

def check_news_block(symbol: str) -> tuple[bool, list]:
    now_utc  = datetime.now(timezone.utc)
    warnings = []
    blocked  = set()
    cache    = load_cache()
    for evt in cache.get("ff_cal", {}).get("data", []):
        try:
            et   = datetime.fromisoformat(evt.get("date","")).astimezone(timezone.utc)
            mins = (et - now_utc).total_seconds() / 60
            if evt.get("impact") == "High" and -15 < mins < BLOCK_NEWS_MINS:
                blocked.add(evt.get("currency","")[:3])
                warnings.append(f"High: {evt.get('title')} in {int(mins)}min")
        except:
            pass
    sym_up   = symbol.upper()
    is_block = any(c and c in sym_up for c in blocked if c)
    return is_block, warnings

def is_trade_time() -> bool:
    now = datetime.now(timezone.utc)
    wd, hr = now.weekday(), now.hour
    if (wd == 4 and hr >= 22) or wd == 5 or (wd == 6 and hr < 22):
        return False
    return START_HOUR <= hr < END_HOUR

def check_rate_limit(symbol: str) -> tuple[bool, str]:
    """Check cooldown + hourly rate limit."""
    now = time.time()
    # Per-symbol cooldown
    if now - _last_signal_time.get(symbol, 0) < COOLDOWN_SECS:
        remaining = int(COOLDOWN_SECS - (now - _last_signal_time.get(symbol, 0)))
        return False, f"Cooldown: {remaining}s remaining for {symbol}"
    # Hourly rate limit (across all symbols)
    hour_key = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")
    count    = _signals_this_hour.get(hour_key, 0)
    if count >= MAX_PER_HOUR:
        return False, f"Rate limit: {count}/{MAX_PER_HOUR} signals this hour"
    return True, ""

def has_athena_position() -> bool:
    pos = bridge("/positions")
    if isinstance(pos, list):
        for p in pos:
            if "ATHENA" in str(p.get("comment","")).upper():
                return True
    return False

# ── Main analysis ──────────────────────────────────────────────────────────────
def run_analysis(symbol: str) -> dict:
    """
    Full Athena BB+RSI mean reversion analysis on M5.
    Returns signal dict. Never executes — athena_tool.py handles that.
    """
    symbol = symbol.upper()
    if not symbol.endswith("XX"):
        symbol = symbol + "xx"
    symbol = symbol[:-2] + "xx"

    log.info(f"=== Athena BB+RSI M5 Analysis: {symbol} ===")

    # ── Account ─────────────────────────────────────────────────────
    account = bridge("/balance")
    if "error" in account:
        return {"action":"wait","reason":f"Bridge unreachable: {account['error']}"}
    equity = float(account.get("equity", 0))
    if equity <= 0:
        return {"action":"wait","reason":"Account equity unavailable."}

    # ── Existing Athena position ─────────────────────────────────────
    if has_athena_position():
        return {"action":"wait","reason":"Athena position already open."}

    # ── Rate limits ──────────────────────────────────────────────────
    ok, reason = check_rate_limit(symbol)
    if not ok:
        return {"action":"wait","reason":reason}

    # ── Session ──────────────────────────────────────────────────────
    if not is_trade_time():
        return {"action":"wait","reason":f"Outside session (GMT {START_HOUR}–{END_HOUR})."}

    # ── Quote + spread ───────────────────────────────────────────────
    quote = bridge(f"/quote?symbol={symbol}")
    if "error" in quote or not quote.get("bid"):
        return {"action":"wait","reason":f"No live quote for {symbol}."}
    bid          = float(quote["bid"])
    ask          = float(quote["ask"])
    spread_pips  = price_to_pips(ask - bid, symbol)
    if spread_pips > MAX_SPREAD:
        return {"action":"wait","reason":f"Spread {spread_pips:.2f} > max {MAX_SPREAD} pips."}

    # ── News block ───────────────────────────────────────────────────
    blocked, news_warn = check_news_block(symbol)
    if blocked:
        return {"action":"wait","reason":f"News block: {'; '.join(news_warn[:2])}"}

    # ── M5 indicators ────────────────────────────────────────────────
    bars = get_bars(symbol, SIG_TF, BB_PERIOD + 30)
    if len(bars) < BB_PERIOD + 5:
        return {"action":"wait","reason":"Insufficient M5 bar data."}

    ind = compute_indicators(bars)
    if not ind:
        return {"action":"wait","reason":"Indicator computation failed."}

    close    = ind["close"]
    bb_upper = ind["bb_upper"]
    bb_lower = ind["bb_lower"]
    rsi      = ind["rsi"]
    atr      = ind.get("atr")
    adx      = ind.get("adx")
    stoch_k  = ind.get("stoch_k")
    macd_hist= ind.get("macd_hist")
    obv_up   = ind.get("obv_rising", True)

    if None in (close, bb_upper, bb_lower, rsi):
        return {"action":"wait","reason":"Key indicator values are None."}

    # ── H4 context ───────────────────────────────────────────────────
    h4_sma      = get_h4_context(symbol)
    ctx_long    = True
    ctx_short   = True
    ctx_note    = "H4 filter skipped"
    if h4_sma is not None and REQUIRE_CTX:
        ctx_long  = close > h4_sma * 0.9995   # Allow slight dip below H4 SMA
        ctx_short = close < h4_sma * 1.0005
        ctx_note  = f"H4 SMA{TREND_MA_PER}={h4_sma:.5f}"

    # ── Signal detection (2 hard conditions + soft bonuses) ──────────
    buy_hard  = (close < bb_lower) and (rsi < RSI_OS)
    sell_hard = (close > bb_upper) and (rsi > RSI_OB)

    if not buy_hard and not sell_hard:
        return {
            "action": "wait",
            "reason": (
                f"No signal. Close={close:.5f} BB=[{bb_lower:.5f},{bb_upper:.5f}] "
                f"RSI={rsi:.1f}"
            )
        }

    direction = "Buy" if buy_hard else "Sell"

    # ── Soft bonus conditions (don't block, but affect confidence) ────
    if direction == "Buy":
        conds = [
            (close < bb_lower,               f"Price closed below lower BB ({close:.5f} < {bb_lower:.5f})"),
            (rsi < RSI_OS,                   f"RSI oversold ({rsi:.1f} < {RSI_OS})"),
            (ctx_long,                       f"H4 context OK — {ctx_note}"),
            (stoch_k is not None and stoch_k < 25, f"Stochastic K oversold ({stoch_k:.1f})"),
            (obv_up,                         f"OBV rising (volume supports buy)"),
            (adx is not None and adx < 30,   f"ADX={adx:.1f} (ranging market — ideal for reversion)"),
            (macd_hist is not None and macd_hist > -0.00005, f"MACD hist not strongly bearish"),
        ]
        entry = ask
        if atr and atr > 0:
            sl = round(entry - atr * SL_ATR_MULT, 6)
            tp = round(entry + atr * TP_ATR_MULT, 6)
        else:
            sl = round(entry - 15 * pip_size(symbol), 6)
            tp = round(entry + 30 * pip_size(symbol), 6)
    else:
        conds = [
            (close > bb_upper,               f"Price closed above upper BB ({close:.5f} > {bb_upper:.5f})"),
            (rsi > RSI_OB,                   f"RSI overbought ({rsi:.1f} > {RSI_OB})"),
            (ctx_short,                      f"H4 context OK — {ctx_note}"),
            (stoch_k is not None and stoch_k > 75, f"Stochastic K overbought ({stoch_k:.1f})"),
            (not obv_up,                     f"OBV falling (volume supports sell)"),
            (adx is not None and adx < 30,   f"ADX={adx:.1f} (ranging market — ideal for reversion)"),
            (macd_hist is not None and macd_hist < 0.00005, f"MACD hist not strongly bullish"),
        ]
        entry = bid
        if atr and atr > 0:
            sl = round(entry + atr * SL_ATR_MULT, 6)
            tp = round(entry - atr * TP_ATR_MULT, 6)
        else:
            sl = round(entry + 15 * pip_size(symbol), 6)
            tp = round(entry - 30 * pip_size(symbol), 6)

    passed = [(m, d) for m, d in conds if m]
    failed = [(m, d) for m, d in conds if not m]

    # Athena only requires 2 hard conditions (already confirmed above)
    # Confidence based on how many soft bonuses also fired
    n_passed = len(passed)
    confidence = "high" if n_passed >= 5 else ("medium" if n_passed >= 3 else "low")

    # ── R:R gate ─────────────────────────────────────────────────────
    sl_pips = price_to_pips(entry - sl, symbol)
    tp_pips = price_to_pips(tp - entry, symbol)
    rr      = round(tp_pips / sl_pips, 2) if sl_pips > 0 else 0

    if rr < MIN_RR:
        return {"action":"wait","reason":f"R:R {rr} below minimum {MIN_RR}."}

    volume = calculate_lot(equity, sl_pips, symbol)

    # Update rate-limit state
    _last_signal_time[symbol] = time.time()
    hour_key = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")
    _signals_this_hour[hour_key] = _signals_this_hour.get(hour_key, 0) + 1

    reason = (
        f"Athena BB+RSI M5: Close={'below' if direction=='Buy' else 'above'} "
        f"{'lower' if direction=='Buy' else 'upper'} BB, RSI={rsi:.1f}. "
        f"{n_passed}/7 conditions. ATR={atr:.5f}, R:R={rr}."
    )
    log.info(f"SIGNAL: {direction} {symbol} | SL={sl} TP={tp} Vol={volume} RR={rr}")

    return {
        "action":            "trade",
        "strategy":          "athena-bb-rsi-m5",
        "signal_type":       "BB_LOWER_TOUCH" if direction=="Buy" else "BB_UPPER_TOUCH",
        "symbol":            symbol,
        "direction":         direction,
        "entry":             entry,
        "stop_loss":         sl,
        "take_profit":       tp,
        "volume":            volume,
        "rr_ratio":          rr,
        "sl_pips":           round(sl_pips, 1),
        "tp_pips":           round(tp_pips, 1),
        "confidence":        confidence,
        "conditions_met":    [d for _, d in passed],
        "conditions_failed": [d for _, d in failed],
        "warnings":          news_warn,
        "reason":            reason,
        "indicators": {
            "close":    close, "bb_upper": bb_upper, "bb_lower": bb_lower,
            "bb_middle": ind.get("bb_middle"), "bb_width": ind.get("bb_width"),
            "rsi":      rsi,  "stoch_k": stoch_k, "adx": adx,
            "atr":      atr,  "h4_sma50": h4_sma, "spread_pips": spread_pips,
        },
        "signal_schema": {   # Matches the SignalMessage spec from the prompt
            "strategy_id":  "ATHENA-v1",
            "magic_number": CFG["strategy"]["magic_number"],
            "risk_percent": RISK_PCT,
            "metadata": {
                "bb_lower": bb_lower, "bb_middle": ind.get("bb_middle"),
                "bb_upper": bb_upper, "rsi": rsi,
            }
        },
        "analysed_at": datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    import sys
    sym = sys.argv[1] if len(sys.argv) > 1 else "EURUSDxx"
    print(f"Running Athena analysis for {sym}...")
    result = run_analysis(sym)
    print(json.dumps(result, indent=2, default=str))
