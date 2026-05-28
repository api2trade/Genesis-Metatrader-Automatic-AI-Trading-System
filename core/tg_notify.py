#!/usr/bin/env python3
"""Quick Telegram notification helper used by cron scripts."""
import os, requests
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).parent.parent / ".env")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT  = os.getenv("TELEGRAM_CHAT_ID",   "")

def notify(msg: str) -> bool:
    if not TOKEN or not CHAT:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": CHAT, "text": msg, "parse_mode": "Markdown"},
            timeout=10,
        )
        return r.status_code == 200
    except Exception:
        return False

if __name__ == "__main__":
    import sys
    notify(sys.argv[1] if len(sys.argv) > 1 else "GENESIS test notification")
