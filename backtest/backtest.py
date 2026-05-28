#!/usr/bin/env python3
"""
GENESIS Backtesting Engine v1
Uses yfinance for 1-year H1 historical data.
Runs the exact same EMA/RSI/ATR strategy as the live system.
Outputs performance report + sends results to Telegram.
"""
import os, json, requests
from datetime import datetime, timezone
from pathlib import Path

TG_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Symbol mapping: MT5 broker suffix → Yahoo Finance ticker
SYMBOL_MAP = {
    "EURUSDxx": "EURUSD=X",
    "XAUUSDxx": "GC=F",
    "GBPUSDxx": "GBPUSD=X",
    "GBPJPYxx": "GBPJPY=X",
    "USDJPYxx": "USDJPY=X",
}

def tg(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json={"chat_id": TG_CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=15)
    except: pass

def backtest_symbol(mt5_sym, yf_sym):
    import yfinance as yf
    import pandas as pd
    import ta

    print(f"\n{'='*50}")
    print(f"Backtesting: {mt5_sym} ({yf_sym})")

    df = yf.download(yf_sym, period="1y", interval="1h", progress=False, auto_adjust=True)
    if df.empty or len(df) < 100:
        print(f"  Insufficient data: {len(df)} bars")
        return None

    # Flatten multi-index if present
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.columns = [c.lower() for c in df.columns]
    df = df.rename(columns={"adj close": "close"})
    df = df.dropna()

    # Calculate indicators using 'ta' instead of 'pandas-ta'
    df["ema20"] = ta.trend.ema_indicator(df["close"], window=20)
    df["ema50"] = ta.trend.ema_indicator(df["close"], window=50)
    df["rsi"]   = ta.momentum.rsi(df["close"], window=14)
    df["atr"]   = ta.volatility.average_true_range(df["high"], df["low"], df["close"], window=14)
    df = df.dropna()

    print(f"  Downloaded {len(df)} H1 bars | {df.index[0].date()} → {df.index[-1].date()}")

    # Strategy: EMA20 > EMA50 + RSI < 45 → Buy | EMA20 < EMA50 + RSI > 55 → Sell
    # SL = 2x ATR below/above entry | TP = 4x ATR (2:1 R:R minimum)
    trades = []
    in_trade = False
    entry_price = sl = tp = direction = entry_idx = None

    for i in range(1, len(df)):
        row = df.iloc[i]
        prev = df.iloc[i-1]
        spread_est = row["atr"] * 0.05  # rough spread estimate

        if not in_trade:
            # Entry signals
            if row["ema20"] > row["ema50"] and prev["rsi"] < 45 and row["rsi"] > 45:
                direction = "Buy"
                entry_price = row["close"] + spread_est
                sl = round(entry_price - 2.0 * row["atr"], 5)
                tp = round(entry_price + 4.0 * row["atr"], 5)
                in_trade = True
                entry_idx = i
            elif row["ema20"] < row["ema50"] and prev["rsi"] > 55 and row["rsi"] < 55:
                direction = "Sell"
                entry_price = row["close"] - spread_est
                sl = round(entry_price + 2.0 * row["atr"], 5)
                tp = round(entry_price - 4.0 * row["atr"], 5)
                in_trade = True
                entry_idx = i
        else:
            # Check SL/TP hit
            high, low = row["high"], row["low"]
            result = None
            if direction == "Buy":
                if low <= sl:
                    result = "loss"; exit_price = sl
                elif high >= tp:
                    result = "win"; exit_price = tp
            else:
                if high >= sl:
                    result = "loss"; exit_price = sl
                elif low <= tp:
                    result = "win"; exit_price = tp

            # Max hold: 48 bars (2 days)
            if result is None and (i - entry_idx) >= 48:
                result = "timeout"; exit_price = row["close"]

            if result:
                diff = (exit_price - entry_price) if direction == "Buy" else (entry_price - exit_price)
                if "JPY" in mt5_sym:
                    pips = round(diff * 100.0, 1)
                elif "XAU" in mt5_sym or "GC" in yf_sym:
                    pips = round(diff, 2)
                else:
                    pips = round(diff * 10000.0, 1)

                trades.append({
                    "direction": direction,
                    "entry": entry_price,
                    "exit": exit_price,
                    "result": result,
                    "pips": pips,
                    "bars_held": i - entry_idx,
                    "date": df.index[entry_idx].strftime("%Y-%m-%d"),
                })
                in_trade = False

    if not trades:
        print("  No trades generated")
        return None

    wins    = [t for t in trades if t["result"] == "win"]
    losses  = [t for t in trades if t["result"] == "loss"]
    timeouts= [t for t in trades if t["result"] == "timeout"]
    total_pips = sum(t["pips"] for t in trades)
    win_pips   = sum(t["pips"] for t in wins)
    loss_pips  = sum(t["pips"] for t in losses)
    winrate    = len(wins) / len(trades) * 100

    # Profit factor
    pf = round(abs(win_pips / loss_pips), 2) if loss_pips != 0 else float("inf")

    # Max drawdown (running pip balance)
    running = 0; peak = 0; max_dd = 0
    for t in trades:
        running += t["pips"]
        if running > peak: peak = running
        dd = peak - running
        if dd > max_dd: max_dd = dd

    result = {
        "symbol": mt5_sym,
        "yf": yf_sym,
        "total_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "timeouts": len(timeouts),
        "win_rate": round(winrate, 1),
        "total_pips": round(total_pips, 1),
        "profit_factor": pf,
        "max_drawdown_pips": round(max_dd, 1),
        "avg_hold_bars": round(sum(t["bars_held"] for t in trades) / len(trades), 1),
    }

    print(f"  Trades: {result['total_trades']} | W:{result['wins']} L:{result['losses']} T:{result['timeouts']}")
    print(f"  Win rate: {result['win_rate']}% | Total pips: {result['total_pips']}")
    print(f"  Profit factor: {result['profit_factor']} | Max DD: {result['max_drawdown_pips']} pips")
    return result

def main():
    tg("🔬 *GENESIS Backtest Starting*\nRunning 1-year H1 backtest on 5 symbols using EMA20/50 + RSI + ATR strategy...\n_This will take ~60 seconds._")

    results = []
    for mt5_sym, yf_sym in SYMBOL_MAP.items():
        try:
            r = backtest_symbol(mt5_sym, yf_sym)
            if r:
                results.append(r)
        except Exception as e:
            print(f"  ERROR {mt5_sym}: {e}")

    if not results:
        tg("❌ *Backtest Failed*: No results generated.")
        return

    # Save results
    try:
        out_path = Path("/var/log/hermes/backtest_results.json")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(results, indent=2))
        print(f"\nResults saved to {out_path}")
    except Exception as e:
        print(f"\nCould not write to /var/log/hermes/backtest_results.json ({e}). Falling back to local workspace.")
        out_path = Path("./backtest_results.json")
        out_path.write_text(json.dumps(results, indent=2))
        print(f"Results saved to {out_path.resolve()}")

    # Build Telegram report
    report = "📊 *GENESIS Backtest Results* (1 Year H1)\n"
    report += "Strategy: EMA20/50 crossover + RSI + 2x ATR SL + 4x ATR TP\n\n"

    overall_trades = sum(r["total_trades"] for r in results)
    overall_wins   = sum(r["wins"] for r in results)
    overall_wr     = round(overall_wins / overall_trades * 100, 1) if overall_trades else 0

    for r in sorted(results, key=lambda x: x["win_rate"], reverse=True):
        emoji = "✅" if r["win_rate"] >= 50 and r["profit_factor"] >= 1.0 else "⚠️" if r["win_rate"] >= 45 else "❌"
        report += f"{emoji} *{r['symbol']}*\n"
        report += f"  {r['wins']}W/{r['losses']}L | WR: {r['win_rate']}% | PF: {r['profit_factor']}\n"
        report += f"  Pips: {r['total_pips']} | Max DD: {r['max_drawdown_pips']} pips\n\n"

    report += f"📈 *Overall:* {overall_wins}/{overall_trades} trades won ({overall_wr}%)\n"

    # Strategy verdict
    viable = [r for r in results if r["win_rate"] >= 50 and r["profit_factor"] >= 1.2]
    if viable:
        report += f"\n✅ *Viable symbols*: {', '.join(r['symbol'] for r in viable)}\n"
        report += "_These pairs have >50% win rate and >1.2 profit factor historically._"
    else:
        report += "\n⚠️ *No symbol meets viability criteria (>50% WR + >1.2 PF)*\n"
        report += "_Strategy needs tuning before live deployment._"

    print("\n" + report)
    tg(report)

    # Save markdown report
    md = f"# GENESIS Backtest Report\n*Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC*\n\n"
    md += report.replace("*", "**").replace("_", "*")
    try:
        report_path = Path("/var/log/hermes/backtest_report.md")
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(md)
        print(f"Report saved to {report_path}")
    except Exception as e:
        print(f"Could not write to /var/log/hermes/backtest_report.md ({e}). Falling back to local workspace.")
        report_path = Path("./backtest_report.md")
        report_path.write_text(md)
        print(f"Report saved to {report_path.resolve()}")

if __name__ == "__main__":
    main()
