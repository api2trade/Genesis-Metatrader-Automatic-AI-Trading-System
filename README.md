# GENESIS — Autonomous MT5 Trading System

> **Powered by [API2TRADE](https://app.api2trade.com)** — the REST API for MetaTrader 5

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![Python 3.11](https://img.shields.io/badge/Python-3.11-brightgreen)](https://python.org)
[![MT5 via API2TRADE](https://img.shields.io/badge/MT5-API2TRADE-orange)](https://app.api2trade.com)

GENESIS is a fully autonomous, multi-strategy Forex trading system that connects to any **MetaTrader 5** account via the [API2TRADE](https://app.api2trade.com) REST API. It runs six independent strategy bots in parallel, managed by **Hermes** — an LLM-powered orchestration brain.

---

## 🏗 Architecture

```
                    ┌─────────────────────────────┐
                    │         HERMES (Brain)       │
                    │    GPT-4o-mini · Hourly LLM  │
                    │    Macro analysis + routing  │
                    └──────────────┬──────────────┘
                                   │
          ┌────────────────────────┼────────────────────────┐
          │           GENESIS Autonomous Engine              │
          │         (genesis_autonomous.py · every 5min)    │
          └──┬──────┬──────┬──────┬──────┬──────────────────┘
             │      │      │      │      │
           ARES  APOLLO ATHENA ARTEMIS  ZEUS   HEPHAESTUS
           M1    M5     M5     H1      M5      Grid
           BB+RSI EMA   BB+RSI Ichimoku ICT    Martingale
             │      │      │      │      │        │
             └──────┴──────┴──────┴──────┴────────┘
                              │
                    ┌─────────▼─────────┐
                    │   API2TRADE REST  │
                    │  app.api2trade.com│
                    └─────────┬─────────┘
                              │
                    ┌─────────▼─────────┐
                    │   MetaTrader 5    │
                    │  (any broker)     │
                    └───────────────────┘
```

---

## 🤖 Strategy Bots

| Bot | Timeframe | Strategy | Symbols | Expected Signals |
|-----|-----------|----------|---------|-----------------|
| **ARES** | M1 | Bollinger Bands + RSI mean reversion | EURUSD, GBPUSD | 10–30/day |
| **APOLLO** | M5 | EMA 9/21 crossover trend following | EURUSD, GBPUSD | 5–15/day |
| **ATHENA** | M5 | Multi-TF BB + RSI ranging | EURUSD | 5–15/day |
| **ARTEMIS** | H1 | Ichimoku Kumo breakout | EURUSD, GBPUSD, GBPJPY | 2–8/day |
| **ZEUS** | M5 | ICT Smart Money — Liquidity + FVG + OB | EURUSD, GBPUSD, XAUUSD | 3–10/day |
| **HEPHAESTUS** | — | Grid / Martingale cycling | Any | Continuous |

---

## 📋 Prerequisites

### 1. API2TRADE Account (Required)

GENESIS communicates with MT5 exclusively through the [API2TRADE](https://app.api2trade.com) REST API — no local MT5 installation, no Windows VPS required.

1. Sign up at **[app.api2trade.com](https://app.api2trade.com)**
2. Connect your MetaTrader 5 account
3. Copy your **Account UUID** and **API Key** from the dashboard

> **Cost:** €12/month per connected MT5 account · No per-call fees · Unlimited requests

### 2. Telegram Bot (Required for alerts)

1. Message [@BotFather](https://t.me/BotFather) → `/newbot`
2. Copy the bot token
3. Get your Chat ID from [@userinfobot](https://t.me/userinfobot)

### 3. OpenAI API Key (Optional — for Hermes brain)

1. Get a key at [platform.openai.com/api-keys](https://platform.openai.com/api-keys)
2. Cost: ~$0.10–0.50/day using `gpt-4o-mini`
3. Without it: GENESIS runs strategies autonomously without LLM macro analysis

---

## 🚀 Quick Start

### Option A — Docker (Recommended for local testing)

```bash
# 1. Clone the repo
git clone https://github.com/api2trade/Genesis-Metatrader-Automatic-AI-Trading-System.git
cd Genesis-Metatrader-Automatic-AI-Trading-System

# 2. Run the setup wizard (generates your .env)
bash setup.sh

# 3. Start the container
docker compose up -d

# 4. Watch it run
docker logs -f genesis
```

### Option B — VPS Production (Ubuntu 22.04)

```bash
# 1. Clone the repo on your VPS
git clone https://github.com/api2trade/Genesis-Metatrader-Automatic-AI-Trading-System.git
cd Genesis-Metatrader-Automatic-AI-Trading-System

# 2. Run setup wizard
bash setup.sh

# 3. Install cron jobs, CLI shortcuts and log folders
bash install.sh

# 4. Verify cron schedule is active
crontab -l

# 5. Watch live logs
tail -f /var/log/hermes/autonomous.log
```

---

## 🔧 Manual Setup

If you prefer to configure manually instead of using `setup.sh`:

```bash
cp .env.example .env
nano .env   # Fill in your credentials
```

Required fields:

| Variable | Where to get it |
|----------|----------------|
| `MT5_ACCOUNT_UUID` | [app.api2trade.com](https://app.api2trade.com) → Dashboard |
| `MT5_API_KEY` | [app.api2trade.com](https://app.api2trade.com) → API Keys |
| `MT5_API_USER` | Your API2TRADE username |
| `MT5_API_PASS` | Your API2TRADE password |
| `TELEGRAM_BOT_TOKEN` | [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_CHAT_ID` | [@userinfobot](https://t.me/userinfobot) |

---

## 💻 Usage

### Strategy Analysis

```bash
# Single strategy analysis (no trade placed)
docker exec -it genesis ares analyze EURUSDxx
docker exec -it genesis apollo analyze GBPUSDxx
docker exec -it genesis athena analyze EURUSDxx
docker exec -it genesis artemis analyze GBPUSDxx
docker exec -it genesis zeus analyze XAUUSDxx

# Scan ALL strategies on one symbol at once
docker exec -it genesis genesis-scan EURUSDxx
docker exec -it genesis genesis-scan GBPUSDxx
```

### Live Logs

```bash
# All decisions
docker exec -it genesis tail -f /var/log/hermes/autonomous.log

# Trade journal (JSONL)
docker exec -it genesis tail -f /var/log/hermes/trade_journal.jsonl

# LLM macro cycles
docker exec -it genesis tail -f /var/log/hermes/trading_cycle.log
```

### Strategy Output Format

Every strategy returns a JSON object:

```json
{
  "action": "trade",
  "strategy": "ares-bb-rsi-m1",
  "symbol": "EURUSDxx",
  "direction": "Buy",
  "entry": 1.08542,
  "stop_loss": 1.08392,
  "take_profit": 1.08842,
  "volume": 0.1,
  "rr_ratio": 2.0,
  "sl_pips": 15.0,
  "confidence": "high",
  "reason": "RSI oversold + BB lower touch + session active"
}
```

---

## 📁 Project Structure

```
Genesis-Metatrader-Automatic-AI-Trading-System/
├── setup.sh                    # ← Interactive setup wizard (start here)
├── install.sh                  # ← VPS production installer
├── docker-compose.yml          # ← Docker deployment
├── Dockerfile
├── .env.example                # ← Credential template
│
├── core/
│   ├── genesis_autonomous.py   # Main engine — runs every 5min via cron
│   ├── trading_cycle.py        # Hermes LLM macro cycle — hourly
│   ├── genesis_daily_report.py # Daily P&L Telegram report
│   ├── genesis_brain_feed.py   # Hourly Telegram market summary
│   ├── heartbeat.py            # System health check
│   └── tg_notify.py            # Telegram helper
│
├── strategies/
│   ├── ares/                   # BB+RSI M1 mean reversion
│   │   ├── ares_cycle.py       # Strategy logic + API2TRADE bridge
│   │   └── ares_tool.py        # CLI: ares analyze/execute EURUSDxx
│   ├── apollo/                 # EMA trend following
│   ├── athena/                 # BB+RSI multi-TF ranging
│   ├── artemis/                # Ichimoku H1 breakout
│   ├── zeus/                   # ICT Smart Money M5
│   └── hephaestus/             # Grid/Martingale
│
├── configs/
│   ├── ares_config.yaml        # ARES parameters (BB period, RSI thresholds, etc.)
│   ├── apollo_config.yaml
│   ├── athena_config.yaml
│   ├── artemis_config.yaml
│   ├── zeus_config.yaml
│   └── hephaestus_config.yaml
│
└── backtest/
    ├── backtest.py             # Python backtester using yfinance
    └── BB_RSI_MeanReversion.mq5  # MT5 Expert Advisor (manual backtest)
```

---

## ⚡ API2TRADE — How It Works

GENESIS uses API2TRADE as the bridge between Python and MetaTrader 5. Every strategy calls these endpoints directly:

```python
# Get account balance
GET /AccountSummary?id={session_uuid}
→ {"balance": 10000.0, "equity": 10000.0, "currency": "USD"}

# Get live quote
GET /Quote?id={session_uuid}&symbol=EURUSDxx
→ {"Bid": 1.08540, "Ask": 1.08542}

# Place a trade
GET /OrderSendSafe?id={session_uuid}&symbol=EURUSDxx&operation=Buy&volume=0.1&stoploss=1.083&takeprofit=1.090&comment=GENESIS-ARES
→ {"ticket": 12345678}

# Close a position
GET /OrderCloseSafe?id={session_uuid}&ticket=12345678&lots=0.1
```

No WebSocket setup, no local MT5 terminal, no Windows server. Just REST calls from any Python environment.

> 💡 **Sign up at [app.api2trade.com](https://app.api2trade.com) to get your session UUID and API key.**

---

## 🛡 Risk Management

Hard limits enforced before **every** trade — cannot be bypassed:

| Limit | Default | Config |
|-------|---------|--------|
| Max risk per trade | **2% of balance** | `MAX_RISK_PCT` in `.env` |
| Max lot size | **3.0 lots** | `MAX_LOTS` in `.env` |
| Max open positions | **4** | `MAX_POSITIONS` in `.env` |
| Stop loss | **Required** (5–150 pips) | Per strategy config |
| Spread filter | **1.0–3.0 pips** | Per strategy YAML |
| News filter | **±15 min** around high-impact | Per strategy YAML |

---

## ⚙️ Configuration

Each strategy has a dedicated YAML config in `configs/`. Example for ARES:

```yaml
# configs/ares_config.yaml
strategy:
  name: ARES
  magic_number: 1001

bollinger:
  period: 20
  std_dev: 2.0

rsi:
  period: 14
  oversold: 30
  overbought: 70

risk:
  risk_pct: 0.01          # 1% per trade
  max_spread_pips: 1.0
  min_rr_ratio: 1.5

sessions:
  allowed:
    - {start: 7, end: 20}  # GMT hours
```

---

## 🖥 VPS Deployment (Recommended)

For 24/7 autonomous trading, deploy on a Ubuntu 22.04 VPS:

```bash
# Minimum spec: 2 vCPU, 2GB RAM, 20GB SSD
# Cost: ~€4–8/month (Hetzner, Contabo, DigitalOcean)

bash setup.sh    # Configure credentials
bash install.sh  # Install Python, venv, cron jobs, CLI shortcuts
```

The installer sets up:
- Python 3.11 virtual environment at `/opt/hermes-agent/.venv-hermes`
- Cron job: `genesis_autonomous.py` every 5 minutes
- Cron job: `trading_cycle.py` (Hermes LLM) every hour
- Log rotation at `/var/log/hermes/`
- CLI shortcuts: `ares`, `apollo`, `athena`, `artemis`, `zeus`, `hephaestus`, `genesis-scan`

---

## 📊 Backtesting

```bash
# Backtest ARES on EURUSD (last 12 months)
python3 backtest/backtest.py --strategy ares --symbol EURUSD --days 365

# Or open the MT5 EA in MetaEditor for native backtesting
# backtest/BB_RSI_MeanReversion.mq5
```

---

## 🤝 Contributing

GENESIS is open source under **GPL v3** — forks must also be open source.

1. Fork the repo
2. Create your feature branch (`git checkout -b feature/my-strategy`)
3. Commit your changes
4. Open a Pull Request

---

## ⚠️ Disclaimer

This software is for **educational and research purposes**. Forex trading involves substantial risk of loss. Past performance does not guarantee future results. Always test on a **demo account** before trading with real money. The authors accept no responsibility for financial losses.

---

## 📄 License

GPL-3.0 · See [LICENSE](LICENSE)

---

<div align="center">

**Built with API2TRADE · [app.api2trade.com](https://app.api2trade.com)**

*Connect any MT5 account to Python in minutes · €12/month per account*

</div>
