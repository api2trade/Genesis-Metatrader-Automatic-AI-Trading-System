#!/usr/bin/env python3
"""
GENESIS — Artemis Cycle (Strategy E: Ichimoku Kumo Breakout on H1)
Tenkan(9), Kijun(26), Senkou B(52), Displacement(26).
BUY: price > kumo + green cloud + RSI>50 + Chikou above price
SELL: price < kumo + red cloud + RSI<50 + Chikou below price
Never places trades — artemis_tool.py handles execution.
"""
import os, json, time, logging, math
from datetime import datetime, timezone
from pathlib import Path
import requests, yaml

CONFIG_PATH = Path(__file__).parent / "artemis_config.yaml"
if not CONFIG_PATH.exists():
    CONFIG_PATH = Path(__file__).parents[2] / "configs" / "artemis_config.yaml"
with open(CONFIG_PATH) as f:
    CFG = yaml.safe_load(f)

BRIDGE     = os.getenv("ARES_BRIDGE_URL", CFG["bridge"]["url"])
MT5_API    = os.getenv("MT5_API_URL",    "https://mt5.mt4api.dev")
MT5_ID     = os.getenv("MT5_ACCOUNT_ID", CFG["mt5_api"]["account_id"])
MT5_KEY    = os.getenv("MT5_API_KEY",    CFG["mt5_api"]["api_key"])
MT5_AUTH   = (os.getenv("MT5_API_USER", ""), os.getenv("MT5_API_PASS", ""))
TG_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID = str(os.getenv("TELEGRAM_CHAT_ID", CFG["telegram"]["chat_id"]))
CACHE_FILE = Path(CFG["cache"]["path"])
# Resolve safe journal path (fallback to local logs/ if system dir not writable)
default_journal = CFG["journal"]["path"]
try:
    Path(default_journal).parent.mkdir(parents=True, exist_ok=True)
    JOURNAL = Path(default_journal)
except Exception:
    local_log_dir = Path(__file__).parents[2] / "logs" / "artemis"
    local_log_dir.mkdir(parents=True, exist_ok=True)
    JOURNAL = local_log_dir / "trade_journal.jsonl"


TENKAN_P   = int(CFG["ichimoku"]["tenkan_period"])
KIJUN_P    = int(CFG["ichimoku"]["kijun_period"])
SENKOU_B_P = int(CFG["ichimoku"]["senkou_b_period"])
DISP       = int(CFG["ichimoku"]["displacement"])
CONF_BARS  = int(CFG["ichimoku"]["confirmation_bars"])
REQ_COLOR  = bool(CFG["ichimoku"]["require_cloud_color_alignment"])
REQ_CHIKOU = bool(CFG["ichimoku"]["require_chikou_confirmation"])
REQ_KIJUN  = bool(CFG["ichimoku"]["require_price_above_kijun"])

RSI_P      = int(CFG["confirmation"]["rsi_period"])
RSI_BUY    = float(CFG["confirmation"]["rsi_buy_threshold"])
RSI_SELL   = float(CFG["confirmation"]["rsi_sell_threshold"])

RISK_PCT   = float(CFG["risk"]["risk_pct"])
MIN_RR     = float(CFG["risk"]["min_rr_ratio"])
TP_MULT    = float(CFG["risk"]["tp_multiplier"])
MAX_SPREAD = float(CFG["risk"]["max_spread_pips"])
BLOCK_NEWS = int(CFG["risk"]["block_news_minutes"])
COOLDOWN   = int(CFG["strictness"]["cooldown_seconds"])
SIG_TF     = CFG["strictness"]["signal_timeframe"]
START_H    = int(CFG["sessions"]["allowed"][0]["start"])
END_H      = int(CFG["sessions"]["allowed"][0]["end"])
COMMENT    = CFG["strategy"]["comment"]

# Resolve safe log path (fallback to local logs/ if system dir not writable)
default_log = "/var/log/artemis/artemis_cycle.log"
try:
    Path(default_log).parent.mkdir(parents=True, exist_ok=True)
    log_file = default_log
except Exception:
    local_log_dir = Path(__file__).parents[2] / "logs" / "artemis"
    local_log_dir.mkdir(parents=True, exist_ok=True)
    log_file = str(local_log_dir / "artemis_cycle.log")

logging.basicConfig(
    filename=log_file,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger(__name__)
_last_sig: dict = {}

def load_cache():
    try: return json.loads(CACHE_FILE.read_text()) if CACHE_FILE.exists() else {}
    except: return {}

def save_cache(c): CACHE_FILE.write_text(json.dumps(c))

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
            json={"chat_id": TG_CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=10)
    except: pass

def pip_size(sym): return 0.01 if "JPY" in sym else (0.1 if "XAU" in sym else 0.0001)
def to_pips(diff, sym): return abs(diff) / pip_size(sym)

def calculate_lot(equity, sl_pips, sym):
    pv = 10.0
    if "JPY" in sym: pv = 9.0
    if "GBP" in sym: pv = 12.5
    if "XAU" in sym: pv = 1.0
    raw = (equity * RISK_PCT) / (sl_pips * pv) if sl_pips > 0 else 0.01
    return round(max(0.01, min(round(raw/0.01)*0.01, 5.0)), 2)

YF_MAP = {"EURUSDxx":"EURUSD=X","GBPUSDxx":"GBPUSD=X","USDJPYxx":"USDJPY=X",
          "XAUUSDxx":"GC=F","GBPJPYxx":"GBPJPY=X"}
YF_TF  = {"H1":"1h","H4":"4h","D1":"1d","M5":"5m"}

def get_bars(sym, tf="H1", count=130):
    try:
        import yfinance as yf, pandas as pd
        ys  = YF_MAP.get(sym, sym.replace("xx","=X"))
        itv = YF_TF.get(tf, "1h")
        per = {"1h":"60d","4h":"60d","1d":"365d","5m":"5d"}.get(itv,"60d")
        df  = yf.download(ys, period=per, interval=itv, progress=False, auto_adjust=True)
        if df.empty: return []
        if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
        df.columns = [c.lower() for c in df.columns]
        return df.dropna().tail(count).reset_index().to_dict("records")
    except Exception as e:
        log.error(f"get_bars {sym}/{tf}: {e}"); return []

def compute_ichimoku(bars: list) -> dict:
    """
    Compute all 5 Ichimoku components. Returns values for the LAST CLOSED bar.
    Senkou Spans are shifted FORWARD by DISP — to get the cloud at current price,
    we read SpanA/B at index -(DISP+2), which is the value plotted at current bar.
    Chikou Span = current close plotted DISP bars back → compare to close at -DISP-2.
    """
    needed = SENKOU_B_P + DISP + 10
    if len(bars) < needed: return {}
    try:
        import pandas as pd, ta, numpy as np
        df = pd.DataFrame(bars)
        df.columns = [c.lower() for c in df.columns]
        for col in ["close","high","low"]:
            df[col] = df[col].astype(float)

        def midpoint(h, l, p):
            return (h.rolling(p).max() + l.rolling(p).min()) / 2

        tenkan  = midpoint(df["high"], df["low"], TENKAN_P)
        kijun   = midpoint(df["high"], df["low"], KIJUN_P)
        span_a  = ((tenkan + kijun) / 2)           # plotted DISP bars ahead
        span_b  = midpoint(df["high"], df["low"], SENKOU_B_P)  # plotted DISP bars ahead

        rsi     = ta.momentum.rsi(df["close"], window=RSI_P)
        atr     = ta.volatility.average_true_range(df["high"], df["low"], df["close"], window=14)
        adx     = ta.trend.adx(df["high"], df["low"], df["close"], window=14)

        def s(series, i=-2):
            try:
                v = float(series.iloc[i])
                return None if math.isnan(v) else round(v, 6)
            except: return None

        # Current cloud = SpanA/B shifted forward DISP bars → read at -(DISP+2) in original series
        cloud_idx  = -(DISP + 2)
        sa_current = s(span_a, cloud_idx)
        sb_current = s(span_b, cloud_idx)

        # Kumo boundaries at current bar
        kumo_top    = max(sa_current, sb_current) if sa_current and sb_current else None
        kumo_bot    = min(sa_current, sb_current) if sa_current and sb_current else None
        cloud_color = "green" if (sa_current and sb_current and sa_current > sb_current) else "red"

        # Future cloud (next DISP bars — what SpanA/B are NOW vs price)
        sa_future   = s(span_a, -2)   # Will be plotted DISP bars from now
        sb_future   = s(span_b, -2)
        future_color = "green" if (sa_future and sb_future and sa_future > sb_future) else "red"

        close_now   = s(df["close"], -2)
        close_disp  = s(df["close"], -(DISP + 2))  # Chikou compare point

        # Chikou = current close vs price DISP bars ago
        chikou_bullish = (close_now or 0) > (close_disp or 0)
        chikou_bearish = (close_now or 0) < (close_disp or 0)

        # Confirmation bars: count how many consecutive bars have closed outside kumo
        conf_bull = 0
        conf_bear = 0
        if kumo_top and kumo_bot:
            for i in range(2, CONF_BARS + 3):
                c = s(df["close"], -i)
                kt = max(s(span_a, -(DISP + i)), s(span_b, -(DISP + i)) or 0)
                kb = min(s(span_a, -(DISP + i)) or 0, s(span_b, -(DISP + i)) or 0)
                if c and kt and c > kt: conf_bull += 1
                elif c and kb and c < kb: conf_bear += 1
                else: break

        return {
            "tenkan":        s(tenkan),
            "kijun":         s(kijun),
            "span_a_current": sa_current,
            "span_b_current": sb_current,
            "span_a_future": sa_future,
            "span_b_future": sb_future,
            "kumo_top":      kumo_top,
            "kumo_bottom":   kumo_bot,
            "cloud_color":   cloud_color,
            "future_color":  future_color,
            "chikou_bullish": chikou_bullish,
            "chikou_bearish": chikou_bearish,
            "conf_bars_bull": conf_bull,
            "conf_bars_bear": conf_bear,
            "rsi":           s(rsi),
            "atr":           s(atr),
            "adx":           s(adx),
            "close":         close_now,
            "kijun_current": s(kijun, -2),
        }
    except Exception as e:
        log.error(f"compute_ichimoku: {e}"); return {}

def check_news_block(sym):
    now = datetime.now(timezone.utc)
    blocked, warns = set(), []
    for evt in load_cache().get("ff_cal", {}).get("data", []):
        try:
            et   = datetime.fromisoformat(evt.get("date","")).astimezone(timezone.utc)
            mins = (et - now).total_seconds() / 60
            if evt.get("impact") == "High" and -15 < mins < BLOCK_NEWS:
                blocked.add(evt.get("currency","")[:3])
                warns.append(f"{evt.get('title')} in {int(mins)}min")
        except: pass
    return any(c and c in sym.upper() for c in blocked if c), warns

def is_trade_time():
    now = datetime.now(timezone.utc)
    wd, hr = now.weekday(), now.hour
    if (wd==4 and hr>=22) or wd==5 or (wd==6 and hr<22): return False
    return START_H <= hr < END_H

def has_artemis_position():
    pos = bridge("/positions")
    return isinstance(pos, list) and any("ARTEMIS" in str(p.get("comment","")).upper() for p in pos)

def run_analysis(symbol: str) -> dict:
    symbol = symbol.upper()
    if not symbol.endswith("XX"): symbol += "xx"
    symbol = symbol[:-2] + "xx"
    log.info(f"=== Artemis Ichimoku H1 Analysis: {symbol} ===")

    acc = bridge("/balance")
    if "error" in acc: return {"action":"wait","reason":f"Bridge error: {acc['error']}"}
    equity = float(acc.get("equity", 0))
    if equity <= 0: return {"action":"wait","reason":"No equity."}

    if has_artemis_position(): return {"action":"wait","reason":"Artemis position already open."}

    if time.time() - _last_sig.get(symbol, 0) < COOLDOWN:
        rem = int(COOLDOWN - (time.time() - _last_sig.get(symbol, 0)))
        return {"action":"wait","reason":f"Cooldown: {rem}s remaining."}

    if not is_trade_time(): return {"action":"wait","reason":f"Outside session (GMT {START_H}–{END_H})."}

    quote = bridge(f"/quote?symbol={symbol}")
    if "error" in quote or not quote.get("bid"): return {"action":"wait","reason":f"No quote for {symbol}."}
    bid, ask  = float(quote["bid"]), float(quote["ask"])
    spread    = to_pips(ask - bid, symbol)
    if spread > MAX_SPREAD: return {"action":"wait","reason":f"Spread {spread:.2f} > {MAX_SPREAD} pips."}

    blocked, news_warn = check_news_block(symbol)
    if blocked: return {"action":"wait","reason":f"News block: {'; '.join(news_warn[:2])}"}

    bars = get_bars(symbol, SIG_TF, SENKOU_B_P + DISP + 20)
    if len(bars) < SENKOU_B_P + DISP + 5: return {"action":"wait","reason":"Insufficient H1 data."}

    ind = compute_ichimoku(bars)
    if not ind: return {"action":"wait","reason":"Ichimoku calculation failed."}

    close   = ind["close"]
    k_top   = ind["kumo_top"]
    k_bot   = ind["kumo_bottom"]
    rsi     = ind["rsi"]
    atr     = ind["atr"]
    kijun   = ind["kijun_current"]
    f_color = ind["future_color"]
    c_color = ind["cloud_color"]

    if None in (close, k_top, k_bot, rsi): return {"action":"wait","reason":"Indicator values None."}

    # ── Signal detection ──────────────────────────────────────────
    bull_break = close > k_top
    bear_break = close < k_bot

    if not bull_break and not bear_break:
        return {"action":"wait","reason":f"Price inside Kumo. Close={close:.5f} Kumo=[{k_bot:.5f},{k_top:.5f}]"}

    direction = "Buy" if bull_break else "Sell"

    # ── Confluence conditions ─────────────────────────────────────
    if direction == "Buy":
        conds = [
            (close > k_top,                              f"Price above Kumo ({close:.5f} > {k_top:.5f})"),
            (f_color == "green",                         f"Future cloud GREEN (Span A > B ahead)"),
            (not REQ_COLOR or c_color == "green",        f"Current cloud {c_color}"),
            (not REQ_CHIKOU or ind["chikou_bullish"],     f"Chikou Span above price ({'+' if ind['chikou_bullish'] else '-'})"),
            (rsi > RSI_BUY,                              f"RSI {rsi:.1f} > {RSI_BUY}"),
            (not REQ_KIJUN or (kijun and close > kijun), f"Price above Kijun ({kijun:.5f if kijun else '?'})"),
            (ind["conf_bars_bull"] >= CONF_BARS,         f"{ind['conf_bars_bull']} bar(s) confirmed above Kumo"),
        ]
        entry = ask
        if kijun: sl = round(kijun - 0.0002, 6)
        elif atr:  sl = round(entry - atr * 1.5, 6)
        else:      sl = round(entry - 30 * pip_size(symbol), 6)
        sl_dist = abs(entry - sl)
        tp = round(entry + sl_dist * TP_MULT, 6)
    else:
        conds = [
            (close < k_bot,                              f"Price below Kumo ({close:.5f} < {k_bot:.5f})"),
            (f_color == "red",                           f"Future cloud RED (Span B > A ahead)"),
            (not REQ_COLOR or c_color == "red",          f"Current cloud {c_color}"),
            (not REQ_CHIKOU or ind["chikou_bearish"],     f"Chikou Span below price"),
            (rsi < RSI_SELL,                             f"RSI {rsi:.1f} < {RSI_SELL}"),
            (not REQ_KIJUN or (kijun and close < kijun), f"Price below Kijun ({kijun:.5f if kijun else '?'})"),
            (ind["conf_bars_bear"] >= CONF_BARS,         f"{ind['conf_bars_bear']} bar(s) confirmed below Kumo"),
        ]
        entry = bid
        if kijun: sl = round(kijun + 0.0002, 6)
        elif atr:  sl = round(entry + atr * 1.5, 6)
        else:      sl = round(entry + 30 * pip_size(symbol), 6)
        sl_dist = abs(entry - sl)
        tp = round(entry - sl_dist * TP_MULT, 6)

    passed = [(m,d) for m,d in conds if m]
    failed = [(m,d) for m,d in conds if not m]

    if len(passed) < 5:
        return {"action":"wait","reason":f"Only {len(passed)}/7 conditions met.",
                "conditions_met":[d for _,d in passed],"conditions_failed":[d for _,d in failed]}

    sl_pips = to_pips(entry - sl, symbol)
    tp_pips = to_pips(tp - entry, symbol)
    rr      = round(tp_pips / sl_pips, 2) if sl_pips > 0 else 0

    if rr < MIN_RR: return {"action":"wait","reason":f"R:R {rr} < minimum {MIN_RR}."}

    volume = calculate_lot(equity, sl_pips, symbol)
    _last_sig[symbol] = time.time()

    log.info(f"SIGNAL: {direction} {symbol} SL={sl} TP={tp} Vol={volume} RR={rr}")

    return {
        "action":       "trade",
        "strategy":     "artemis-ichimoku-h1",
        "signal_type":  "KUMO_BREAKOUT_BULLISH" if direction=="Buy" else "KUMO_BREAKOUT_BEARISH",
        "symbol":       symbol,
        "direction":    direction,
        "entry":        entry,
        "stop_loss":    sl,
        "take_profit":  tp,
        "volume":       volume,
        "rr_ratio":     rr,
        "sl_pips":      round(sl_pips, 1),
        "tp_pips":      round(tp_pips, 1),
        "confidence":   "high" if len(passed)==len(conds) else "medium",
        "conditions_met":   [d for _,d in passed],
        "conditions_failed":[d for _,d in failed],
        "warnings":     news_warn,
        "indicators":   {
            "tenkan": ind["tenkan"], "kijun": kijun,
            "kumo_top": k_top, "kumo_bottom": k_bot,
            "cloud_color": c_color, "future_cloud": f_color,
            "span_a": ind["span_a_current"], "span_b": ind["span_b_current"],
            "rsi": rsi, "atr": atr, "adx": ind.get("adx"),
            "chikou_bullish": ind["chikou_bullish"],
            "spread_pips": spread,
        },
        "signal_schema": {
            "strategy_id": "ARTEMIS-v1",
            "magic_number": CFG["strategy"]["magic_number"],
            "risk_percent": RISK_PCT,
            "metadata": {
                "tenkan_sen": ind["tenkan"], "kijun_sen": kijun,
                "senkou_a": ind["span_a_current"], "senkou_b": ind["span_b_current"],
                "kumo_top": k_top, "kumo_bottom": k_bot,
                "cloud_color": c_color, "rsi": rsi,
            }
        },
        "analysed_at": datetime.now(timezone.utc).isoformat(),
    }

if __name__ == "__main__":
    import sys
    sym = sys.argv[1] if len(sys.argv) > 1 else "EURUSDxx"
    print(json.dumps(run_analysis(sym), indent=2, default=str))
