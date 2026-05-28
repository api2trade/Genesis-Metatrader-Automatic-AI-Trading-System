#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
#  GENESIS Trading System — Interactive Setup Wizard
#  Powered by API2TRADE  ·  https://app.api2trade.com
# ═══════════════════════════════════════════════════════════════════════════════
set -e

GENESIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$GENESIS_DIR/.env"

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[1;33m'
BLU='\033[0;34m'; CYN='\033[0;36m'; WHT='\033[1;37m'; NC='\033[0m'

banner() {
  echo ""
  echo -e "${CYN}  ██████  ███████ ███    ██ ███████ ███████ ██ ███████${NC}"
  echo -e "${CYN} ██       ██      ████   ██ ██      ██      ██ ██     ${NC}"
  echo -e "${CYN} ██   ███ █████   ██ ██  ██ █████   ███████ ██ ███████${NC}"
  echo -e "${CYN} ██    ██ ██      ██  ██ ██ ██           ██ ██      ██${NC}"
  echo -e "${CYN}  ██████  ███████ ██   ████ ███████ ███████ ██ ███████${NC}"
  echo ""
  echo -e "${WHT}  Autonomous MT5 Trading System — Setup Wizard${NC}"
  echo -e "${BLU}  Powered by API2TRADE · https://app.api2trade.com${NC}"
  echo -e "  ─────────────────────────────────────────────────────"
  echo ""
}

ok()   { echo -e "  ${GRN}✓${NC}  $1"; }
warn() { echo -e "  ${YLW}⚠${NC}  $1"; }
err()  { echo -e "  ${RED}✗${NC}  $1"; }
hdr()  { echo -e "\n  ${WHT}$1${NC}"; echo "  $(printf '─%.0s' {1..50})"; }

banner

# ── Check if .env already configured ─────────────────────────────────────────
if [ -f "$ENV_FILE" ] && grep -q "^MT5_ACCOUNT_UUID=[a-f0-9-]" "$ENV_FILE" 2>/dev/null; then
  echo -e "  ${GRN}Existing .env found.${NC}"
  echo ""
  read -rp "  Re-run setup and overwrite? [y/N]: " REDO
  [[ "$REDO" =~ ^[Yy]$ ]] || { echo "  Skipping setup."; exit 0; }
  echo ""
fi

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 1: API2TRADE CREDENTIALS
# ═══════════════════════════════════════════════════════════════════════════════
hdr "Step 1 — API2TRADE Account"
echo ""
echo -e "  Sign up at ${BLU}https://app.api2trade.com${NC} if you haven't already."
echo -e "  You need: ${WHT}Account UUID${NC} and ${WHT}API Key${NC} from your dashboard."
echo -e "  Cost: ${GRN}€12/month per connected MT5 account${NC}"
echo ""

read -rp "  API2TRADE Account UUID (from dashboard): " MT5_ACCOUNT_UUID
while [[ ! "$MT5_ACCOUNT_UUID" =~ ^[0-9a-f-]{36}$ ]]; do
  err "Invalid UUID format. Should look like: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
  read -rp "  API2TRADE Account UUID: " MT5_ACCOUNT_UUID
done

read -rp "  API2TRADE API Key: " MT5_API_KEY
while [[ ${#MT5_API_KEY} -lt 10 ]]; do
  err "API key looks too short. Check your API2TRADE dashboard."
  read -rp "  API2TRADE API Key: " MT5_API_KEY
done

read -rp "  API2TRADE Username (from dashboard): " MT5_API_USER
read -rsp "  API2TRADE Password (hidden): " MT5_API_PASS
echo ""

MT5_API_URL="https://mt5.mt4api.dev"

# ── Verify API2TRADE connection ───────────────────────────────────────────────
echo ""
echo "  Verifying API2TRADE credentials..."
HTTP_STATUS=$(curl -s -o /tmp/genesis_api_check.json -w "%{http_code}" \
  --user "$MT5_API_USER:$MT5_API_PASS" \
  "$MT5_API_URL/AccountSummary?id=$MT5_ACCOUNT_UUID" \
  --max-time 10 2>/dev/null || echo "000")

if [ "$HTTP_STATUS" = "200" ]; then
  BALANCE=$(python3 -c "import json; d=json.load(open('/tmp/genesis_api_check.json')); print(f\"\${d.get('balance',0):,.2f} {d.get('currency','USD')}\")" 2>/dev/null || echo "connected")
  ok "API2TRADE connected — Balance: $BALANCE"
else
  ERRMSG=$(cat /tmp/genesis_api_check.json 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('message',''))" 2>/dev/null || echo "unknown error")
  warn "Could not verify connection (HTTP $HTTP_STATUS): $ERRMSG"
  warn "Credentials saved — you can verify manually later."
fi

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 2: TELEGRAM BOT
# ═══════════════════════════════════════════════════════════════════════════════
hdr "Step 2 — Telegram Notifications"
echo ""
echo "  GENESIS sends trade alerts, P&L reports and heartbeats via Telegram."
echo -e "  Create a bot at ${BLU}https://t.me/BotFather${NC} → /newbot → copy the token."
echo -e "  Get your Chat ID at ${BLU}https://t.me/userinfobot${NC}"
echo ""

read -rp "  Telegram Bot Token (e.g. 123456:ABC-...): " TELEGRAM_BOT_TOKEN
read -rp "  Your Telegram Chat ID (numeric, e.g. 123456789): " TELEGRAM_CHAT_ID

# Quick send test
echo ""
echo "  Sending test message..."
TG_RESP=$(curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
  -d "chat_id=${TELEGRAM_CHAT_ID}" \
  -d "text=🤖 *GENESIS* setup complete. System is online." \
  -d "parse_mode=Markdown" \
  --max-time 8 2>/dev/null)
TG_OK=$(echo "$TG_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('ok',''))" 2>/dev/null)

if [ "$TG_OK" = "True" ]; then
  ok "Telegram working — check your chat for the test message!"
else
  warn "Telegram test failed. Check token and chat ID. (Skipping — you can fix in .env)"
fi

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 3: OPENAI API KEY (Hermes LLM Brain)
# ═══════════════════════════════════════════════════════════════════════════════
hdr "Step 3 — OpenAI API Key (Hermes LLM Brain)"
echo ""
echo "  Hermes uses GPT-4o-mini to make macro trading decisions every hour."
echo -e "  Get your key at ${BLU}https://platform.openai.com/api-keys${NC}"
echo "  Cost: ~\$0.10–0.50/day for GPT-4o-mini at normal trading volume."
echo "  ${YLW}Optional — skip with Enter if you want analysis-only mode.${NC}"
echo ""

read -rp "  OpenAI API Key (sk-proj-...): " OPENAI_API_KEY
HERMES_MODEL="gpt-4o-mini"
OPENAI_BASE_URL="https://api.openai.com/v1"

if [[ -z "$OPENAI_API_KEY" ]]; then
  warn "No OpenAI key — Hermes LLM brain disabled. Strategies still run autonomously."
  OPENAI_API_KEY="sk-your-openai-api-key-here"
fi

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 4: RISK LIMITS
# ═══════════════════════════════════════════════════════════════════════════════
hdr "Step 4 — Risk Management"
echo ""
echo "  These limits are enforced by every strategy before any trade."
echo ""

read -rp "  Max open positions at once [default: 4]: " MAX_POSITIONS
MAX_POSITIONS="${MAX_POSITIONS:-4}"

read -rp "  Max risk per trade as % of balance [default: 0.02 = 2%]: " MAX_RISK_PCT
MAX_RISK_PCT="${MAX_RISK_PCT:-0.02}"

read -rp "  Max lot size per trade [default: 3.0]: " MAX_LOTS
MAX_LOTS="${MAX_LOTS:-3.0}"

# ═══════════════════════════════════════════════════════════════════════════════
# WRITE .env
# ═══════════════════════════════════════════════════════════════════════════════
hdr "Writing .env"

cat > "$ENV_FILE" << ENVEOF
# GENESIS Trading System — Environment Configuration
# Generated by setup.sh on $(date -u +"%Y-%m-%d %H:%M UTC")
# ─────────────────────────────────────────────────────────────────────────────
# SECURITY: Never commit this file to git. It is in .gitignore.
# ─────────────────────────────────────────────────────────────────────────────

# ── API2TRADE ─────────────────────────────────────────────────────────────────
# Get these from: https://app.api2trade.com → Dashboard → API Keys
MT5_ACCOUNT_UUID=${MT5_ACCOUNT_UUID}
MT5_ACCOUNT_ID=${MT5_ACCOUNT_UUID}
MT5_API_KEY=${MT5_API_KEY}
MT5_API_USER=${MT5_API_USER}
MT5_API_PASS=${MT5_API_PASS}
MT5_API_URL=${MT5_API_URL}

# ── Telegram ──────────────────────────────────────────────────────────────────
# Create bot: https://t.me/BotFather  |  Get Chat ID: https://t.me/userinfobot
TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}
TELEGRAM_CHAT_ID=${TELEGRAM_CHAT_ID}

# ── OpenAI (Hermes LLM Brain) ─────────────────────────────────────────────────
# Get key: https://platform.openai.com/api-keys
OPENAI_API_KEY=${OPENAI_API_KEY}
OPENAI_BASE_URL=${OPENAI_BASE_URL}
HERMES_MODEL=${HERMES_MODEL}

# ── Risk Limits ───────────────────────────────────────────────────────────────
MAX_POSITIONS=${MAX_POSITIONS}
MAX_RISK_PCT=${MAX_RISK_PCT}
MAX_LOTS=${MAX_LOTS}

# ── Optional: Twelve Data (news/economic calendar enrichment) ─────────────────
# Free tier available at: https://twelvedata.com
TWELVE_DATA_API_KEY=
ENVEOF

chmod 600 "$ENV_FILE"
ok ".env written and locked (chmod 600)"

# ═══════════════════════════════════════════════════════════════════════════════
# DONE
# ═══════════════════════════════════════════════════════════════════════════════
echo ""
echo -e "  ─────────────────────────────────────────────────────"
echo -e "  ${GRN}✓ GENESIS setup complete!${NC}"
echo ""
echo "  Next steps:"
echo ""
echo -e "  ${WHT}Docker (local test):${NC}"
echo "    docker compose up -d"
echo "    docker logs -f genesis"
echo ""
echo -e "  ${WHT}VPS (production):${NC}"
echo "    bash install.sh"
echo ""
echo -e "  ${WHT}Run a live scan right now:${NC}"
echo "    docker exec -it genesis ares analyze EURUSDxx"
echo "    docker exec -it genesis genesis-scan EURUSDxx"
echo ""
echo -e "  ${WHT}Watch live logs:${NC}"
echo "    docker logs -f genesis"
echo "    docker exec -it genesis tail -f /var/log/hermes/autonomous.log"
echo ""
echo -e "  ${BLU}API2TRADE dashboard: https://app.api2trade.com${NC}"
echo -e "  ─────────────────────────────────────────────────────"
echo ""
