"""Find your Telegram chat id and save it to .env."""

from __future__ import annotations

import os
import re
import time

import requests
from dotenv import load_dotenv

from .settings import PROJECT_DIR


def _save_env(key: str, value: str) -> None:
    path = PROJECT_DIR / ".env"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    line = f"{key}={value}"
    if re.search(rf"^{key}=.*$", text, flags=re.M):
        text = re.sub(rf"^{key}=.*$", line, text, flags=re.M)
    else:
        text = text.rstrip("\n") + f"\n{line}\n"
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o600)


def setup_telegram() -> int:
    load_dotenv(PROJECT_DIR / ".env")
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        print("Put TELEGRAM_BOT_TOKEN=... in .env first (from @BotFather), then run this again.")
        return 1
    base = f"https://api.telegram.org/bot{token}"
    me = requests.get(f"{base}/getMe", timeout=15).json()
    if not me.get("ok"):
        print(f"Token rejected by Telegram: {me.get('description')}")
        return 1
    name = me["result"]["username"]
    print(f"Open Telegram, find @{name}, and send it any message (e.g. 'hi').")
    print("Waiting up to 2 minutes...")
    offset = 0
    deadline = time.time() + 120
    while time.time() < deadline:
        ups = requests.get(f"{base}/getUpdates", params={"timeout": 20, "offset": offset},
                           timeout=35).json().get("result", [])
        for u in ups:
            offset = u["update_id"] + 1
            chat = (u.get("message") or {}).get("chat") or {}
            if chat.get("type") == "private":
                chat_id = str(chat["id"])
                _save_env("TELEGRAM_CHAT_ID", chat_id)
                # mark these updates as read so the agent starts clean
                requests.get(f"{base}/getUpdates", params={"offset": offset}, timeout=15)
                requests.post(f"{base}/sendMessage", timeout=15, json={
                    "chat_id": chat_id,
                    "text": "Connected. Trade plans and daily reports will arrive here.\n"
                            "Commands: /stop pauses all trading, /resume allows it again "
                            "(checked at the start of each run)."})
                print(f"Saved TELEGRAM_CHAT_ID={chat_id} to .env. Check Telegram for a confirmation.")
                return 0
    print("No message received. Send the bot a message and run this again.")
    return 1
