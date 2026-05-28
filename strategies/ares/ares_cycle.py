#!/usr/bin/env python3
"""
GENESIS — Ares BB+RSI Mean Reversion Strategy (Strategy B)
Python port of BB_RSI_MeanReversion.mq5 — runs via api2trade.com REST API.

Trigger: Called by ares_telegram_bot.py on /ares_analyze
         OR by ares_runner.py timer if auto-mode is enabled in config.

NEVER trades autonomously — ares_telegram_bot.py gate controls execution.
"""
import os, json, time, logging, math
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
import yaml

# ── Config ─────────────────────────────────────────────────────────────────────
CONFIG_PATH = Path(__file__).parent / "ares_config.yaml"
if not CONFIG_PATH.exists():
    CONFIG_PATH = Path(__file__).parents[2] / "configs" / "ares_config.yaml"
with open(CONFIG_PATH) as f:
    CFG = yaml.safe_load(f)

# Account — uses SAME account as Hermes (Account A bridge) unless overridden
BRIDGE     = os.getenv("ARES_BRIDGE_URL", CFG["bridge"]["url"])
MT5_API    = os.getenv("MT5_API_URL",    "https://api.api2trade.com")
MT5_ID     = os.getenv("MT5_ACCOUNT_ID", CFG["mt5_api"]["account_id"])
MT5_KEY    = os.getenv("MT5_API_KEY",    CFG["mt5_api"]["api_key"])
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
    local_log_dir = Path(__file__).parents[2] / "logs" / "ares"
    local_log_dir.mkdir(parents=True, exist_ok=True)
    JOURNAL = local_log_dir / "trade_journal.jsonl"


# Strategy parameters — mirroring all EA inputs
BB_PERIOD       = 20
BB_DEVIATION    = 2.0
RSI_PERIOD      = 14
RSI_OVERSOLD    = float(CFG["strictness"].get("rsi_oversold", 30))
RSI_OVERBOUGHT  = float(CFG["strictness"].get("rsi_overbought", 70))
CONTEXT_MA_PER  = int(CFG["strictness"].get("min_adx", 50))  # M15 MA period
CONTEXT_MA_TOL  = 0.0002
USE_M15_CONTEXT = CFG["risk"].get("block_medium_news", True)
REQUIRE_OUTSIDE = True     # Price must close outside BB
REQUIRE_RSI     = True     # RSI must confirm
SL_PIPS         = int(CFG.get("sl_pips", 20))
TP_PIPS         = int(CFG.get("tp_pips", 40))
RISK_PCT        = float(CFG["risk"]["risk_pct"])
MIN_RR          = float(CFG["risk"]["min_rr_ratio"])
MAX_SPREAD_PIPS = 1.0
BLOCK_NEWS_MINS = int(CFG["risk"]["block_news_minutes"])
START_HOUR      = 5    # GMT
END_HOUR        = 17   # GMT
MAGIC_COMMENT   = CFG["strategy"]["comment"]   # "ARES-v1"
MIN_ADX         = float(CFG["strictness"]["min_adx"])  # 22

# Resolve safe log path (fallback to local logs/ if system dir not writable)
default_log = "/var/log/ares/ares_cycle.log"
try:
    Path(default_log).parent.mkdir(parents=True, exist_ok=True)
    log_file = default_log
except Exception:
    local_log_dir = Path(__file__).parents[2] / "logs" / "ares"
    local_log_dir.mkdir(parents=True, exist_ok=True)
    log_file = str(local_log_dir / "ares_cycle.log")

logging.basicConfig(
    filename=log_file,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger(__name__)

# ── Cache helpers ──────────────────────────────────────────────────────────────
def load_cache() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text()) if CACHE_FILE.exists() else {}
    except:
        return {}

def save_cache(c: dict):
    CACHE_FILE.write_text(json.dumps(c))

# ── API2TRADE direct call (no local bridge needed) ────────────────────────────
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


def mt5api(path, params=None) -> dict | list:
    try:
        r = requests.get(
            f"{MT5_API}{path}",
            headers={"x-api-key": MT5_KEY},
            params={"id": MT5_ID, **(params or {})},
            timeout=15
        )
        return r.json()
    except Exception as e:
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

# ── Market data via yfinance (same approach as trading_cycle.py) ───────────────
YF_MAP = {
    "EURUSDxx": "EURUSD=X", "GBPUSDxx": "GBPUSD=X", "USDJPYxx": "USDJPY=X",
    "XAUUSDxx": "GC=F",     "GBPJPYxx": "GBPJPY=X",
}

def get_bars(symbol: str, tf: str = "1m", count: int = 150) -> list:
    """Fetch OHLCV bars via yfinance. tf = '1m','15m','1h','4h','1d'."""
    try:
        import yfinance as yf
        import pandas as pd
        yf_sym   = YF_MAP.get(symbol, symbol.replace("xx", "=X"))
        period_map = {"1m": "5d", "15m": "5d", "1h": "60d", "4h": "60d", "1d": "365d"}
        period   = period_map.get(tf, "5d")
        df = yf.download(yf_sym, period=period, interval=tf,
                         progress=False, auto_adjust=True)
        if df.empty:
            return []
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.columns = [c.lower() for c in df.columns]
        df = df.rename(columns={"adj close": "close"})
        df = df.dropna().tail(count).reset_index()
        return df.to_dict("records")
    except Exception as e:
        log.error(f"get_bars {symbol}/{tf}: {e}")
        return []

# ── Indicator calculations ─────────────────────────────────────────────────────
def compute_bb_rsi(bars: list, bb_period=20, bb_dev=2.0, rsi_period=14) -> dict:
    """
    Compute Bollinger Bands and RSI from raw OHLCV bars.
    Returns dict with bb_upper, bb_lower, bb_middle, rsi for the LAST CLOSED bar.
    """
    if len(bars) < max(bb_period, rsi_period) + 5:
        return {}
    try:
        import pandas as pd
        import ta

        df = pd.DataFrame(bars)
        df.columns = [c.lower() for c in df.columns]
        df["close"] = df["close"].astype(float)
        df["high"]  = df["high"].astype(float)
        df["low"]   = df["low"].astype(float)

        # Bollinger Bands
        bb_upper = ta.volatility.bollinger_hband(df["close"], window=bb_period, window_dev=bb_dev)
        bb_lower = ta.volatility.bollinger_lband(df["close"], window=bb_period, window_dev=bb_dev)
        bb_mid   = ta.volatility.bollinger_mavg(df["close"],  window=bb_period)
        bb_pct   = ta.volatility.bollinger_pband(df["close"], window=bb_period, window_dev=bb_dev)

        # RSI
        rsi = ta.momentum.rsi(df["close"], window=rsi_period)

        # ADX (for trend strength — strictness gate)
        adx     = ta.trend.adx(df["high"], df["low"], df["close"], window=14)
        adx_pos = ta.trend.adx_pos(df["high"], df["low"], df["close"], window=14)
        adx_neg = ta.trend.adx_neg(df["high"], df["low"], df["close"], window=14)

        # MACD histogram for momentum confirmation
        macd_hist = ta.trend.macd_diff(df["close"])

        # EMA trend
        ema20 = ta.trend.ema_indicator(df["close"], window=20)
        ema50 = ta.trend.ema_indicator(df["close"], window=50)

        # Use index -2 = last FULLY CLOSED bar (index -1 is current forming)
        i = -2

        def safe(s):
            try:
                v = float(s.iloc[i])
                return None if math.isnan(v) else round(v, 6)
            except:
                return None

        return {
            "bb_upper":   safe(bb_upper),
            "bb_lower":   safe(bb_lower),
            "bb_middle":  safe(bb_mid),
            "bb_pct":     safe(bb_pct),
            "rsi":        safe(rsi),
            "adx":        safe(adx),
            "adx_plus":   safe(adx_pos),
            "adx_minus":  safe(adx_neg),
            "macd_hist":  safe(macd_hist),
            "ema20":      safe(ema20),
            "ema50":      safe(ema50),
            "close":      round(float(df["close"].iloc[i]), 6),
            "trend":      "bullish" if (safe(ema20) or 0) > (safe(ema50) or 0) else "bearish",
        }
    except Exception as e:
        log.error(f"compute_bb_rsi: {e}")
        return {}

def get_m15_sma(symbol: str, period: int = 50) -> float | None:
    """Get SMA-50 on M15 for context filtering."""
    cache = load_cache()
    key   = f"ares_m15_sma_{symbol}"
    now   = time.time()
    if key in cache and now - cache[key].get("ts", 0) < 300:  # 5-min cache
        return cache[key].get("val")
    try:
        import ta, pandas as pd
        bars = get_bars(symbol, "15m", period + 10)
        if len(bars) < period:
            return None
        df = pd.DataFrame(bars)
        df.columns = [c.lower() for c in df.columns]
        df["close"] = df["close"].astype(float)
        sma = ta.trend.sma_indicator(df["close"], window=period)
        val = round(float(sma.iloc[-2]), 6)
        cache[key] = {"ts": now, "val": val}
        save_cache(cache)
        return val
    except Exception as e:
        log.error(f"get_m15_sma {symbol}: {e}")
        return None

# ── Pip size helper ────────────────────────────────────────────────────────────
def pip_size(symbol: str) -> float:
    if "JPY" in symbol.upper():
        return 0.01
    if "XAU" in symbol.upper() or "GOLD" in symbol.upper():
        return 0.1
    return 0.0001

def price_to_pips(diff: float, symbol: str) -> float:
    return abs(diff) / pip_size(symbol)

# ── Lot size calculation ───────────────────────────────────────────────────────
def calculate_lot(equity: float, sl_pips: int, symbol: str) -> float:
    risk_eur = equity * RISK_PCT
    # Approximate pip value: €10 per pip per standard lot for EUR pairs
    pip_val_per_lot = 10.0
    if "JPY" in symbol.upper():  pip_val_per_lot = 9.0
    if "GBP" in symbol.upper():  pip_val_per_lot = 12.5
    if "XAU" in symbol.upper():  pip_val_per_lot = 1.0  # Gold ~$1/pip/0.01lot
    raw_lot = risk_eur / (sl_pips * pip_val_per_lot)
    # Clamp and round to 0.01 step
    raw_lot = max(0.01, min(raw_lot, 5.0))
    return round(round(raw_lot / 0.01) * 0.01, 2)

# ── Spread check ───────────────────────────────────────────────────────────────
def get_spread_pips(quote: dict, symbol: str) -> float:
    ask = float(quote.get("ask", 0))
    bid = float(quote.get("bid", 0))
    return price_to_pips(ask - bid, symbol)

# ── News / calendar block ──────────────────────────────────────────────────────
def check_news_block(symbol: str) -> tuple[bool, list]:
    now_utc  = datetime.now(timezone.utc)
    warnings = []
    blocked_ccys = set()
    cache    = load_cache()

    # ForexFactory calendar (shared cache with Hermes)
    ff_events = cache.get("ff_cal", {}).get("data", [])
    for evt in ff_events:
        try:
            et   = datetime.fromisoformat(evt.get("date", "")).astimezone(timezone.utc)
            mins = (et - now_utc).total_seconds() / 60
            if evt.get("impact") == "High" and -15 < mins < BLOCK_NEWS_MINS:
                blocked_ccys.add(evt.get("currency", "")[:3])
                warnings.append(f"High impact: {evt.get('title')} in {int(mins)}min")
        except:
            pass

    # MT5 API news (fresh)
    try:
        news = requests.get(f"{MT5_API}/news", timeout=8).json()
        for evt in (news if isinstance(news, list) else []):
            try:
                et   = datetime.fromisoformat(evt["date"]).astimezone(timezone.utc)
                mins = (et - now_utc).total_seconds() / 60
                if evt.get("impact") == "High" and -15 < mins < BLOCK_NEWS_MINS:
                    blocked_ccys.add(evt.get("country", "")[:3])
                    warnings.append(f"MT5 High: {evt.get('title')} in {int(mins)}min")
            except:
                pass
    except:
        pass

    sym_up  = symbol.upper()
    blocked = any(c and c in sym_up for c in blocked_ccys if c)
    return blocked, warnings

# ── Session filter ─────────────────────────────────────────────────────────────
def is_trade_time() -> bool:
    now_utc = datetime.now(timezone.utc)
    wd, hr  = now_utc.weekday(), now_utc.hour
    # Weekend check
    if (wd == 4 and hr >= 22) or wd == 5 or (wd == 6 and hr < 22):
        return False
    # Session hours (GMT)
    return START_HOUR <= hr < END_HOUR

# ── Position check ─────────────────────────────────────────────────────────────
def has_open_position() -> bool:
    positions = bridge("/positions")
    if isinstance(positions, list):
        for p in positions:
            comment = str(p.get("comment", ""))
            if MAGIC_COMMENT in comment or MAGIC_COMMENT.split("-")[0] in comment:
                return True
    return False

# ─────────────────────────────────────────────────────────────────────────────
# MAIN ANALYSIS FUNCTION
# Called by ares_telegram_bot.py on /ares_analyze <SYMBOL>
# Returns signal dict — NEVER places a trade itself.
# ─────────────────────────────────────────────────────────────────────────────
def run_analysis(symbol: str) -> dict:
    """
    Full BB+RSI mean reversion analysis for `symbol`.
    Returns signal dict with action='trade' or action='wait'.
    """
    symbol = symbol.upper()
    if not symbol.endswith("XX"):
        symbol = symbol + "xx"
    # Normalise to mixed case suffix used by broker
    symbol = symbol[:-2] + "xx"

    log.info(f"=== Ares BB+RSI Analysis: {symbol} ===")

    # ── 1. Account state ───────────────────────────────────────────
    account = bridge("/balance")
    if "error" in account:
        return {"action": "wait", "reason": f"Bridge unreachable: {account['error']}"}
    equity  = float(account.get("equity", 0))
    balance = float(account.get("balance", 0))
    if equity <= 0:
        return {"action": "wait", "reason": "Account equity is zero or unavailable."}

    # ── 2. Existing position check ─────────────────────────────────
    if has_open_position():
        return {"action": "wait", "reason": "Ares position already open. Close it first."}

    # ── 3. Session filter ──────────────────────────────────────────
    if not is_trade_time():
        return {"action": "wait", "reason": f"Outside trading session (GMT {START_HOUR}:00–{END_HOUR}:00)."}

    # ── 4. Live quote + spread ─────────────────────────────────────
    quote = bridge(f"/quote?symbol={symbol}")
    if "error" in quote or not quote.get("bid"):
        return {"action": "wait", "reason": f"No live quote for {symbol}."}
    bid = float(quote["bid"])
    ask = float(quote["ask"])
    spread_pips = get_spread_pips(quote, symbol)
    if spread_pips > MAX_SPREAD_PIPS:
        return {
            "action": "wait",
            "reason": f"Spread too wide: {spread_pips:.2f} pips > max {MAX_SPREAD_PIPS} pips."
        }

    # ── 5. News block ──────────────────────────────────────────────
    blocked, news_warnings = check_news_block(symbol)
    if blocked:
        return {
            "action": "wait",
            "reason": f"Blocked by news: {'; '.join(news_warnings[:2])}"
        }

    # ── 6. M1 bars + indicators ────────────────────────────────────
    bars_m1 = get_bars(symbol, "1m", 150)
    if len(bars_m1) < 50:
        return {"action": "wait", "reason": "Insufficient M1 bar data."}

    ind = compute_bb_rsi(bars_m1, BB_PERIOD, BB_DEVIATION, RSI_PERIOD)
    if not ind:
        return {"action": "wait", "reason": "Indicator computation failed."}

    close    = ind["close"]
    bb_upper = ind["bb_upper"]
    bb_lower = ind["bb_lower"]
    rsi      = ind["rsi"]
    adx      = ind["adx"]
    adx_p    = ind["adx_plus"]
    adx_n    = ind["adx_minus"]
    mhist    = ind["macd_hist"]

    if None in (close, bb_upper, bb_lower, rsi):
        return {"action": "wait", "reason": "One or more indicator values are None."}

    # ── 7. M15 context (SMA50) ─────────────────────────────────────
    m15_sma   = get_m15_sma(symbol, 50)
    ctx_valid_long  = True
    ctx_valid_short = True
    if m15_sma is not None:
        ctx_valid_long  = close > m15_sma - CONTEXT_MA_TOL
        ctx_valid_short = close < m15_sma + CONTEXT_MA_TOL

    # ── 8. Signal logic (mirrors MQL5 EA exactly) ──────────────────
    long_signal  = False
    short_signal = False

    # Long conditions
    bb_long  = (close < bb_lower)  if REQUIRE_OUTSIDE else True
    rsi_long = (rsi < RSI_OVERSOLD) if REQUIRE_RSI    else True
    long_signal = bb_long and rsi_long and ctx_valid_long

    # Short conditions
    bb_short  = (close > bb_upper)   if REQUIRE_OUTSIDE else True
    rsi_short = (rsi > RSI_OVERBOUGHT) if REQUIRE_RSI  else True
    short_signal = bb_short and rsi_short and ctx_valid_short

    if not long_signal and not short_signal:
        return {
            "action": "wait",
            "reason": (
                f"No signal. Close={close:.5f} | "
                f"BB=[{bb_lower:.5f}, {bb_upper:.5f}] | RSI={rsi:.1f}"
            )
        }

    # ── 9. Strictness confluence (Strategy B extra gate) ──────────
    # ADX confirms trend momentum exists
    adx_ok = adx is not None and adx > MIN_ADX

    conditions_met   = []
    conditions_failed = []

    if long_signal:
        direction = "Buy"
        entry     = ask
        sl        = round(ask - SL_PIPS * pip_size(symbol), 6)
        tp        = round(ask + TP_PIPS * pip_size(symbol), 6)

        conds = [
            (close < bb_lower,           f"Price below lower BB ({close:.5f} < {bb_lower:.5f})"),
            (rsi < RSI_OVERSOLD,         f"RSI oversold ({rsi:.1f} < {RSI_OVERSOLD})"),
            (ctx_valid_long,             f"M15 SMA50 context valid (price above SMA-tol)"),
            (adx_ok,                     f"ADX momentum ({adx:.1f} > {MIN_ADX})"),
            (mhist is not None and mhist > -0.0001,
                                         f"MACD histogram not strongly bearish ({mhist:.6f})"),
        ]
    else:
        direction = "Sell"
        entry     = bid
        sl        = round(bid + SL_PIPS * pip_size(symbol), 6)
        tp        = round(bid - TP_PIPS * pip_size(symbol), 6)

        conds = [
            (close > bb_upper,           f"Price above upper BB ({close:.5f} > {bb_upper:.5f})"),
            (rsi > RSI_OVERBOUGHT,       f"RSI overbought ({rsi:.1f} > {RSI_OVERBOUGHT})"),
            (ctx_valid_short,            f"M15 SMA50 context valid (price below SMA+tol)"),
            (adx_ok,                     f"ADX momentum ({adx:.1f} > {MIN_ADX})"),
            (mhist is not None and mhist < 0.0001,
                                         f"MACD histogram not strongly bullish ({mhist:.6f})"),
        ]

    for met, desc in conds:
        (conditions_met if met else conditions_failed).append(desc)

    min_confluence = int(CFG["strictness"]["min_confluence_count"])
    if len(conditions_met) < min_confluence:
        return {
            "action":           "wait",
            "reason":           f"Only {len(conditions_met)}/{min_confluence} conditions met.",
            "conditions_met":   conditions_met,
            "conditions_failed": conditions_failed,
        }

    # ── 10. R:R gate ───────────────────────────────────────────────
    sl_pips_val = price_to_pips(entry - sl, symbol)
    tp_pips_val = price_to_pips(tp - entry, symbol)
    rr          = round(tp_pips_val / sl_pips_val, 2) if sl_pips_val > 0 else 0

    if rr < MIN_RR:
        return {"action": "wait", "reason": f"R:R {rr} below minimum {MIN_RR}."}

    # ── 11. Lot size ───────────────────────────────────────────────
    volume = calculate_lot(equity, SL_PIPS, symbol)

    # ── 12. Build signal ───────────────────────────────────────────
    reason = (
        f"BB+RSI mean reversion — {len(conditions_met)}/{len(conds)} conditions met. "
        f"RSI={rsi:.1f}, BB_pct={ind.get('bb_pct', '?')}, ADX={adx:.1f}, "
        f"Spread={spread_pips:.2f}pips, R:R={rr}."
    )
    log.info(f"SIGNAL: {direction} {symbol} | Entry={entry} SL={sl} TP={tp} Vol={volume} RR={rr}")

    return {
        "action":            "trade",
        "symbol":            symbol,
        "direction":         direction,
        "entry":             entry,
        "stop_loss":         sl,
        "take_profit":       tp,
        "volume":            volume,
        "rr_ratio":          rr,
        "sl_pips":           round(sl_pips_val, 1),
        "tp_pips":           round(tp_pips_val, 1),
        "confidence":        "high" if len(conditions_met) >= min_confluence + 1 else "medium",
        "conditions_met":    conditions_met,
        "conditions_failed": conditions_failed,
        "warnings":          news_warnings,
        "reason":            reason,
        "indicators":        {
            "close": close, "bb_upper": bb_upper, "bb_lower": bb_lower,
            "rsi": rsi, "adx": adx, "macd_hist": mhist,
            "m15_sma50": m15_sma, "spread_pips": spread_pips,
        },
        "analysed_at": datetime.now(timezone.utc).isoformat(),
    }


# ── CLI test mode ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    sym = sys.argv[1] if len(sys.argv) > 1 else "EURUSDxx"
    print(f"Running Ares analysis for {sym}...")
    result = run_analysis(sym)
    print(json.dumps(result, indent=2, default=str))
