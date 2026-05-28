# ⚡ GENESIS — Autonomous AI Forex Trading System

> **6 strategy bots. 1 AI orchestrator. Fully autonomous. Live MT5 trading via [API2TRADE](https://app.api2trade.com).**

GENESIS is a production-grade, open-source autonomous forex trading engine built in Python. It runs 24/5 on a VPS, connects to a live MetaTrader 5 account via the [API2TRADE REST API](https://app.api2trade.com), and executes trades using six independent algorithmic strategies — each named after a Greek god — all coordinated by an AI agent called **Hermes**.

This is the complete, fully working source code published as a case study by [API2TRADE](https://app.api2trade.com).

---

## 🏛️ The Six Strategy Gods

| Bot | Command | Strategy | Timeframe | Best For |
|-----|---------|----------|-----------|----------|
| **ARES** | `ares analyze EURUSDxx` | BB + RSI Mean Reversion | M1 | Fast scalps on EURUSD/GBPUSD |
| **APOLLO** | `apollo analyze EURUSDxx` | EMA 9/21 Trend Following | M5 | Trending markets, London open |
| **ATHENA** | `athena analyze EURUSDxx` | BB + RSI Mean Reversion | M5 | EURUSD ranging conditions |
| **ARTEMIS** | `artemis analyze EURUSDxx` | Ichimoku Kumo Breakout | H1 | Trend continuations, GBPJPY |
| **ZEUS** | `zeus analyze EURUSDxx` | ICT Smart Money Concepts | M5 | London/NY killzones, gold |
| **HEPHAESTUS** | `hephaestus status` | Grid + Martingale ⚠️ | Continuous | Ranging markets (extreme risk) |

---

## 🚀 Quick Start (Ubuntu 22.04 VPS)

```bash
# 1. Clone the repo
git clone https://github.com/your-org/genesis.git
cd genesis

# 2. Run the auto-installer (handles everything)
bash install.sh
```

The installer will:
- Install Python 3.11 and all dependencies
- Create `/opt/hermes-agent/` with the full engine
- Set up all `/var/log/<strategy>/` directories
- Prompt for your API2TRADE credentials, Telegram token, and OpenRouter key
- Install all CLI shortcuts (`ares`, `zeus`, `genesis-scan`, etc.)
- Set up all cron jobs automatically

**That's it.** After install, test with:
```bash
ares analyze EURUSDxx
zeus analyze GBPUSDxx
genesis-scan EURUSDxx
```

---

## 📋 Prerequisites

| Requirement | Where to get it | Cost |
|-------------|----------------|------|
| Ubuntu 22.04 VPS | [Hetzner](https://hetzner.com) (CX22) or any provider | ~€12/mo |
| MT5 trading account | [Exness](https://exness.com) or any MT5 broker | Free |
| **API2TRADE account** | **[app.api2trade.com](https://app.api2trade.com)** | **€12/mo per account** |
| Telegram Bot | [@BotFather](https://t.me/BotFather) on Telegram | Free |
| OpenRouter API key | [openrouter.ai](https://openrouter.ai) | ~$1–2/mo (GPT-4o-mini) |
| TwelveData API key | [twelvedata.com](https://twelvedata.com) | Free tier |

> **Total monthly cost: ~€30–40/month** for a fully live, autonomous trading system.

---

## 🔑 API2TRADE Setup

GENESIS uses the **[API2TRADE REST API](https://app.api2trade.com)** to communicate with your MT5 terminal:

1. Sign up at [app.api2trade.com](https://app.api2trade.com)
2. Connect your MT5 account (enter login/password/server)
3. Copy your **Account UUID** and **API Key** from the dashboard
4. Paste them into `.env` when prompted by `install.sh`

```bash
# API calls used by GENESIS:
GET  /balance                          # Account equity & balance
GET  /positions                        # All open positions
GET  /quote?symbol=EURUSDxx            # Live bid/ask
GET  /symbols                          # Available symbols
GET  /history                          # Closed order history
POST /market   {symbol, volume, type, stop_loss, take_profit, comment}
POST /close    {ticket}
POST /modify   {ticket, stop_loss, take_profit}
```

---

## 📁 Repository Structure

```
genesis/
├── README.md                    ← You are here
├── install.sh                   ← One-command VPS installer
├── requirements.txt             ← Python dependencies
├── .env.example                 ← Environment variable template
├── .gitignore
│
├── core/                        ← Hermes AI orchestrator + engine
│   ├── trading_cycle.py         ← LLM macro analysis (hourly)
│   ├── genesis_autonomous.py    ← Main 5-min strategy scan loop
│   ├── genesis_brain_feed.py    ← Hourly Telegram intelligence report
│   ├── genesis_daily_report.py  ← Daily P&L summary
│   ├── genesis_trade_monitor.py ← Open position monitor
│   ├── genesis_market_open.py   ← Market open notification
│   ├── heartbeat.py             ← VPS health ping
│   └── tg_notify.py             ← Telegram notification helper
│
├── strategies/
│   ├── ares/                    ← BB+RSI M1 (+ Telegram bot)
│   ├── apollo/                  ← EMA M5 (+ Telegram bot)
│   ├── athena/                  ← BB+RSI M5 (+ Telegram bot)
│   ├── artemis/                 ← Ichimoku H1
│   ├── zeus/                    ← ICT Smart Money M5
│   └── hephaestus/              ← Grid/Martingale ⚠️
│
├── configs/                     ← YAML config for every strategy
│   ├── ares_config.yaml
│   ├── apollo_config.yaml
│   ├── athena_config.yaml
│   ├── artemis_config.yaml
│   ├── zeus_config.yaml
│   └── hephaestus_config.yaml   ← confirm_risk_acknowledged: false
│
├── backtest/
│   ├── backtest.py              ← Historical backtest runner
│   └── BB_RSI_MeanReversion.mq5 ← Original MQL5 EA (ARES source)
│
└── services/                    ← Systemd unit files (templates)
    ├── hermes.service
    ├── ares.service
    └── hermes_openrouter.service
```

---

## ⚙️ Manual Configuration

Each strategy reads its parameters from `configs/<strategy>_config.yaml`. Edit these **before** running:

```yaml
# configs/ares_config.yaml — example
bridge:
  url: "http://127.0.0.1:8000"    # Local bridge URL — don't change

mt5_api:
  account_id: "YOUR_API2TRADE_ACCOUNT_UUID"
  api_key:    "YOUR_API2TRADE_API_KEY"

telegram:
  chat_id: "YOUR_TELEGRAM_CHAT_ID"

risk:
  risk_pct: 0.01          # 1% per trade (never exceed 0.02)
  min_rr_ratio: 1.5       # Minimum reward:risk ratio

sessions:
  allowed:
    - {start: 7, end: 21} # London + NY hours UTC only
```

### Hephaestus — Explicit Risk Gate

The grid/martingale strategy is **disabled by default** and will refuse to run until you explicitly acknowledge the risk:

```yaml
# configs/hephaestus_config.yaml
strategy:
  confirm_risk_acknowledged: false   # ← Change to true ONLY after reading the risk warning
```

> ⚠️ Grid/Martingale can produce 90%+ win rates in ranging markets but carries **unlimited drawdown risk** in trending conditions. Never run with more than 0.01 initial lots without extensive backtesting.

---

## 🔄 Cron Schedule

The installer sets up these cron jobs automatically:

| Schedule | Script | Purpose |
|----------|--------|---------|
| `*/5 * * * *` | `core/genesis_autonomous.py` | Scan all strategies + execute signals |
| `0 * * * *` | `core/trading_cycle.py` | Hermes LLM macro analysis |
| `30 * * * *` | `core/genesis_brain_feed.py` | Hourly Telegram report |
| `0 7 * * 1-5` | `core/genesis_market_open.py` | Market open alert (weekdays) |
| `0 6 * * *` | `core/genesis_daily_report.py` | Daily P&L summary |
| `*/10 * * * *` | `core/heartbeat.py` | VPS health ping |

---

## 📱 CLI Commands

After installation, use these from anywhere on the VPS:

```bash
# Analyze — returns signal or wait reason
ares analyze EURUSDxx
apollo analyze GBPUSDxx
athena analyze EURUSDxx
artemis analyze GBPUSDxx
zeus analyze XAUUSDxx
zeus analyze GBPUSDxx

# Execute a trade (bot places order)
ares execute EURUSDxx
zeus execute GBPUSDxx

# Hephaestus grid
hephaestus status
hephaestus tick

# Full scan — all strategies on one symbol at once
genesis-scan EURUSDxx
genesis-scan GBPUSDxx
```

---

## 📊 External Data Sources

GENESIS uses 8 free/low-cost data sources for its intelligence layer:

| Source | Data | Cost |
|--------|------|------|
| [yfinance](https://pypi.org/project/yfinance/) | OHLCV bars, currency strength, indices | Free |
| [FRED API](https://fred.stlouisfed.org/docs/api/) | Fed rates, yield curves, CPI, GDP | Free |
| [ForexFactory](https://nfs.faireconomy.media/ff_calendar_thisweek.json) | Economic calendar | Free |
| [CFTC.gov](https://www.cftc.gov/MarketReports/CommitmentsofTraders/index.htm) | COT report (hedge fund positioning) | Free |
| CNN Fear & Greed | Market sentiment 0–100 | Free |
| [TwelveData](https://twelvedata.com) | RSI, MACD, ADX, Stochastic | Free tier |
| [API2TRADE](https://app.api2trade.com) | Economic calendar, closed orders, live quotes | €12/mo |
| [Alternative.me](https://alternative.me/crypto/fear-and-greed-index/) | Crypto Fear & Greed (risk proxy) | Free |

---

## 📈 Hermes — How the AI Makes Decisions

The `trading_cycle.py` script runs hourly and sends a **rich intelligence prompt** to GPT-4o-mini (via OpenRouter). The prompt includes:

- Fed Funds Rate, yield curves, CPI, GDP, M2 money supply
- VIX + CNN Fear & Greed Index
- Currency strength index (all 8 majors, 4h momentum)
- CFTC COT Report (hedge fund net positions)
- Interest rate differentials (2Y bond yields, 7 currencies)
- 20-day rolling pair correlations
- ForexFactory calendar (next 2 weeks)
- M15/H1/H4/D1 technical indicators (all symbols)
- Last 20 trades (win/loss learning)

**Hermes responds with a single JSON object:**
```json
{
  "action": "trade",
  "symbol": "EURUSDxx",
  "direction": "Buy",
  "stop_loss": 1.08120,
  "take_profit": 1.09400,
  "volume": 0.04,
  "confidence": "high",
  "reason": "...",
  "signals_aligned": ["COT_BULLISH_EUR", "LONDON_KILLZONE", ...]
}
```

---

## 🛡️ Risk Management

GENESIS has **hard-coded, non-overridable risk limits** at every level:

| Rule | Value |
|------|-------|
| Max open positions | 4 total |
| Max per strategy | 1 position |
| Stop-loss | Mandatory — always. Range: 5–150 pips |
| Max lots per trade | 3.0 lots |
| Max risk per trade | 2% of balance |
| Weekend trading | Blocked (Fri 22:00 – Sun 22:00 UTC) |
| High-impact news | Blocked 30–60 min before events |
| Max spread | 1.5–2.0 pips (strategy-dependent) |
| Emergency close | 10 retries × 30s before Telegram alert |

---

## 📋 Logs

```bash
# Main engine decisions
tail -f /var/log/hermes/trading_cycle.log

# All trades (JSONL)
tail -f /var/log/hermes/trade_journal.jsonl

# Autonomous loop
tail -f /var/log/hermes/autonomous.log

# Per-strategy
tail -f /var/log/zeus/zeus_cycle.log
tail -f /var/log/ares/ares_cycle.log
```

---

## 🔬 Backtesting

Run the backtest module against historical yfinance data:

```bash
cd /opt/hermes-agent
python3 backtest/backtest.py --symbol EURUSD --timeframe H1 --days 365
```

> **Our initial results:** An EMA20/50+RSI strategy produced a 24.8% win rate across 460 trades (all Profit Factors < 1.0). This is documented honestly in the case study — we rejected that strategy and built the 6 gods instead.

---

## 📖 Full Case Study

Read the complete technical deep-dive, architecture decisions, challenges, and cost breakdown:

👉 **[GENESIS Case Study — Published by API2TRADE](https://app.api2trade.com)**

---

## 📜 License

This project is licensed under the **GNU General Public License v3.0 (GPL-3.0)**.

You are free to use, modify, and distribute this code. Any derivative works must also be released under GPL-3.0.

See [LICENSE](LICENSE) for full text.

---

## ⚠️ Disclaimer

GENESIS trades real money on a live MT5 account. **Forex trading involves significant risk of capital loss.** Past strategy performance does not guarantee future results. This is an open-source research project, not financial advice.

- Never risk more than you can afford to lose
- Always backtest a strategy before running it live
- Monitor the system daily, especially Hephaestus
- The authors are not responsible for trading losses

---

## 🔗 Links

| Resource | URL |
|----------|-----|
| **API2TRADE** (MT5 REST API) | [app.api2trade.com](https://app.api2trade.com) |
| OpenRouter (LLM gateway) | [openrouter.ai](https://openrouter.ai) |
| Hetzner VPS | [hetzner.com](https://hetzner.com) |
| FRED API | [fred.stlouisfed.org](https://fred.stlouisfed.org/docs/api/) |
| TwelveData | [twelvedata.com](https://twelvedata.com) |
| CFTC COT Report | [cftc.gov](https://www.cftc.gov/MarketReports/CommitmentsofTraders/) |
| ForexFactory Calendar | [forexfactory.com](https://forexfactory.com) |

---

*Built with ❤️ and published by [API2TRADE](https://app.api2trade.com)*
