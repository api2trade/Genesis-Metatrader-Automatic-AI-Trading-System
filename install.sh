#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# GENESIS Auto-Installer
# Tested on: Ubuntu 22.04 LTS
# Usage:     bash install.sh
# ─────────────────────────────────────────────────────────────────────────────

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

INSTALL_DIR="/opt/hermes-agent"
VENV="$INSTALL_DIR/.venv-hermes"
PYTHON="$VENV/bin/python3"
PIP="$VENV/bin/pip"

echo ""
echo -e "${CYAN}${BOLD}"
echo "  ██████  ███████ ███    ██ ███████ ███████ ██ ███████ "
echo " ██       ██      ████   ██ ██      ██      ██ ██      "
echo " ██   ███ █████   ██ ██  ██ █████   ███████ ██ ███████ "
echo " ██    ██ ██      ██  ██ ██ ██           ██ ██      ██ "
echo "  ██████  ███████ ██   ████ ███████ ███████ ██ ███████ "
echo ""
echo -e "${NC}${BOLD}  Autonomous Forex Trading System — Auto Installer${NC}"
echo -e "  Powered by ${CYAN}API2TRADE${NC} (https://app.api2trade.com)"
echo ""
echo "─────────────────────────────────────────────────────"

# ── Step 1: System packages ───────────────────────────────────────────────────
echo -e "\n${YELLOW}[1/8] Installing system packages...${NC}"
sudo apt-get update -qq
sudo apt-get install -y -qq python3.11 python3.11-venv python3-pip git curl wget unzip

# ── Step 2: Create install directory ─────────────────────────────────────────
echo -e "\n${YELLOW}[2/8] Creating install directory at $INSTALL_DIR...${NC}"
sudo mkdir -p "$INSTALL_DIR"
sudo chown "$USER":"$USER" "$INSTALL_DIR"

# Copy all repo files into install dir
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cp -r "$SCRIPT_DIR"/. "$INSTALL_DIR/"
echo -e "  ${GREEN}✓${NC} Files copied to $INSTALL_DIR"

# ── Step 3: Python virtual environment ───────────────────────────────────────
echo -e "\n${YELLOW}[3/8] Creating Python virtual environment...${NC}"
python3.11 -m venv "$VENV"
"$PIP" install --upgrade pip -q
"$PIP" install -r "$INSTALL_DIR/requirements.txt" -q
echo -e "  ${GREEN}✓${NC} Virtual environment ready at $VENV"

# ── Step 4: Log directories ───────────────────────────────────────────────────
echo -e "\n${YELLOW}[4/8] Creating log directories...${NC}"
for strat in hermes ares apollo athena artemis zeus hephaestus; do
    sudo mkdir -p "/var/log/$strat"
    sudo chown "$USER":"$USER" "/var/log/$strat"
    echo -e "  ${GREEN}✓${NC} /var/log/$strat"
done

# ── Step 5: Environment file ──────────────────────────────────────────────────
echo -e "\n${YELLOW}[5/8] Setting up environment variables...${NC}"
ENV_FILE="$INSTALL_DIR/.env"

if [ -f "$ENV_FILE" ]; then
    echo -e "  ${CYAN}ℹ${NC}  .env already exists — skipping (edit manually if needed)"
else
    cp "$INSTALL_DIR/.env.example" "$ENV_FILE"
    echo ""
    echo -e "  ${BOLD}You need to fill in your credentials.${NC}"
    echo -e "  Get your API2TRADE UUID + API Key at: ${CYAN}https://app.api2trade.com${NC}"
    echo ""

    read -p "  API2TRADE Account UUID: " uuid
    read -p "  API2TRADE API Key:       " apikey
    read -p "  Telegram Bot Token:      " tgtoken
    read -p "  Telegram Chat ID:        " tgchat
    read -p "  OpenRouter API Key:      " openkey

    sed -i "s/YOUR_API2TRADE_ACCOUNT_UUID/$uuid/g" "$ENV_FILE"
    sed -i "s/YOUR_API2TRADE_API_KEY/$apikey/g" "$ENV_FILE"
    sed -i "s/YOUR_TELEGRAM_BOT_TOKEN/$tgtoken/g" "$ENV_FILE"
    sed -i "s/YOUR_TELEGRAM_CHAT_ID/$tgchat/g" "$ENV_FILE"
    sed -i "s/YOUR_OPENAI_OR_OPENROUTER_KEY/$openkey/g" "$ENV_FILE"

    chmod 600 "$ENV_FILE"
    echo -e "  ${GREEN}✓${NC} .env created and secured (chmod 600)"
fi

# ── Step 6: Config files — inject env values ──────────────────────────────────
echo -e "\n${YELLOW}[6/8] Linking config files...${NC}"
ln -sf "$INSTALL_DIR/configs" "$INSTALL_DIR/configs_active" 2>/dev/null || true
echo -e "  ${GREEN}✓${NC} Configs ready at $INSTALL_DIR/configs/"
echo -e "  ${CYAN}ℹ${NC}  Edit configs/<strategy>_config.yaml to tune each strategy"

# ── Step 7: CLI shortcuts ─────────────────────────────────────────────────────
echo -e "\n${YELLOW}[7/8] Installing CLI shortcuts...${NC}"

install_shortcut() {
    local name=$1
    local tool=$2
    cat > "/usr/local/bin/$name" << EOF
#!/usr/bin/env bash
$PYTHON $INSTALL_DIR/$tool "\$@"
EOF
    sudo chmod +x "/usr/local/bin/$name"
}

sudo bash -c "cat > /usr/local/bin/ares << 'EOF'
#!/usr/bin/env bash
$PYTHON $INSTALL_DIR/strategies/ares/ares_tool.py \"\$@\"
EOF"
sudo bash -c "cat > /usr/local/bin/apollo << 'EOF'
#!/usr/bin/env bash
$PYTHON $INSTALL_DIR/strategies/apollo/apollo_tool.py \"\$@\"
EOF"
sudo bash -c "cat > /usr/local/bin/athena << 'EOF'
#!/usr/bin/env bash
$PYTHON $INSTALL_DIR/strategies/athena/athena_tool.py \"\$@\"
EOF"
sudo bash -c "cat > /usr/local/bin/artemis << 'EOF'
#!/usr/bin/env bash
$PYTHON $INSTALL_DIR/strategies/artemis/artemis_tool.py \"\$@\"
EOF"
sudo bash -c "cat > /usr/local/bin/zeus << 'EOF'
#!/usr/bin/env bash
$PYTHON $INSTALL_DIR/strategies/zeus/zeus_tool.py \"\$@\"
EOF"
sudo bash -c "cat > /usr/local/bin/hephaestus << 'EOF'
#!/usr/bin/env bash
$PYTHON $INSTALL_DIR/strategies/hephaestus/hephaestus_tool.py \"\$@\"
EOF"
sudo bash -c "cat > /usr/local/bin/genesis-scan << 'EOF'
#!/usr/bin/env bash
SYMBOL=\${1:-EURUSDxx}
echo \"\"
echo \"=== GENESIS FULL SCAN: \$SYMBOL ===\"
echo \"\"
for bot in ares apollo athena artemis zeus; do
    echo \"--- \$bot ---\"
    \$bot analyze \$SYMBOL 2>/dev/null | python3 -c \"import sys,json; d=json.load(sys.stdin); print(f'  Action: {d.get(\\\"action\\\",\\\"?\\\")}'  + (f' | {d.get(\\\"direction\\\",\\\"?\\\")} | R:R {d.get(\\\"rr_ratio\\\",\\\"?\\\")}' if d.get(\\\"action\\\")==\\\"trade\\\" else f' — {str(d.get(\\\"reason\\\",\\\"\\\"))[:80]}'))\" 2>/dev/null || echo \"  (scan error)\"
done
echo \"\"
EOF"
sudo chmod +x /usr/local/bin/ares /usr/local/bin/apollo /usr/local/bin/athena \
               /usr/local/bin/artemis /usr/local/bin/zeus /usr/local/bin/hephaestus \
               /usr/local/bin/genesis-scan
echo -e "  ${GREEN}✓${NC} CLI shortcuts: ares, apollo, athena, artemis, zeus, hephaestus, genesis-scan"

# ── Step 8: Cron jobs ─────────────────────────────────────────────────────────
echo -e "\n${YELLOW}[8/8] Installing cron jobs...${NC}"
CRON_TMP=$(mktemp)
crontab -l 2>/dev/null > "$CRON_TMP" || true

# Only add if not already present
add_cron() {
    local entry=$1
    grep -qF "$entry" "$CRON_TMP" || echo "$entry" >> "$CRON_TMP"
}

add_cron "*/5 * * * * cd $INSTALL_DIR && $PYTHON $INSTALL_DIR/core/genesis_autonomous.py >> /var/log/hermes/autonomous.log 2>&1"
add_cron "0 * * * * cd $INSTALL_DIR && $PYTHON $INSTALL_DIR/core/trading_cycle.py >> /var/log/hermes/trading_cycle.log 2>&1"
add_cron "30 * * * * cd $INSTALL_DIR && $PYTHON $INSTALL_DIR/core/genesis_brain_feed.py >> /var/log/hermes/autonomous.log 2>&1"
add_cron "0 7 * * 1-5 cd $INSTALL_DIR && $PYTHON $INSTALL_DIR/core/genesis_market_open.py >> /var/log/hermes/autonomous.log 2>&1"
add_cron "0 6 * * * cd $INSTALL_DIR && $PYTHON $INSTALL_DIR/core/genesis_daily_report.py >> /var/log/hermes/autonomous.log 2>&1"
add_cron "*/10 * * * * cd $INSTALL_DIR && $PYTHON $INSTALL_DIR/core/heartbeat.py >> /var/log/hermes/autonomous.log 2>&1"

crontab "$CRON_TMP"
rm "$CRON_TMP"
echo -e "  ${GREEN}✓${NC} Cron jobs installed"

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "─────────────────────────────────────────────────────"
echo -e "${GREEN}${BOLD}  ✓ GENESIS installed successfully!${NC}"
echo "─────────────────────────────────────────────────────"
echo ""
echo -e "  ${BOLD}Quick test:${NC}"
echo -e "    ares analyze EURUSDxx"
echo -e "    zeus analyze GBPUSDxx"
echo -e "    genesis-scan EURUSDxx"
echo ""
echo -e "  ${BOLD}Logs:${NC}"
echo -e "    tail -f /var/log/hermes/autonomous.log"
echo -e "    tail -f /var/log/hermes/trading_cycle.log"
echo ""
echo -e "  ${BOLD}Docs:${NC}"
echo -e "    ${CYAN}https://app.api2trade.com${NC}"
echo ""
echo -e "  ${YELLOW}⚠  Hephaestus (Grid/Martingale) is DISABLED by default.${NC}"
echo -e "     Read configs/hephaestus_config.yaml risk warning before enabling."
echo ""
