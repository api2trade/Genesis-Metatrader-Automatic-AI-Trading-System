# ─────────────────────────────────────────────────────────────────────────────
# GENESIS Trading System — Docker Image
# Base: Ubuntu 22.04 (matches production VPS)
# ─────────────────────────────────────────────────────────────────────────────
FROM ubuntu:22.04

# Avoid interactive prompts during apt
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# ── System packages ───────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y \
    python3.11 \
    python3.11-venv \
    python3-pip \
    cron \
    curl \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# Set timezone to UTC (matches trading sessions)
ENV TZ=UTC
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# ── Install directory (mirrors production /opt/hermes-agent) ──────────────────
RUN mkdir -p /opt/hermes-agent
WORKDIR /opt/hermes-agent

# ── Python virtual environment ────────────────────────────────────────────────
RUN python3.11 -m venv /opt/hermes-agent/.venv-hermes
ENV PATH="/opt/hermes-agent/.venv-hermes/bin:$PATH"

# ── Install Python dependencies ───────────────────────────────────────────────
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# ── Copy all source files ─────────────────────────────────────────────────────
COPY core/           ./core/
COPY strategies/     ./strategies/
COPY configs/        ./configs/
COPY backtest/       ./backtest/
COPY services/       ./services/
COPY scripts/        ./scripts/
COPY .env.example    .

# ── Copy configs into each strategy subfolder (where *_cycle.py expects them) ─
RUN cp configs/ares_config.yaml       strategies/ares/ && \
    cp configs/apollo_config.yaml     strategies/apollo/ && \
    cp configs/athena_config.yaml     strategies/athena/ && \
    cp configs/artemis_config.yaml    strategies/artemis/ && \
    cp configs/zeus_config.yaml       strategies/zeus/ && \
    cp configs/hephaestus_config.yaml strategies/hephaestus/

# ── Create log directories ────────────────────────────────────────────────────
RUN mkdir -p \
    /var/log/hermes \
    /var/log/ares \
    /var/log/apollo \
    /var/log/athena \
    /var/log/artemis \
    /var/log/zeus \
    /var/log/hephaestus

# ── CLI shortcuts ──────────────────────────────────────────────────────────────
RUN for bot in ares apollo athena artemis zeus hephaestus; do \
    printf '#!/bin/bash\n/opt/hermes-agent/.venv-hermes/bin/python3 /opt/hermes-agent/strategies/%s/%s_tool.py "$@"\n' \
        "$bot" "$bot" > /usr/local/bin/$bot && \
    chmod +x /usr/local/bin/$bot; \
done

RUN printf '#!/bin/bash\nSYMBOL=${1:-EURUSDxx}\necho ""\necho "=== GENESIS FULL SCAN: $SYMBOL ==="\necho ""\nfor bot in ares apollo athena artemis zeus; do\n    echo "--- $bot ---"\n    $bot analyze $SYMBOL 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'\''  Action: {d.get(chr(34)+chr(97)+chr(99)+chr(116)+chr(105)+chr(111)+chr(110)+chr(34),chr(63))}'\'')"\ndone\necho ""\n' \
    > /usr/local/bin/genesis-scan && chmod +x /usr/local/bin/genesis-scan

# ── Entrypoint ────────────────────────────────────────────────────────────────
COPY docker-entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
