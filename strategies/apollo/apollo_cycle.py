#!/usr/bin/env python3
"""
GENESIS — Apollo Cycle (Strategy C: MA Crossover Trend Following)
Called by apollo_tool.py when Hermes decides to run a trend analysis.

Strategy logic:
  - Fast EMA(9) crosses above Slow EMA(21) on M5 → BUY (Golden Cross)
  - Fast EMA(9) crosses below Slow EMA(21) on M5 → SELL (Death Cross)
  - H1 SMA(50) trend alignment required (only trade in direction of H1 trend)
  - ATR-based dynamic SL/TP (adapts to volatility)
  - ADX confirms trend strength
  - News + session + spread filters (same as Ares)

run_analysis(symbol) → signal dict — NEVER places a trade itself.
"""
import os, json, time, logging, math
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

CONFIG_PATH = Path(__file__).parent / "apollo_config.yaml"
if not CONFIG_PATH.exists():
    CONFIG_PATH = Path(__file__).parents[2] / "configs" / "apollo_config.yaml"
with open(CONFIG_PATH) as f:
    CFG = yaml.safe_load(f)

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
    local_log_dir = Path(__file__).parents[2] / "logs" / "apollo"
    local_log_dir.mkdir(parents=True, exist_ok=True)
    JOURNAL = local_log_dir / "trade_journal.jsonl"


FAST_MA     = int(CFG["indicators"]["fast_ma_period"])
SLOW_MA     = int(CFG["indicators"]["slow_ma_period"])
MA_METHOD   = CFG["indicators"]["ma_method"]
SIG_TF      = CFG["indicators"]["signal_timeframe"]
TREND_TF    = CFG["indicators"]["trend_timeframe"]
TREND_MA    = int(CFG["indicators"]["trend_ma_period"])
ATR_PERIOD  = int(CFG["indicators"]["atr_period"])

RISK_PCT        = float(CFG["risk"]["risk_pct"])
MIN_RR          = float(CFG["risk"]["min_rr_ratio"])
SL_ATR_MULT     = float(CFG["risk"]["sl_atr_multiplier"])
TP_ATR_MULT     = float(CFG["risk"]["tp_atr_multiplier"])
MAX_SPREAD      = float(CFG["risk"]["max_spread_pips"])
BLOCK_NEWS_MINS = int(CFG["risk"]["block_news_minutes"])
BLOCK_MEDIUM    = bool(CFG["risk"]["block_medium_news"])

REQUIRE_TREND   = bool(CFG["strictness"]["require_trend_alignment"])
MIN_MA_SEP      = float(CFG["strictness"]["min_ma_separation_pct"])
MIN_ADX         = float(CFG["strictness"]["min_adx"])
COOLDOWN_SECS   = int(CFG["strictness"]["cooldown_seconds"])

START_HOUR      = int(CFG["sessions"]["allowed"][0]["start"])
END_HOUR        = int(CFG["sessions"]["allowed"][0]["end"])
MAGIC_COMMENT   = CFG["strategy"]["comment"]

# Resolve safe log path (fallback to local logs/ if system dir not writable)
default_log = "/var/log/apollo/apollo_cycle.log"
try:
    Path(default_log).parent.mkdir(parents=True, exist_ok=True)
    log_file = default_log
except Exception:
    local_log_dir = Path(__file__).parents[2] / "logs" / "apollo"
    local_log_dir.mkdir(parents=True, exist_ok=True)
    log_file = str(local_log_dir / "apollo_cycle.log")

logging.basicConfig(
    filename=log_file,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger(__name__)

# Cooldown state (in-process; reset on restart — acceptable)
_last_signal_time: dict = {}

# ── Cache ──────────────────────────────────────────────────────────────────────
def load_cache() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text()) if CACHE_FILE.exists() else {}
    except:
        return {}

def save_cache(c):
    CACHE_FILE.write_text(json.dumps(c))

# ── Bridge ─────────────────────────────────────────────────────────────────────
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

# ── Market data ────────────────────────────────────────────────────────────────
YF_MAP = {
    "EURUSDxx": "EURUSD=X", "GBPUSDxx": "GBPUSD=X", "USDJPYxx": "USDJPY=X",
    "XAUUSDxx": "GC=F",     "GBPJPYxx": "GBPJPY=X",
}
YF_TF = {"M1": "1m", "M5": "5m", "M15": "15m", "H1": "1h", "H4": "4h", "D1": "1d"}

def get_bars(symbol: str, tf: str = "M5", count: int = 100) -> list:
    try:
        import yfinance as yf, pandas as pd
        yf_sym   = YF_MAP.get(symbol, symbol.replace("xx", "=X"))
        interval = YF_TF.get(tf, "5m")
        period   = {"1m": "5d", "5m": "5d", "15m": "5d", "1h": "60d",
                    "4h": "60d", "1d": "365d"}.get(interval, "5d")
        df = yf.download(yf_sym, period=period, interval=interval,
                         progress=False, auto_adjust=True)
        if df.empty:
            return []
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.columns = [c.lower() for c in df.columns]
        return df.dropna().tail(count).reset_index().to_dict("records")
    except Exception as e:
        log.error(f"get_bars {symbol}/{tf}: {e}")
        return []

# ── Core indicator calculation ─────────────────────────────────────────────────
def compute_ma_crossover(bars: list, fast: int, slow: int,
                          method: str = "EMA") -> dict:
    """
    Compute fast/slow MA and detect crossover on the last two closed candles.
    Returns crossover dict with current + previous values.

    Crossover detected by comparing [bar -2] vs [bar -1]:
      - Use index -3 and -2 (leave -1 as the currently forming candle)
    """
    needed = slow + 5
    if len(bars) < needed:
        return {}
    try:
        import pandas as pd, ta

        df = pd.DataFrame(bars)
        df.columns = [c.lower() for c in df.columns]
        df["close"] = df["close"].astype(float)
        df["high"]  = df["high"].astype(float)
        df["low"]   = df["low"].astype(float)

        # MA calculation
        if method.upper() == "EMA":
            fast_ma = ta.trend.ema_indicator(df["close"], window=fast)
            slow_ma = ta.trend.ema_indicator(df["close"], window=slow)
        else:
            fast_ma = ta.trend.sma_indicator(df["close"], window=fast)
            slow_ma = ta.trend.sma_indicator(df["close"], window=slow)

        # ADX for trend strength
        adx     = ta.trend.adx(df["high"], df["low"], df["close"], window=14)
        adx_pos = ta.trend.adx_pos(df["high"], df["low"], df["close"], window=14)
        adx_neg = ta.trend.adx_neg(df["high"], df["low"], df["close"], window=14)

        # ATR for dynamic SL/TP
        atr = ta.volatility.average_true_range(
            df["high"], df["low"], df["close"], window=ATR_PERIOD)

        # MACD for momentum confirmation
        macd_hist = ta.trend.macd_diff(df["close"])

        def safe(s, i=-2):
            try:
                v = float(s.iloc[i])
                return None if math.isnan(v) else round(v, 6)
            except:
                return None

        # Current = last closed candle (index -2), Prev = one before (index -3)
        curr_fast = safe(fast_ma, -2)
        curr_slow = safe(slow_ma, -2)
        prev_fast = safe(fast_ma, -3)
        prev_slow = safe(slow_ma, -3)

        if None in (curr_fast, curr_slow, prev_fast, prev_slow):
            return {}

        # Crossover detection
        golden_cross = (prev_fast <= prev_slow) and (curr_fast > curr_slow)
        death_cross  = (prev_fast >= prev_slow) and (curr_fast < curr_slow)

        # MA separation check (filter weak crosses)
        separation = abs(curr_fast - curr_slow) / curr_slow if curr_slow > 0 else 0

        return {
            "curr_fast":    curr_fast,
            "curr_slow":    curr_slow,
            "prev_fast":    prev_fast,
            "prev_slow":    prev_slow,
            "separation":   round(separation, 6),
            "golden_cross": golden_cross,
            "death_cross":  death_cross,
            "adx":          safe(adx),
            "adx_plus":     safe(adx_pos),
            "adx_minus":    safe(adx_neg),
            "atr":          safe(atr),
            "macd_hist":    safe(macd_hist),
            "close":        round(float(df["close"].iloc[-2]), 6),
        }
    except Exception as e:
        log.error(f"compute_ma_crossover: {e}")
        return {}

def get_trend_ma(symbol: str) -> float | None:
    """H1 SMA(50) for higher-timeframe trend direction."""
    cache = load_cache()
    key   = f"apollo_h1sma_{symbol}"
    now   = time.time()
    if key in cache and now - cache[key].get("ts", 0) < 300:
        return cache[key].get("val")
    try:
        import pandas as pd, ta
        bars = get_bars(symbol, "H1", TREND_MA + 10)
        if len(bars) < TREND_MA:
            return None
        df = pd.DataFrame(bars)
        df.columns = [c.lower() for c in df.columns]
        df["close"] = df["close"].astype(float)
        sma = ta.trend.sma_indicator(df["close"], window=TREND_MA)
        val = round(float(sma.iloc[-2]), 6)
        cache[key] = {"ts": now, "val": val}
        save_cache(cache)
        return val
    except Exception as e:
        log.error(f"get_trend_ma {symbol}: {e}")
        return None

# ── Helpers (mirrors ares_cycle.py) ───────────────────────────────────────────
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

def check_news_block(symbol: str) -> tuple[bool, list]:
    now_utc  = datetime.now(timezone.utc)
    warnings = []
    blocked  = set()
    cache    = load_cache()
    for evt in cache.get("ff_cal", {}).get("data", []):
        try:
            et   = datetime.fromisoformat(evt.get("date","")).astimezone(timezone.utc)
            mins = (et - now_utc).total_seconds() / 60
            imp  = evt.get("impact","")
            if imp == "High" and -15 < mins < BLOCK_NEWS_MINS:
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

def has_apollo_position() -> bool:
    pos = bridge("/positions")
    if isinstance(pos, list):
        for p in pos:
            if "APOLLO" in str(p.get("comment", "")).upper():
                return True
    return False

# ── Main analysis ──────────────────────────────────────────────────────────────
def run_analysis(symbol: str) -> dict:
    """
    Full MA Crossover trend-following analysis.
    Returns signal dict with action='trade' or action='wait'.
    Never places a trade — apollo_tool.py handles execution.
    """
    symbol = symbol.upper()
    if not symbol.endswith("XX"):
        symbol = symbol + "xx"
    symbol = symbol[:-2] + "xx"

    log.info(f"=== Apollo MA Crossover Analysis: {symbol} ===")

    # ── Account ────────────────────────────────────────────────────
    account = bridge("/balance")
    if "error" in account:
        return {"action": "wait", "reason": f"Bridge unreachable: {account['error']}"}
    equity = float(account.get("equity", 0))
    if equity <= 0:
        return {"action": "wait", "reason": "Account equity unavailable."}

    # ── Existing Apollo position ───────────────────────────────────
    if has_apollo_position():
        return {"action": "wait", "reason": "Apollo position already open."}

    # ── Cooldown ───────────────────────────────────────────────────
    last = _last_signal_time.get(symbol, 0)
    if time.time() - last < COOLDOWN_SECS:
        remaining = int(COOLDOWN_SECS - (time.time() - last))
        return {"action": "wait", "reason": f"Cooldown active: {remaining}s remaining."}

    # ── Session ────────────────────────────────────────────────────
    if not is_trade_time():
        return {"action": "wait",
                "reason": f"Outside session (GMT {START_HOUR}:00–{END_HOUR}:00)."}

    # ── Quote + spread ─────────────────────────────────────────────
    quote = bridge(f"/quote?symbol={symbol}")
    if "error" in quote or not quote.get("bid"):
        return {"action": "wait", "reason": f"No live quote for {symbol}."}
    bid = float(quote["bid"])
    ask = float(quote["ask"])
    spread_pips = price_to_pips(ask - bid, symbol)
    if spread_pips > MAX_SPREAD:
        return {"action": "wait",
                "reason": f"Spread {spread_pips:.2f} pips > max {MAX_SPREAD}."}

    # ── News ───────────────────────────────────────────────────────
    blocked, news_warn = check_news_block(symbol)
    if blocked:
        return {"action": "wait",
                "reason": f"News block: {'; '.join(news_warn[:2])}"}

    # ── M5 bars + MA crossover ─────────────────────────────────────
    bars_m5 = get_bars(symbol, SIG_TF, SLOW_MA + 20)
    if len(bars_m5) < SLOW_MA + 5:
        return {"action": "wait", "reason": "Insufficient M5 bar data."}

    ind = compute_ma_crossover(bars_m5, FAST_MA, SLOW_MA, MA_METHOD)
    if not ind:
        return {"action": "wait", "reason": "MA calculation failed."}

    golden = ind["golden_cross"]
    death  = ind["death_cross"]

    if not golden and not death:
        return {
            "action": "wait",
            "reason": (
                f"No crossover. Fast={ind['curr_fast']:.5f} "
                f"Slow={ind['curr_slow']:.5f} "
                f"(prev: {ind['prev_fast']:.5f}/{ind['prev_slow']:.5f})"
            )
        }

    direction = "Buy" if golden else "Sell"
    signal_type = "GOLDEN_CROSS" if golden else "DEATH_CROSS"

    # ── H1 trend alignment ─────────────────────────────────────────
    trend_ma    = get_trend_ma(symbol)
    close_price = ind["close"]
    trend_ok    = True
    trend_note  = "Trend filter skipped (MA unavailable)"

    if trend_ma is not None and REQUIRE_TREND:
        if direction == "Buy":
            trend_ok  = close_price > trend_ma
            trend_note = (f"H1 SMA{TREND_MA}={trend_ma:.5f} — "
                          f"{'aligned ✓' if trend_ok else 'AGAINST trend ✗'}")
        else:
            trend_ok  = close_price < trend_ma
            trend_note = (f"H1 SMA{TREND_MA}={trend_ma:.5f} — "
                          f"{'aligned ✓' if trend_ok else 'AGAINST trend ✗'}")

    # ── Confluence checks ──────────────────────────────────────────
    adx      = ind.get("adx")
    sep      = ind.get("separation", 0)
    mhist    = ind.get("macd_hist")
    atr      = ind.get("atr")

    conds = []
    if direction == "Buy":
        conds = [
            (golden,                               f"Golden Cross: EMA{FAST_MA} crossed above EMA{SLOW_MA}"),
            (trend_ok,                             trend_note),
            (adx is not None and adx > MIN_ADX,   f"ADX trend strength {adx:.1f} > {MIN_ADX}"),
            (sep >= MIN_MA_SEP,                    f"MA separation {sep*100:.3f}% ≥ {MIN_MA_SEP*100:.3f}%"),
            (mhist is not None and mhist > 0,      f"MACD histogram positive ({mhist:.6f})"),
        ]
    else:
        conds = [
            (death,                                f"Death Cross: EMA{FAST_MA} crossed below EMA{SLOW_MA}"),
            (trend_ok,                             trend_note),
            (adx is not None and adx > MIN_ADX,   f"ADX trend strength {adx:.1f} > {MIN_ADX}"),
            (sep >= MIN_MA_SEP,                    f"MA separation {sep*100:.3f}% ≥ {MIN_MA_SEP*100:.3f}%"),
            (mhist is not None and mhist < 0,      f"MACD histogram negative ({mhist:.6f})"),
        ]

    passed = [(m, d) for m, d in conds if m]
    failed = [(m, d) for m, d in conds if not m]

    # Require at least 4/5 for Apollo (trend-following can be less strict than Ares)
    min_conf = 4
    if len(passed) < min_conf:
        return {
            "action":            "wait",
            "reason":            f"Only {len(passed)}/{min_conf} conditions met.",
            "conditions_met":    [d for _, d in passed],
            "conditions_failed": [d for _, d in failed],
        }

    # ── ATR-based SL/TP ───────────────────────────────────────────
    if atr is None or atr <= 0:
        atr = 0.001  # fallback

    if direction == "Buy":
        entry = ask
        sl    = round(entry - atr * SL_ATR_MULT, 6)
        tp    = round(entry + atr * TP_ATR_MULT, 6)
    else:
        entry = bid
        sl    = round(entry + atr * SL_ATR_MULT, 6)
        tp    = round(entry - atr * TP_ATR_MULT, 6)

    sl_pips = price_to_pips(entry - sl, symbol)
    tp_pips = price_to_pips(tp - entry, symbol)
    rr      = round(tp_pips / sl_pips, 2) if sl_pips > 0 else 0

    if rr < MIN_RR:
        return {"action": "wait",
                "reason": f"R:R {rr} below minimum {MIN_RR}."}

    volume = calculate_lot(equity, sl_pips, symbol)

    # Update cooldown
    _last_signal_time[symbol] = time.time()

    reason = (
        f"Apollo {signal_type}: EMA{FAST_MA}={ind['curr_fast']:.5f} "
        f"{'>' if golden else '<'} EMA{SLOW_MA}={ind['curr_slow']:.5f}. "
        f"ADX={adx:.1f}, ATR={atr:.5f}, R:R={rr}, Spread={spread_pips:.2f}pips."
    )
    log.info(f"SIGNAL: {direction} {symbol} | {signal_type} | SL={sl} TP={tp} Vol={volume}")

    return {
        "action":            "trade",
        "strategy":          "apollo-ma-crossover",
        "signal_type":       signal_type,
        "symbol":            symbol,
        "direction":         direction,
        "entry":             entry,
        "stop_loss":         sl,
        "take_profit":       tp,
        "volume":            volume,
        "rr_ratio":          rr,
        "sl_pips":           round(sl_pips, 1),
        "tp_pips":           round(tp_pips, 1),
        "confidence":        "high" if len(passed) == len(conds) else "medium",
        "conditions_met":    [d for _, d in passed],
        "conditions_failed": [d for _, d in failed],
        "warnings":          news_warn,
        "reason":            reason,
        "indicators": {
            "fast_ma":    ind["curr_fast"],
            "slow_ma":    ind["curr_slow"],
            "adx":        adx,
            "atr":        atr,
            "macd_hist":  mhist,
            "h1_trend_ma": trend_ma,
            "spread_pips": spread_pips,
        },
        "analysed_at": datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    import sys
    sym = sys.argv[1] if len(sys.argv) > 1 else "EURUSDxx"
    print(f"Running Apollo analysis for {sym}...")
    result = run_analysis(sym)
    print(json.dumps(result, indent=2, default=str))
