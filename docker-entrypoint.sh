#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════════
#  GENESIS Docker Entrypoint
#  Validates credentials → sets up cron → starts autonomous engine
# ═══════════════════════════════════════════════════════════════════════════════
set -e

PYTHON="/opt/hermes-agent/.venv-hermes/bin/python3"
AGENT="/opt/hermes-agent"

echo ""
echo "  ██████  ███████ ███    ██ ███████ ███████ ██ ███████"
echo " ██       ██      ████   ██ ██      ██      ██ ██"
echo " ██   ███ █████   ██ ██  ██ █████   ███████ ██ ███████"
echo " ██    ██ ██      ██  ██ ██ ██           ██ ██      ██"
echo "  ██████  ███████ ██   ████ ███████ ███████ ██ ███████"
echo ""
echo "  Autonomous MT5 Trading System — Docker Mode"
echo "  Powered by API2TRADE · https://app.api2trade.com"
echo "─────────────────────────────────────────────────────"

# ── Load .env ─────────────────────────────────────────────────────────────────
if [ -f /opt/hermes-agent/.env ]; then
    set -a
    source /opt/hermes-agent/.env
    set +a
else
    echo ""
    echo "  ╔═══════════════════════════════════════════════════╗"
    echo "  ║            NO .env FILE FOUND                     ║"
    echo "  ║                                                   ║"
    echo "  ║  Mount your credentials file:                     ║"
    echo "  ║    docker run -v \$(pwd)/.env:/opt/hermes-agent/.env ║"
    echo "  ║                                                   ║"
    echo "  ║  Or generate it first:                            ║"
    echo "  ║    bash setup.sh                                  ║"
    echo "  ╚═══════════════════════════════════════════════════╝"
    echo ""
    exit 1
fi

# ── Validate required credentials ─────────────────────────────────────────────
echo ""
MISSING=0
PLACEHOLDER_PATTERN="YOUR_|CHANGE_ME|example|placeholder|sk-your"

check_var() {
    local var_name="$1"
    local var_val="${!var_name}"
    if [ -z "$var_val" ] || echo "$var_val" | grep -qiE "$PLACEHOLDER_PATTERN"; then
        echo "  ✗  $var_name — not configured"
        MISSING=$((MISSING + 1))
    else
        # Mask secrets in output
        local masked="${var_val:0:8}••••"
        echo "  ✓  $var_name = $masked"
    fi
}

echo "  Credential check:"
check_var MT5_ACCOUNT_UUID
check_var MT5_API_USER
check_var MT5_API_PASS
check_var TELEGRAM_BOT_TOKEN
check_var TELEGRAM_CHAT_ID

if [ "$MISSING" -gt 0 ]; then
    echo ""
    echo "  ╔═══════════════════════════════════════════════════════╗"
    echo "  ║  $MISSING required credential(s) missing.                 ║"
    echo "  ║                                                       ║"
    echo "  ║  Run the setup wizard on your host machine:           ║"
    echo "  ║    bash setup.sh                                      ║"
    echo "  ║                                                       ║"
    echo "  ║  Then restart:                                        ║"
    echo "  ║    docker compose restart                             ║"
    echo "  ║                                                       ║"
    echo "  ║  Get API credentials: https://app.api2trade.com       ║"
    echo "  ╚═══════════════════════════════════════════════════════╝"
    echo ""
    exit 1
fi

# ── Verify API2TRADE connectivity ──────────────────────────────────────────────
echo ""
echo "  Verifying API2TRADE connection..."
ACCT_JSON=$(curl -s \
    --user "$MT5_API_USER:$MT5_API_PASS" \
    "$MT5_API_URL/AccountSummary?id=$MT5_ACCOUNT_UUID" \
    --max-time 10 2>/dev/null || echo '{}')

BALANCE=$(echo "$ACCT_JSON" | $PYTHON -c "
import sys, json
try:
    d = json.load(sys.stdin)
    b = d.get('balance', 0)
    c = d.get('currency', 'USD')
    t = d.get('type', '')
    if b > 0:
        print(f'  ✓  Connected | Balance: {b:,.2f} {c} [{t}]')
    else:
        err = d.get('message', d.get('code', 'unknown error'))
        print(f'  ✗  API error: {err}')
except:
    print('  ✗  Could not parse API response')
" 2>/dev/null || echo "  ✗  Connection failed")
echo "$BALANCE"

if echo "$BALANCE" | grep -q "✗"; then
    echo ""
    echo "  WARNING: API2TRADE connection failed."
    echo "  Check your credentials at https://app.api2trade.com"
    echo "  Starting anyway — strategies will retry each cycle."
    echo ""
fi

# ── Export env for cron ────────────────────────────────────────────────────────
printenv | grep -E "^(MT5_|TELEGRAM_|OPENAI_|HERMES_|TWELVE_|MAX_)" > /etc/environment

# ── Install cron jobs ──────────────────────────────────────────────────────────
echo ""
echo "  Installing cron jobs..."

cat > /etc/cron.d/genesis << CRONEOF
SHELL=/bin/bash
PATH=/opt/hermes-agent/.venv-hermes/bin:/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin
BASH_ENV=/etc/environment

# GENESIS autonomous strategy scan — every 5 minutes
*/5 * * * * root cd $AGENT && $PYTHON $AGENT/core/genesis_autonomous.py >> /var/log/hermes/autonomous.log 2>&1

# Hermes LLM macro cycle — every hour
0 * * * * root cd $AGENT && $PYTHON $AGENT/core/trading_cycle.py >> /var/log/hermes/trading_cycle.log 2>&1

# Brain feed Telegram report — every hour at :30
30 * * * * root cd $AGENT && $PYTHON $AGENT/core/genesis_brain_feed.py >> /var/log/hermes/autonomous.log 2>&1

# Market open alert — weekdays 07:00 UTC
0 7 * * 1-5 root cd $AGENT && $PYTHON $AGENT/core/genesis_market_open.py >> /var/log/hermes/autonomous.log 2>&1

# Daily P&L report — 06:00 UTC
0 6 * * * root cd $AGENT && $PYTHON $AGENT/core/genesis_daily_report.py >> /var/log/hermes/autonomous.log 2>&1

# Heartbeat — every 10 minutes
*/10 * * * * root cd $AGENT && $PYTHON $AGENT/core/heartbeat.py >> /var/log/hermes/autonomous.log 2>&1
CRONEOF

chmod 644 /etc/cron.d/genesis
echo "  ✓  6 cron jobs installed"

# ── Ready ─────────────────────────────────────────────────────────────────────
echo ""
echo "─────────────────────────────────────────────────────"
echo "  ✓  GENESIS is running"
echo ""
echo "  Commands:"
echo "    docker exec -it genesis ares analyze EURUSDxx"
echo "    docker exec -it genesis zeus analyze GBPUSDxx"
echo "    docker exec -it genesis genesis-scan EURUSDxx"
echo "    docker exec -it genesis tail -f /var/log/hermes/autonomous.log"
echo "─────────────────────────────────────────────────────"
echo ""

# ── Start cron + tail logs ────────────────────────────────────────────────────
service cron start

touch /var/log/hermes/autonomous.log /var/log/hermes/trading_cycle.log
tail -f /var/log/hermes/autonomous.log /var/log/hermes/trading_cycle.log
