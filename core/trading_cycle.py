#!/usr/bin/env python3
"""
GENESIS Enhanced Autonomous Trading Cycle v2.1 — Phase 1 Safety Upgrades
Data sources: MT5 Bridge, pandas-ta, Twelve Data, FRED, CNN Fear & Greed, DXY
"""
import os, json, time, requests, logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ─── Config ───────────────────────────────────────────────────
MT5_API       = "https://api.api2trade.com"
MT5_ID        = "YOUR_API2TRADE_ACCOUNT_UUID"
MT5_KEY       = "YOUR_API2TRADE_API_KEY"
OPENAI_KEY    = os.getenv("OPENAI_API_KEY")
TG_TOKEN      = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID", "")
TWELVE_KEY    = os.getenv("TWELVE_DATA_KEY", "73547e10ec7c413e98fbc2e3a62a9096")
FRED_KEY      = os.getenv("FRED_API_KEY", "7172fcbab0fe6ac3079f24addf311dc0")

SYMBOLS       = ["EURUSDxx", "XAUUSDxx", "GBPUSDxx", "GBPJPYxx", "USDJPYxx"]
TD_SYMBOLS    = {"EURUSDxx":"EUR/USD","XAUUSDxx":"XAU/USD","GBPUSDxx":"GBP/USD","GBPJPYxx":"GBP/JPY","USDJPYxx":"USD/JPY"}
RISK_PCT      = 0.01
JOURNAL       = Path("/var/log/hermes/trade_journal.jsonl")
CACHE_FILE    = Path("/tmp/genesis_cache.json")

# Full symbol map: MT5 broker symbol → Yahoo Finance ticker
YF_MAP = {
    "EURUSDxx": "EURUSD=X",  "GBPUSDxx": "GBPUSD=X",  "USDJPYxx": "USDJPY=X",
    "XAUUSDxx": "GC=F",       "GBPJPYxx": "GBPJPY=X",  "AUDUSDxx": "AUDUSD=X",
    "USDCHFxx": "USDCHF=X",  "USDCADxx": "USDCAD=X",  "EURJPYxx": "EURJPY=X",
    "NZDUSDxx": "NZDUSD=X",  "EURGBPxx": "EURGBP=X",  "XAGUSDxx": "SI=F",
    "USOILxx":  "CL=F",       "NAS100xx": "NQ=F",       "US30xx":   "YM=F",
    "SPX500xx": "ES=F",       "GER40xx":  "FDAX=F",     "BTCUSDxx": "BTC-USD",
    "ETHUSDxx": "ETH-USD",
}
# Resolve safe log path (fallback to local logs/ if system dir not writable)
default_log = "/var/log/hermes/trading_cycle.log"
try:
    Path(default_log).parent.mkdir(parents=True, exist_ok=True)
    log_file = default_log
except Exception:
    local_log_dir = Path(__file__).parents[1] / "logs" / "hermes"
    local_log_dir.mkdir(parents=True, exist_ok=True)
    log_file = str(local_log_dir / "trading_cycle.log")

logging.basicConfig(
    filename=log_file,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger(__name__)

# ─── Cache (avoid hammering external APIs) ────────────────────
def load_cache():
    try:
        return json.loads(CACHE_FILE.read_text()) if CACHE_FILE.exists() else {}
    except: return {}

def save_cache(c): CACHE_FILE.write_text(json.dumps(c))

# ─── Helpers ──────────────────────────────────────────────────
def tg(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json={"chat_id": TG_CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e: log.error(f"TG: {e}")

def calc_risk_eur(equity: float) -> float:
    """Dynamic 1% risk sizing — grows with account, compounds automatically."""
    return round(equity * RISK_PCT, 2)

# ─── Phase 1: Pre-flight Health Check ─────────────────────────
def preflight_check() -> bool:
    """Verify all critical systems are alive before any trading logic runs."""
    failures = []

    # Check 1: Local MT5 Bridge
    try:
        r = requests.get(f"{MT5_API}/AccountSummary", params={"id": MT5_ID}, auth=MT5_AUTH, timeout=5)
        if r.status_code != 200:
            failures.append(f"Bridge HTTP {r.status_code}")
    except Exception as e:
        failures.append(f"Bridge unreachable: {e}")

    # Check 2: MT5 API (direct)
    try:
        r = requests.get(f"{MT5_API}/news", timeout=5)
        if r.status_code != 200:
            failures.append(f"MT5 API HTTP {r.status_code}")
    except Exception as e:
        failures.append(f"MT5 API unreachable: {e}")

    # Check 3: LLM Proxy
    try:
        r = requests.get("http://127.0.0.1:9999/health", timeout=5)
        # Nginx proxy may return 404 on /health but that still means it's up
    except Exception as e:
        failures.append(f"LLM proxy unreachable: {e}")

    if failures:
        msg = "⚠️ *GENESIS PRE-FLIGHT FAILED*\n" + "\n".join(f"- {f}" for f in failures)
        log.error(f"Pre-flight failures: {failures}")
        tg(msg)
        return False

    log.info("Pre-flight: all systems nominal")
    return True

def bridge(path, method="GET", data=None):
    try:
        url = f"{BRIDGE}{path}"
        r = requests.post(url, json=data, timeout=15) if method == "POST" else requests.get(url, timeout=15)
        return r.json()
    except Exception as e: return {"error": str(e)}

# ─── Phase 2: Emergency Close (retry loop for open positions) ──
def emergency_close_with_retry(ticket: str, symbol: str):
    """If bridge drops while a trade is open, retry for 5 min then alert."""
    for attempt in range(10):  # 10 x 30s = 5 minutes
        try:
            result = bridge("/close", "POST", {"ticket": ticket})
            if result.get("message") == "ok" or result.get("ticket"):
                log.info(f"Emergency close succeeded on attempt {attempt+1}")
                tg(f"🚨 *GENESIS EMERGENCY CLOSE*: {symbol} closed after {attempt+1} retries.")
                return True
        except Exception as e:
            log.error(f"Emergency close attempt {attempt+1} failed: {e}")
        time.sleep(30)
    tg(f"🚨 *GENESIS CRITICAL*: Cannot close {symbol} (ticket {ticket}) after 5min of retries.\nManual intervention required NOW!")
    return False



YF_TF = {"M15": "15m", "H1": "1h", "H4": "4h", "D1": "1d"}

# ─── yfinance Multi-Timeframe Bars (MT5 Bars API is VPS IP-blocked) ──
def get_bars(symbol, tf="H1", count=100):
    try:
        import yfinance as yf, pandas as pd
        yf_sym  = YF_MAP.get(symbol, symbol.replace("xx", "=X"))
        interval = YF_TF.get(tf, "1h")
        period   = {"15m": "5d", "1h": "60d", "4h": "60d", "1d": "365d"}.get(interval, "60d")
        df = yf.download(yf_sym, period=period, interval=interval, progress=False, auto_adjust=True)
        if df.empty: return []
        if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
        df.columns = [c.lower() for c in df.columns]
        df = df.rename(columns={"adj close": "close"})
        df = df.dropna().tail(count).reset_index()
        return df.to_dict("records")
    except Exception as e:
        log.error(f"get_bars {symbol}/{tf}: {e}")
        return []

# ─── Currency Strength Index (all 8 majors via yfinance, cached 10min) ────
def get_currency_strength():
    cache = load_cache()
    now   = time.time()
    if "cs" in cache and now - cache["cs"].get("ts", 0) < 600:
        return cache["cs"].get("data", {})
    try:
        import yfinance as yf, pandas as pd
        pairs = {
            "EURUSD=X": ("EUR","USD"), "GBPUSD=X": ("GBP","USD"), "USDJPY=X": ("USD","JPY"),
            "USDCHF=X": ("USD","CHF"), "AUDUSD=X": ("AUD","USD"), "USDCAD=X": ("USD","CAD"),
            "NZDUSD=X": ("NZD","USD"), "EURGBP=X": ("EUR","GBP"), "EURJPY=X": ("EUR","JPY"),
            "GBPJPY=X": ("GBP","JPY"), "AUDJPY=X": ("AUD","JPY"), "CADJPY=X": ("CAD","JPY"),
        }
        tickers = list(pairs.keys())
        hist = yf.download(tickers, period="2d", interval="1h", progress=False, auto_adjust=True)
        closes = hist["Close"] if "Close" in hist else hist
        if isinstance(closes.columns, pd.MultiIndex): closes.columns = closes.columns.get_level_values(0)
        strength = {c: 0.0 for c in ["EUR","GBP","USD","JPY","CHF","AUD","CAD","NZD"]}
        counts   = {c: 0 for c in strength}
        for ticker, (base, quote) in pairs.items():
            if ticker not in closes.columns: continue
            pct = closes[ticker].pct_change(periods=4).iloc[-1]
            if pd.isna(pct): continue
            strength[base] = strength.get(base, 0) + float(pct)
            strength[quote] = strength.get(quote, 0) - float(pct)
            counts[base] = counts.get(base, 0) + 1
            counts[quote] = counts.get(quote, 0) + 1
        result = {c: round(strength[c]/counts[c]*100, 3) if counts[c] > 0 else 0 for c in strength}
        cache["cs"] = {"ts": now, "data": result}
        save_cache(cache)
        return result
    except Exception as e:
        log.error(f"CurrencyStrength: {e}")
        return {}

# ─── Multi-asset snapshot via yfinance (indices, gold, oil, crypto) ──
def get_asset_snapshot():
    cache = load_cache()
    now   = time.time()
    if "snap" in cache and now - cache["snap"].get("ts", 0) < 300:
        return cache["snap"].get("data", {})
    try:
        import yfinance as yf
        tickers = {"SPX": "^GSPC", "NASDAQ": "^IXIC", "DOW": "^DJI", "VIX": "^VIX",
                   "GOLD": "GC=F", "OIL": "CL=F", "DXY": "DX-Y.NYB",
                   "BTC": "BTC-USD", "SILVER": "SI=F", "BONDS_10Y": "^TNX"}
        result = {}
        for name, sym in tickers.items():
            try:
                h = yf.Ticker(sym).history(period="2d", interval="1h")
                if not h.empty:
                    c = float(h["Close"].iloc[-1])
                    prev = float(h["Close"].iloc[-2]) if len(h) > 1 else c
                    result[name] = {"price": round(c, 4), "change_pct": round((c-prev)/prev*100, 3)}
            except: pass
        cache["snap"] = {"ts": now, "data": result}
        save_cache(cache)
        return result
    except Exception as e:
        log.error(f"AssetSnapshot: {e}")
        return {}



# ─── CFTC COT Report (Hedge Fund Positioning, weekly, cached 6h) ──────────────
def get_cot_positioning():
    """Download CFTC Commitment of Traders (Leveraged Money = hedge funds).
    This shows HOW hedge funds are positioned — the single highest quality
    directional signal for medium-term forex forecasting."""
    cache = load_cache()
    now   = time.time()
    if "cot" in cache and now - cache["cot"].get("ts", 0) < 21600:
        return cache["cot"].get("data", {})
    try:
        import zipfile, io, pandas as pd
        year = datetime.now(timezone.utc).strftime("%Y")
        url  = f"https://www.cftc.gov/files/dea/history/fut_fin_xls_{year}.zip"
        r    = requests.get(url, timeout=30)
        if r.status_code != 200:
            return {}
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            fname = [f for f in z.namelist() if f.endswith(".xls")][0]
            df    = pd.read_excel(io.BytesIO(z.read(fname)), engine="xlrd")

        date_col  = "As_of_Date_In_Form_YYMMDD"
        name_col  = "Market_and_Exchange_Names"
        long_col  = "Lev_Money_Positions_Long_All"
        short_col = "Lev_Money_Positions_Short_All"
        chg_l_col = "Change_in_Lev_Money_Long_All"
        chg_s_col = "Change_in_Lev_Money_Short_All"

        latest   = df[date_col].max()
        df_latest = df[df[date_col] == latest]
        report_date = str(latest)

        result = {"report_date": report_date}
        pairs_map = {
            "EURO FX": "EUR", "BRITISH POUND": "GBP", "JAPANESE YEN": "JPY",
            "SWISS FRANC": "CHF", "CANADIAN DOLLAR": "CAD", "AUSTRALIAN": "AUD",
            "NZ DOLLAR": "NZD", "GOLD - COMMODITY": "XAU",
        }
        for keyword, ccy in pairs_map.items():
            rows = df_latest[df_latest[name_col].str.contains(keyword, case=False, na=False)]
            if rows.empty: continue
            r_row = rows.iloc[0]
            try:
                longs  = int(r_row[long_col])
                shorts = int(r_row[short_col])
                chg_l  = int(r_row[chg_l_col])
                chg_s  = int(r_row[chg_s_col])
                net    = longs - shorts
                total  = longs + shorts
                bull_pct = round(longs / total * 100, 1) if total > 0 else 50
                result[ccy] = {
                    "longs": longs, "shorts": shorts, "net": net,
                    "bull_pct": bull_pct,
                    "wk_change": chg_l - chg_s,   # net change this week
                    "bias": "bullish" if net > 0 else "bearish",
                }
            except: pass
        cache["cot"] = {"ts": now, "data": result}
        save_cache(cache)
        log.info(f"COT loaded: {report_date} | {len(result)-1} instruments")
        return result
    except Exception as e:
        log.error(f"COT: {e}")
        return {}

# ─── Interest Rate Differentials (2Y bond yields, cached 6h) ─────────────────
def get_rate_differentials():
    """2-year government bond yield differentials drive short-term FX flows.
    The pair with the highest positive differential attracts carry trade inflows."""
    cache = load_cache()
    now   = time.time()
    if "rates" in cache and now - cache["rates"].get("ts", 0) < 21600:
        return cache["rates"].get("data", {})
    # FRED series IDs for 2Y government bond yields
    series = {
        "USD": "DGS2",          # US 2Y Treasury
        "EUR": "IRLTLT01EZM156N", # Euro area 10Y (2Y not available, use as proxy)
        "GBP": "IRLTLT01GBM156N", # UK
        "JPY": "IRLTLT01JPM156N", # Japan
        "CHF": "IRLTLT01CHM156N", # Switzerland
        "CAD": "IRLTLT01CAM156N", # Canada
        "AUD": "IRLTLT01AUM156N", # Australia
    }
    yields = {}
    for ccy, sid in series.items():
        val = get_fred(sid)
        if val is not None:
            yields[ccy] = round(float(val), 3)

    # Compute differentials for major pairs (base - quote)
    diffs = {}
    pair_map = {
        "EURUSD": ("USD", "EUR"), "GBPUSD": ("USD", "GBP"),
        "USDJPY": ("JPY", "USD"), "USDCHF": ("CHF", "USD"),
        "USDCAD": ("CAD", "USD"), "AUDUSD": ("USD", "AUD"),
    }
    for pair, (quote_ccy, base_ccy) in pair_map.items():
        if base_ccy in yields and quote_ccy in yields:
            diff = round(yields[base_ccy] - yields[quote_ccy], 3)
            diffs[pair] = {"diff_pct": diff,
                           "favors": base_ccy if diff > 0 else quote_ccy}

    result = {"yields": yields, "differentials": diffs}
    cache["rates"] = {"ts": now, "data": result}
    save_cache(cache)
    return result

# ─── Key Price Levels (prev day/week OHLC — magnetic prices) ─────────────────
def get_key_levels(symbol):
    """Previous day and previous week OHLC. These are the most watched price
    levels by institutional traders. Markets frequently revisit them."""
    try:
        import yfinance as yf, pandas as pd
        yf_sym = YF_MAP.get(symbol, symbol.replace("xx", "=X"))
        df = yf.download(yf_sym, period="10d", interval="1d",
                         progress=False, auto_adjust=True)
        if df.empty or len(df) < 3: return {}
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.columns = [c.lower() for c in df.columns]
        df = df.dropna()
        prev_day  = df.iloc[-2]
        prev_week_df = df.iloc[-6:-1]  # last 5 trading days = last week
        return {
            "prev_day_high":  round(float(prev_day["high"]), 5),
            "prev_day_low":   round(float(prev_day["low"]), 5),
            "prev_day_close": round(float(prev_day["close"]), 5),
            "prev_week_high": round(float(prev_week_df["high"].max()), 5),
            "prev_week_low":  round(float(prev_week_df["low"].min()), 5),
            "week_range_pct": round((prev_week_df["high"].max() - prev_week_df["low"].min()) / prev_week_df["close"].mean() * 100, 3),
        }
    except Exception as e:
        log.error(f"KeyLevels {symbol}: {e}")
        return {}

# ─── Pair Correlation Matrix (20-day rolling, cached 30min) ──────────────────
def get_correlations():
    """20-day rolling correlation between major pairs.
    Helps Hermes avoid taking the same directional bet twice (e.g. long EURUSD
    AND long GBPUSD when they're 95% correlated)."""
    cache = load_cache()
    now   = time.time()
    if "corr" in cache and now - cache["corr"].get("ts", 0) < 1800:
        return cache["corr"].get("data", {})
    try:
        import yfinance as yf, pandas as pd
        syms = {"EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X", "USDJPY": "USDJPY=X",
                "XAUUSD": "GC=F",     "GBPJPY": "GBPJPY=X", "AUDUSD": "AUDUSD=X"}
        hist = yf.download(list(syms.values()), period="30d", interval="1d",
                           progress=False, auto_adjust=True)
        closes = hist["Close"] if "Close" in hist else hist
        if isinstance(closes.columns, pd.MultiIndex):
            closes.columns = closes.columns.get_level_values(0)
        closes = closes.rename(columns={v: k for k, v in syms.items()})
        corr_matrix = closes.pct_change().dropna().tail(20).corr()
        result = {}
        pairs = list(syms.keys())
        for i, p1 in enumerate(pairs):
            for p2 in pairs[i+1:]:
                if p1 in corr_matrix.columns and p2 in corr_matrix.columns:
                    c = round(float(corr_matrix.loc[p1, p2]), 3)
                    result[f"{p1}/{p2}"] = c
        cache["corr"] = {"ts": now, "data": result}
        save_cache(cache)
        return result
    except Exception as e:
        log.error(f"Correlations: {e}")
        return {}

# ─── ForexFactory Economic Calendar (this week + next week) ─────────────────
def get_forex_calendar():
    """Real economic calendar from ForexFactory JSON feed.
    Far more detailed than MT5 news: includes forecast vs actual vs previous,
    currency tag, and impact level for every event this and next week."""
    cache = load_cache()
    now   = time.time()
    if "ff_cal" in cache and now - cache["ff_cal"].get("ts", 0) < 1800:
        return cache["ff_cal"].get("data", [])
    events = []
    for period in ["thisweek", "nextweek"]:
        try:
            r = requests.get(f"https://nfs.faireconomy.media/ff_calendar_{period}.json",
                             timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200:
                events.extend(r.json())
        except: pass
    cache["ff_cal"] = {"ts": now, "data": events}
    save_cache(cache)
    return events

# ─── Sentiment Aggregator (multi-source, cached 15 min) ───────────────────────
def get_sentiment_dashboard():
    """Aggregates sentiment signals from multiple independent sources into
    a unified dashboard. Hermes uses this to gauge overall market mood."""
    cache = load_cache()
    now   = time.time()
    if "sent" in cache and now - cache["sent"].get("ts", 0) < 900:
        return cache["sent"].get("data", {})
    result = {}

    # 1. Crypto Fear & Greed (5-day trend — risk-on/off proxy)
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=5", timeout=8)
        items = r.json().get("data", [])
        if items:
            scores = [int(x["value"]) for x in items]
            result["crypto_fg_today"]    = scores[0]
            result["crypto_fg_label"]    = items[0]["value_classification"]
            result["crypto_fg_trend"]    = "improving" if scores[0] > scores[-1] else "deteriorating"
            result["crypto_fg_3d_avg"]   = round(sum(scores[:3]) / 3, 1)
    except: pass

    # 2. CME Currency Futures Volume (vs 5-day avg — unusual volume = conviction)
    try:
        import yfinance as yf
        futures = {"EUR": "6E=F", "GBP": "6B=F", "JPY": "6J=F", "AUD": "6A=F", "GOLD": "GC=F"}
        vol_ratios = {}
        for ccy, sym in futures.items():
            h = yf.Ticker(sym).history(period="6d", interval="1d")
            if not h.empty and len(h) >= 2:
                today_vol = float(h["Volume"].iloc[-1])
                avg_vol   = float(h["Volume"].iloc[:-1].mean())
                if avg_vol > 0:
                    vol_ratios[ccy] = round(today_vol / avg_vol, 2)
        result["futures_volume_ratios"] = vol_ratios
        # Flag any unusually high volume (>2x avg = strong conviction)
        high_vol = [f"{c}:{r}x" for c, r in vol_ratios.items() if r > 2.0]
        result["high_volume_conviction"] = high_vol if high_vol else ["none"]
    except: pass

    # 3. Volatility regime (VIX level interpretation)
    try:
        import yfinance as yf
        vix_h = yf.Ticker("^VIX").history(period="5d", interval="1d")
        if not vix_h.empty:
            vix_now  = float(vix_h["Close"].iloc[-1])
            vix_prev = float(vix_h["Close"].iloc[-2]) if len(vix_h) > 1 else vix_now
            result["vix_live"]   = round(vix_now, 2)
            result["vix_change"] = round(vix_now - vix_prev, 2)
            result["vix_regime"] = "extreme_fear" if vix_now > 35 else ("high_vol" if vix_now > 25 else ("elevated" if vix_now > 18 else "calm"))
    except: pass

    # 4. Gold/JPY safe-haven demand (risk-off indicator)
    try:
        import yfinance as yf
        for sym, name in [("GC=F", "gold_1d_pct"), ("USDJPY=X", "jpy_1d_pct")]:
            h = yf.Ticker(sym).history(period="3d", interval="1d")
            if len(h) >= 2:
                pct = (float(h["Close"].iloc[-1]) - float(h["Close"].iloc[-2])) / float(h["Close"].iloc[-2]) * 100
                result[name] = round(pct, 3)
        # Rising gold + falling USDJPY = risk-off
        if "gold_1d_pct" in result and "jpy_1d_pct" in result:
            risk_off_score = result["gold_1d_pct"] - result["jpy_1d_pct"]
            result["risk_off_score"] = round(risk_off_score, 3)
            result["risk_sentiment"] = "risk_off" if risk_off_score > 0.3 else ("risk_on" if risk_off_score < -0.3 else "neutral")
    except: pass

    cache["sent"] = {"ts": now, "data": result}
    save_cache(cache)
    return result

def get_news():
    try:
        r = requests.get("https://api.api2trade.com/news", timeout=10)
        return r.json() if r.status_code == 200 else []
    except: return []

# ─── Technical Analysis (pandas-ta from MT5 bars) ─────────────
def compute_indicators(bars):
    """Compute a full suite of technical indicators from raw OHLCV bars."""
    if len(bars) < 30:
        return {}
    try:
        import pandas as pd
        import ta
        df = pd.DataFrame(bars)
        df.columns = [c.lower() for c in df.columns]
        df = df.rename(columns={"tickvolume": "volume"})
        for col in ["close", "high", "low", "open"]:
            df[col] = df[col].astype(float)
        if "volume" not in df.columns:
            df["volume"] = 1.0
        df["volume"] = df["volume"].astype(float)

        # ── Core Momentum ──────────────────────────────────────────
        rsi         = ta.momentum.rsi(df["close"], window=14)
        williams_r  = ta.momentum.williams_r(df["high"], df["low"], df["close"], lbp=14)
        cci         = ta.trend.cci(df["high"], df["low"], df["close"], window=20)
        stoch_k     = ta.momentum.stoch(df["high"], df["low"], df["close"], window=14)
        stoch_d     = ta.momentum.stoch_signal(df["high"], df["low"], df["close"], window=14)

        # ── Trend ──────────────────────────────────────────────────
        macd        = ta.trend.macd(df["close"])
        macd_signal = ta.trend.macd_signal(df["close"])
        macd_hist   = ta.trend.macd_diff(df["close"])
        ema20       = ta.trend.ema_indicator(df["close"], window=20)
        ema50       = ta.trend.ema_indicator(df["close"], window=50)
        ema200      = ta.trend.ema_indicator(df["close"], window=200)
        adx         = ta.trend.adx(df["high"], df["low"], df["close"], window=14)
        adx_pos     = ta.trend.adx_pos(df["high"], df["low"], df["close"], window=14)
        adx_neg     = ta.trend.adx_neg(df["high"], df["low"], df["close"], window=14)
        psar        = ta.trend.psar_down(df["high"], df["low"], df["close"])  # Parabolic SAR

        # ── Volatility ─────────────────────────────────────────────
        bb_upper    = ta.volatility.bollinger_hband(df["close"], window=20)
        bb_lower    = ta.volatility.bollinger_lband(df["close"], window=20)
        bb_mid      = ta.volatility.bollinger_mavg(df["close"], window=20)
        bb_pct      = ta.volatility.bollinger_pband(df["close"], window=20)
        atr         = ta.volatility.average_true_range(df["high"], df["low"], df["close"], window=14)
        keltner_u   = ta.volatility.keltner_channel_hband(df["high"], df["low"], df["close"])
        keltner_l   = ta.volatility.keltner_channel_lband(df["high"], df["low"], df["close"])

        # ── Volume ─────────────────────────────────────────────────
        obv         = ta.volume.on_balance_volume(df["close"], df["volume"])

        # ── Ichimoku Cloud ─────────────────────────────────────────
        ich_conv    = ta.trend.ichimoku_conversion_line(df["high"], df["low"])   # Tenkan-sen
        ich_base    = ta.trend.ichimoku_base_line(df["high"], df["low"])          # Kijun-sen
        ich_a       = ta.trend.ichimoku_a(df["high"], df["low"])                  # Senkou A
        ich_b       = ta.trend.ichimoku_b(df["high"], df["low"])                  # Senkou B

        # ── Pivot Points (classic daily pivots) ────────────────────
        pp    = (df["high"].iloc[-2] + df["low"].iloc[-2] + df["close"].iloc[-2]) / 3
        r1    = 2 * pp - df["low"].iloc[-2]
        s1    = 2 * pp - df["high"].iloc[-2]
        r2    = pp + (df["high"].iloc[-2] - df["low"].iloc[-2])
        s2    = pp - (df["high"].iloc[-2] - df["low"].iloc[-2])

        last = -1
        close_last = float(df["close"].iloc[last])

        # Ichimoku cloud position
        ich_cloud_top    = max(float(ich_a.iloc[last] or 0), float(ich_b.iloc[last] or 0))
        ich_cloud_bottom = min(float(ich_a.iloc[last] or 0), float(ich_b.iloc[last] or 0))
        ich_position     = "above_cloud" if close_last > ich_cloud_top else ("below_cloud" if close_last < ich_cloud_bottom else "in_cloud")

        def safe(series): return round(float(series.iloc[last]), 5) if series is not None and not series.isna().all() else None

        return {
            # Momentum
            "rsi":         safe(rsi),
            "williams_r":  safe(williams_r),
            "cci":         safe(cci),
            "stoch_k":     safe(stoch_k),
            "stoch_d":     safe(stoch_d),
            # Trend
            "macd":        safe(macd),
            "macd_signal": safe(macd_signal),
            "macd_hist":   safe(macd_hist),
            "ema20":       safe(ema20),
            "ema50":       safe(ema50),
            "ema200":      safe(ema200),
            "adx":         safe(adx),
            "adx_plus":    safe(adx_pos),
            "adx_minus":   safe(adx_neg),
            "psar":        safe(psar),
            # Volatility
            "bb_upper":    safe(bb_upper),
            "bb_lower":    safe(bb_lower),
            "bb_mid":      safe(bb_mid),
            "bb_pct":      safe(bb_pct),   # 0=at lower band, 1=at upper band
            "atr":         safe(atr),
            "keltner_u":   safe(keltner_u),
            "keltner_l":   safe(keltner_l),
            # Volume
            "obv":         safe(obv),
            # Ichimoku
            "ich_conv":    safe(ich_conv),
            "ich_base":    safe(ich_base),
            "ich_a":       safe(ich_a),
            "ich_b":       safe(ich_b),
            "ich_position": ich_position,
            # Pivot Points
            "pivot":       round(pp, 5),
            "r1":          round(r1, 5),
            "s1":          round(s1, 5),
            "r2":          round(r2, 5),
            "s2":          round(s2, 5),
            # Price
            "close":       round(close_last, 5),
            "trend":       "bullish" if float(ema20.iloc[last]) > float(ema50.iloc[last]) else "bearish",
        }
    except Exception as e:
        log.error(f"compute_indicators error: {e}")
        return {}

# ─── Twelve Data (5 indicators, cached 15 min) ─────────────────
def get_twelve_data(symbol):
    td_sym = TD_SYMBOLS.get(symbol)
    if not td_sym: return {}
    cache = load_cache()
    key   = f"td_{symbol}"
    now   = time.time()
    if key in cache and now - cache[key].get("ts", 0) < 900:
        return cache[key].get("data", {})
    data = {}
    try:
        # RSI
        r = requests.get("https://api.twelvedata.com/rsi", timeout=10,
                         params={"symbol": td_sym, "interval": "1h", "apikey": TWELVE_KEY, "outputsize": 1})
        rsi_val = r.json().get("values", [{}])[0].get("rsi") if r.status_code == 200 else None
        if rsi_val: data["td_rsi_1h"] = round(float(rsi_val), 2)
    except: pass
    try:
        # MACD
        r = requests.get("https://api.twelvedata.com/macd", timeout=10,
                         params={"symbol": td_sym, "interval": "1h", "apikey": TWELVE_KEY, "outputsize": 1})
        mv = r.json().get("values", [{}])[0] if r.status_code == 200 else {}
        if mv.get("macd"): data["td_macd"] = round(float(mv["macd"]), 5)
        if mv.get("macd_signal"): data["td_macd_signal"] = round(float(mv["macd_signal"]), 5)
    except: pass
    try:
        # ADX (trend strength)
        r = requests.get("https://api.twelvedata.com/adx", timeout=10,
                         params={"symbol": td_sym, "interval": "1h", "apikey": TWELVE_KEY, "outputsize": 1})
        adx_val = r.json().get("values", [{}])[0].get("adx") if r.status_code == 200 else None
        if adx_val: data["td_adx_1h"] = round(float(adx_val), 2)
    except: pass
    try:
        # Stochastic
        r = requests.get("https://api.twelvedata.com/stoch", timeout=10,
                         params={"symbol": td_sym, "interval": "1h", "apikey": TWELVE_KEY, "outputsize": 1})
        sv = r.json().get("values", [{}])[0] if r.status_code == 200 else {}
        if sv.get("slow_k"): data["td_stoch_k"] = round(float(sv["slow_k"]), 2)
        if sv.get("slow_d"): data["td_stoch_d"] = round(float(sv["slow_d"]), 2)
    except: pass
    cache[key] = {"ts": now, "data": data}
    save_cache(cache)
    return data

# ─── CNN Fear & Greed (cached 30 min) ─────────────────────────
def get_fear_greed():
    cache = load_cache()
    now   = time.time()
    if "fg" in cache and now - cache["fg"].get("ts", 0) < 1800:
        return cache["fg"].get("data", {})
    try:
        r = requests.get("https://production.dataviz.cnn.io/index/fearandgreed/graphdata", timeout=10,
                         headers={"User-Agent": "Mozilla/5.0"})
        d = r.json()
        score = d["fear_and_greed"]["score"]
        rating = d["fear_and_greed"]["rating"]
        data = {"fear_greed_score": round(score, 1), "fear_greed_rating": rating}
        cache["fg"] = {"ts": now, "data": data}
        save_cache(cache)
        return data
    except Exception as e:
        log.error(f"Fear&Greed: {e}")
        return {}

# ─── FRED Macroeconomic Data (cached 6 hours) ─────────────────
def get_fred(series_id):
    cache = load_cache()
    key   = f"fred_{series_id}"
    now   = time.time()
    if key in cache and now - cache[key].get("ts", 0) < 21600:
        return cache[key].get("val")
    try:
        r = requests.get("https://api.stlouisfed.org/fred/series/observations", timeout=10,
                         params={"series_id": series_id, "api_key": FRED_KEY,
                                 "sort_order": "desc", "limit": 1, "file_type": "json"})
        val = float(r.json()["observations"][0]["value"])
        cache[key] = {"ts": now, "val": val}
        save_cache(cache)
        return val
    except: return None

def get_macro():
    return {
        "fed_funds_rate":      get_fred("DFF"),
        "yield_curve_10y2y":   get_fred("T10Y2Y"),
        "yield_curve_10y3m":   get_fred("T10Y3M"),
        "vix":                 get_fred("VIXCLS"),
        "us_cpi_yoy":          get_fred("CPIAUCSL"),
        "us_unemployment":     get_fred("UNRATE"),
        "us_gdp_growth":       get_fred("A191RL1Q225SBEA"),
        "eur_cpi":             get_fred("CP0000EZ19M086NEST"),
        "us_m2_money_supply":  get_fred("M2SL"),
        "us_retail_sales_mom": get_fred("RSXFS"),
    }

# ─── DXY via yfinance (cached 10 min) ─────────────────────────
def get_dxy():
    cache = load_cache()
    now   = time.time()
    if "dxy" in cache and now - cache["dxy"].get("ts", 0) < 600:
        return cache["dxy"].get("val")
    try:
        import yfinance as yf
        dxy = yf.Ticker("DX-Y.NYB")
        hist = dxy.history(period="2d", interval="1h")
        if not hist.empty:
            val = round(float(hist["Close"].iloc[-1]), 3)
            cache["dxy"] = {"ts": now, "val": val}
            save_cache(cache)
            return val
    except Exception as e:
        log.error(f"DXY: {e}")
    return None

# ─── Trade Journal ────────────────────────────────────────────
def journal_write(entry: dict):
    with open(JOURNAL, "a") as f:
        f.write(json.dumps(entry) + "\n")

def journal_read_last(n=10):
    if not JOURNAL.exists(): return []
    lines = JOURNAL.read_text().strip().split("\n")
    return [json.loads(l) for l in lines[-n:] if l]

def update_journal_results(closed_orders):
    """Match closed orders to open journal entries and update P&L."""
    if not JOURNAL.exists(): return
    lines = JOURNAL.read_text().strip().split("\n")
    updated = []
    closed_tickets = {str(o.get("ticket")): o for o in closed_orders if isinstance(o, dict)}
    for line in lines:
        if not line: continue
        try:
            entry = json.loads(line)
            ticket = str(entry.get("ticket"))
            if ticket in closed_tickets and "result" not in entry:
                o = closed_tickets[ticket]
                entry["result"] = "win" if o.get("profit", 0) > 0 else "loss"
                entry["pnl"]    = round(o.get("profit", 0), 2)
                entry["closed"] = True
        except: pass
        updated.append(json.dumps(entry))
    JOURNAL.write_text("\n".join(updated) + "\n")

# ─── Main Cycle ───────────────────────────────────────────────
def run_cycle():
    now_utc = datetime.now(timezone.utc)
    log.info(f"=== Cycle {now_utc.strftime('%Y-%m-%d %H:%M')} ===")

    # Market hours check
    wd, hr = now_utc.weekday(), now_utc.hour
    if (wd == 4 and hr >= 22) or wd == 5 or (wd == 6 and hr < 22):
        log.info("Market closed — weekend. Skipping.")
        return

    # ── Phase 1: Pre-flight health check before ANY trading logic ──
    if not preflight_check():
        return  # Alert already sent inside preflight_check()

    # 1. Account (real data)
    account = bridge("/balance")
    if "error" in account:
        tg("⚠️ *GENESIS*: Bridge unreachable!")
        return
    balance = account.get("balance", 0)
    equity  = account.get("equity", 0)
    max_risk_eur = calc_risk_eur(equity)

    # 2. Open positions
    positions = bridge("/positions")
    if isinstance(positions, list) and len(positions) > 0:
        pos = positions[0]
        log.info(f"Position open — skipping new trade. P&L: {pos.get('profit')}")
        # ── Phase 2: Guard open trade — if bridge becomes unreachable, emergency close
        if "error" in bridge("/balance"):  # double-check bridge is alive
            emergency_close_with_retry(str(pos.get("ticket")), pos.get("symbol", ""))
        return

    # 3. Update journal with closed orders
    try:
        today    = now_utc.strftime("%Y-%m-%dT00:00:00")
        tomorrow = (now_utc.replace(hour=0, minute=0, second=0) + timedelta(days=2)).strftime("%Y-%m-%dT00:00:00")
        closed   = requests.get(f"{MT5_API}/ClosedOrders",
                               params={"id": MT5_ID, "from": today, "to": tomorrow},
                               headers={"x-api-key": MT5_KEY}, timeout=15).json()
        if isinstance(closed, list):
            update_journal_results(closed)
    except Exception as e:
        log.error(f"ClosedOrders fetch failed: {e}")

    # 4. Economic calendar
    news = get_news()
    blocked_ccys, upcoming_high = set(), []
    for evt in news:
        try:
            et = datetime.fromisoformat(evt["date"]).astimezone(timezone.utc)
            mins = (et - now_utc).total_seconds() / 60
            if evt.get("impact") == "High" and -30 < mins < 120:
                blocked_ccys.add(evt.get("country", ""))
                upcoming_high.append(f"{evt['title']} ({evt['country']}) in {int(mins)}min")
        except: pass

    # 5. Full intelligence layer
    macro       = get_macro()
    dxy         = get_dxy()
    fg          = get_fear_greed()
    cstrength    = get_currency_strength()
    assets       = get_asset_snapshot()
    cot          = get_cot_positioning()
    rates        = get_rate_differentials()
    correlations = get_correlations()
    sentiment    = get_sentiment_dashboard()
    ff_calendar  = get_forex_calendar()
    log.info(f"Intelligence loaded | VIX={sentiment.get('vix_live')} | CryptoFG={sentiment.get('crypto_fg_today')} | Risk={sentiment.get('risk_sentiment')}")

    # 6. Trade history (last 20 for learning)
    past_trades = journal_read_last(20)
    wins   = sum(1 for t in past_trades if t.get("result") == "win")
    losses = sum(1 for t in past_trades if t.get("result") == "loss")

    # 7. Expand symbol universe from MT5 + multi-TF indicators
    available_symbols = bridge("/symbols")
    if isinstance(available_symbols, list):
        tradeable = [str(s) for s in available_symbols
                     if any(k in str(s) for k in ["USD","EUR","GBP","JPY","XAU","XAG","NAS","US30","SPX","GER","BTC","ETH","OIL"])]
        scan_symbols = list(dict.fromkeys(SYMBOLS + tradeable[:6]))
    else:
        scan_symbols = SYMBOLS

    market_data = {}
    for sym in scan_symbols:
        quote = bridge(f"/quote?symbol={sym}")
        if "error" in quote or not quote.get("bid"): continue

        bars_m15 = get_bars(sym, "M15", 100)
        bars_h1  = get_bars(sym, "H1",  100)
        bars_h4  = get_bars(sym, "H4",  80)
        bars_d1  = get_bars(sym, "D1",  60)
        ind_m15  = compute_indicators(bars_m15)
        ind_h1   = compute_indicators(bars_h1)
        ind_h4   = compute_indicators(bars_h4)
        ind_d1   = compute_indicators(bars_d1)
        td_data    = get_twelve_data(sym)
        key_levels = get_key_levels(sym)

        market_data[sym] = {
            "bid":        quote.get("bid"),
            "ask":        quote.get("ask"),
            "spread":     round((quote.get("ask", 0) - quote.get("bid", 0)) * 10000, 1),
            "m15":        ind_m15,
            "h1":         ind_h1,
            "h4":         ind_h4,
            "d1":         ind_d1,
            "key_levels": key_levels,
            "twelve_data": td_data,
        }
        time.sleep(0.3)

    if not market_data:
        log.error("No market data")
        return


    # 9. Build prompt — rich intelligence feed, zero rules
    strongest  = sorted(cstrength.items(), key=lambda x: x[1], reverse=True)[:3] if cstrength else []
    weakest    = sorted(cstrength.items(), key=lambda x: x[1])[:3] if cstrength else []
    prompt = f"""You are Hermes, an elite autonomous forex AI with full access to real market intelligence.
You have complete freedom. No rules. No limits. Use everything below to make your best autonomous decision.

ACCOUNT: Balance=€{balance:.2f} | Equity=€{equity:.2f} | 1% risk = €{max_risk_eur:.2f}
SESSION: {now_utc.strftime('%Y-%m-%d %H:%M')} UTC | {['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'][wd]}

--- MACRO INTELLIGENCE ---
Fed Rate: {macro.get('fed_funds_rate')}% | Yield Curve 10y-2y: {macro.get('yield_curve_10y2y')} | 10y-3m: {macro.get('yield_curve_10y3m')}
US CPI YoY: {macro.get('us_cpi_yoy')} | US Unemployment: {macro.get('us_unemployment')}% | GDP Growth: {macro.get('us_gdp_growth')}%
EUR CPI: {macro.get('eur_cpi')} | M2: {macro.get('us_m2_money_supply')}B | Retail Sales: {macro.get('us_retail_sales_mom')}
VIX: {macro.get('vix')} | CNN Fear & Greed: {fg.get('fear_greed_score')}/100 ({fg.get('fear_greed_rating')})

--- ASSET PRICES & MOMENTUM ---
{json.dumps(assets, indent=2)}

--- CURRENCY STRENGTH (4h momentum, % vs peers) ---
Strongest: {strongest}
Weakest:   {weakest}
Full: {cstrength}

--- CFTC COT REPORT (Hedge Fund Positioning — latest: {cot.get('report_date','?')}) ---
{json.dumps({k: v for k, v in cot.items() if k != 'report_date'}, indent=2)}

--- INTEREST RATE DIFFERENTIALS (2Y bond yields — carry trade flows) ---
Yields: {rates.get('yields', {})}
Pair Differentials: {rates.get('differentials', {})}

--- PAIR CORRELATIONS (20-day rolling, avoid doubling up correlated positions) ---
{json.dumps(correlations, indent=2)}

--- SENTIMENT DASHBOARD (multi-source aggregation) ---
{json.dumps(sentiment, indent=2)}

--- ECONOMIC CALENDAR (ForexFactory — high impact events this & next week) ---
{json.dumps([{"date":e.get("date"),"currency":e.get("currency"),"event":e.get("title"),"impact":e.get("impact"),"forecast":e.get("forecast"),"previous":e.get("previous"),"actual":e.get("actual")} for e in ff_calendar if e.get("impact") in ["High","Medium"]][:20], indent=2)}

--- HIGH-IMPACT NEWS (MT5, next 2h) ---
{upcoming_high if upcoming_high else 'None'}

--- HERMES TRADE HISTORY (last {len(past_trades)} trades: {wins}W / {losses}L) ---
{json.dumps([{{'sym':t.get('symbol'),'dir':t.get('direction'),'result':t.get('result'),'pnl':t.get('pnl'),'entry':t.get('entry')}} for t in past_trades[-10:]], indent=2)}

--- LIVE MARKET DATA (M15/H1/H4/D1 + TwelveData + Key Levels on all scanned symbols) ---
{json.dumps(market_data, indent=2)}

Respond ONLY with valid JSON:
{{
  "action": "trade" or "wait",
  "reason": "your full autonomous reasoning",
  "symbol": "MT5 symbol e.g. EURUSDxx or null",
  "direction": "Buy" or "Sell" or null,
  "stop_loss": number or null,
  "take_profit": number or null,
  "volume": number or null,
  "confidence": "low/medium/high",
  "signals_aligned": ["signals you identified"]
}}"""

    try:
        r = requests.post("http://127.0.0.1:9999/v1/chat/completions",
                          headers={"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"},
                          json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": prompt}],
                                "response_format": {"type": "json_object"}, "max_tokens": 600, "temperature": 0.2},
                          timeout=60)
        decision = json.loads(r.json()["choices"][0]["message"]["content"])
    except Exception as e:
        log.error(f"GPT error: {e}")
        return

    log.info(f"Decision: {decision}")

    # Execute — Hermes decides everything
    if decision.get("action") == "trade":
        sym  = decision.get("symbol")
        dire = decision.get("direction")
        sl   = decision.get("stop_loss")
        tp   = decision.get("take_profit")
        vol  = decision.get("volume", 0.1)

        if not all([sym, dire, sl, tp]): return

        # Block check
        for ccy in blocked_ccys:
            if ccy and ccy[:2] in sym.upper():
                tg(f"⏸ *GENESIS*: Blocked {sym} — {ccy} news in 2h")
                return

        order = bridge("/market", "POST", {"symbol": sym, "volume": vol, "type": dire,
                                            "stop_loss": sl, "take_profit": tp, "comment": "GENESIS-v2"})
        log.info(f"Order: {order}")

        ticket = order.get("ticket") or order.get("Ticket")
        if ticket:
            # ── Phase 1: Partial fill verification ──────────────────
            time.sleep(2)  # Give broker 2s to settle the order
            live_positions = bridge("/positions")
            actual_vol = 0.0
            if isinstance(live_positions, list):
                for p in live_positions:
                    if str(p.get("ticket")) == str(ticket):
                        actual_vol = p.get("lots", 0.0)
                        break
            if actual_vol > 0 and abs(actual_vol - vol) > 0.001:
                log.warning(f"Partial fill: requested {vol}, filled {actual_vol}")
                tg(f"⚠️ *GENESIS PARTIAL FILL*: Requested {vol} lot, got {actual_vol} lot. Treating as open.")
            # ────────────────────────────────────────────────────────

            journal_write({"ticket": str(ticket), "symbol": sym, "direction": dire,
                           "volume": actual_vol if actual_vol > 0 else vol,
                           "sl": sl, "tp": tp, "entry": market_data.get(sym, {}).get("bid"),
                           "opened": now_utc.isoformat(), "result": None, "pnl": None,
                           "max_risk_eur": max_risk_eur})
            tg(f"✅ *GENESIS TRADE*\n"
               f"📈 {sym} {dire} | Vol: {actual_vol if actual_vol > 0 else vol}\n"
               f"Entry: {market_data.get(sym,{}).get('bid')} | SL: {sl} | TP: {tp}\n"
               f"🎯 Confidence: {decision.get('confidence')}\n"
               f"📊 Signals: {', '.join(decision.get('signals_aligned', []))}\n"
               f"💡 {decision.get('reason','')[:150]}\n"
               f"💰 Equity: €{equity:.2f} | Risk: €{max_risk_eur:.2f}")
        else:
            err = order.get("message", str(order))
            tg(f"⚠️ *GENESIS*: Order failed — {err}")
    else:
        log.info(f"No trade: {decision.get('reason','')}")

    log.info("=== Cycle complete ===")

if __name__ == "__main__":
    try:
        run_cycle()
    except Exception as e:
        log.error(f"CRASH: {e}", exc_info=True)
        tg(f"🚨 *GENESIS CRASH*: {str(e)[:200]}")
