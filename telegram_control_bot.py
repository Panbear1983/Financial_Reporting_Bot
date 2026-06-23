#!/usr/bin/env python3
"""
telegram_control_bot.py — iPhone-friendly control interface for the OpenClaw financial bot.

Supports both slash commands and natural language (via Claude Haiku).

Slash commands:
  /help                          List all commands
  /status                        Show schedule times and current config summary
  /watchlist                     Show all stocks in tracked_stocks.json
  /addstock CODE NAME            Add a stock (e.g. /addstock 2412 中華電)
  /removestock CODE              Remove a stock from the watchlist
  /portfolio                     Show portfolio holdings with shares and cost basis
  /report morning|closing|all    Trigger an on-demand report now

Natural language examples:
  "add TSMC to my watchlist"
  "run the closing report"
  "what stocks am I tracking?"
  "remove 6443 from the list"

Security: only responds to the chat_id set in CONTROL_CHAT_ID env var.
"""

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

CONTROL_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ALLOWED_CHAT_ID   = str(os.getenv("CONTROL_CHAT_ID", ""))
OPENROUTER_KEY    = os.getenv("OPENROUTER_API_KEY", "")
TRACKED_STOCKS    = BASE_DIR / "tracked_stocks.json"
PORTFOLIO_FILE    = BASE_DIR / "portfolio.json"

API_BASE = f"https://api.telegram.org/bot{CONTROL_BOT_TOKEN}"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(BASE_DIR / "control_bot.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


# ── Telegram API helpers ──────────────────────────────────────────────────────

def send(chat_id: str, text: str) -> None:
    try:
        requests.post(
            f"{API_BASE}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as e:
        log.error("Failed to send message: %s", e)


def get_updates(offset: int) -> list:
    try:
        resp = requests.get(
            f"{API_BASE}/getUpdates",
            params={"offset": offset, "timeout": 30},
            timeout=35,
        )
        return resp.json().get("result", [])
    except Exception as e:
        log.error("getUpdates error: %s", e)
        return []


# ── JSON file helpers ─────────────────────────────────────────────────────────

def load_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ── LLM natural language understanding ───────────────────────────────────────

SYSTEM_PROMPT = """You are a financial assistant controlling an OpenClaw Taiwan stock reporting bot.

The user may speak in English, Chinese, or a mix. Your job is to understand their intent and respond with a JSON action.

Available actions:
{"action": "help"}
{"action": "status"}
{"action": "watchlist"}
{"action": "addstock", "code": "2330", "name": "台積電"}
{"action": "removestock", "code": "2330"}
{"action": "portfolio"}
{"action": "report", "mode": "morning"}   — mode: morning, closing, all
{"action": "chat", "text": "your reply"}  — for questions or unclear requests

Rules:
- Always respond with valid JSON only. No markdown, no extra text.
- Use "addstock" when user wants to add/track a stock. Extract the stock code (4-digit Taiwan code or US ticker) and name.
- Use "removestock" when user wants to remove/untrack a stock.
- Use "report" when user wants to run or trigger a report. Default mode is "closing" if unspecified.
- Use "chat" for general questions, greetings, or anything unclear. Keep replies under 80 words.
- If the user asks what stocks they're tracking or their portfolio, use "watchlist" or "portfolio".
"""


def call_llm(text: str, context: str = "") -> dict:
    if not OPENROUTER_KEY:
        return {"action": "chat", "text": "LLM not configured (OPENROUTER_API_KEY missing)."}
    prompt = f"{context}\nUser message: {text}" if context else text
    try:
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENROUTER_KEY}", "Content-Type": "application/json"},
            json={
                "model": "anthropic/claude-haiku-3-5",
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": 150,
            },
            timeout=15,
        )
        if resp.ok:
            raw = resp.json()["choices"][0]["message"]["content"].strip()
            return json.loads(raw)
    except json.JSONDecodeError:
        log.warning("LLM returned non-JSON: %s", raw)
    except Exception as e:
        log.error("LLM error: %s", e)
    return {"action": "chat", "text": "Sorry, I had trouble understanding that. Try /help for commands."}


def handle_llm_action(chat_id: str, action: dict) -> None:
    name = action.get("action", "chat")
    if name == "help":
        cmd_help(chat_id, [])
    elif name == "status":
        cmd_status(chat_id, [])
    elif name == "watchlist":
        cmd_watchlist(chat_id, [])
    elif name == "portfolio":
        cmd_portfolio(chat_id, [])
    elif name == "addstock":
        code = action.get("code", "")
        stock_name = action.get("name", "")
        cmd_addstock(chat_id, ["/addstock", code, stock_name])
    elif name == "removestock":
        code = action.get("code", "")
        cmd_removestock(chat_id, ["/removestock", code])
    elif name == "report":
        mode = action.get("mode", "closing")
        cmd_report(chat_id, ["/report", mode])
    else:
        send(chat_id, action.get("text", "I didn't understand that. Try /help."))


# ── Command handlers ──────────────────────────────────────────────────────────

def cmd_help(chat_id: str, _parts: list) -> None:
    send(chat_id, (
        "*OpenClaw Control Bot*\n\n"
        "You can type naturally or use slash commands:\n\n"
        "/status — schedule & config summary\n"
        "/watchlist — show tracked stocks\n"
        "/addstock CODE NAME — add to watchlist\n"
        "/removestock CODE — remove from watchlist\n"
        "/portfolio — show holdings\n"
        "/report morning|closing|all — run report now\n"
        "/help — show this message\n\n"
        "_Or just say what you want — I understand natural language too._"
    ))


def cmd_status(chat_id: str, _parts: list) -> None:
    stocks = load_json(TRACKED_STOCKS)
    portfolio = load_json(PORTFOLIO_FILE)
    send(chat_id, (
        "*Current Status*\n\n"
        f"Tracked stocks: {len(stocks)}\n"
        f"Portfolio positions: {len(portfolio)}\n\n"
        "*Schedule (UTC)*\n"
        "• Morning:   01:30 (09:30 Taiwan)\n"
        "• Closing:   08:00 (16:00 Taiwan)"
    ))


def cmd_watchlist(chat_id: str, _parts: list) -> None:
    stocks = load_json(TRACKED_STOCKS)
    if not stocks:
        send(chat_id, "Watchlist is empty.")
        return
    lines = [f"*Watchlist ({len(stocks)} stocks)*\n"]
    for code, name in stocks.items():
        lines.append(f"• `{code}` {name}")
    send(chat_id, "\n".join(lines))


def cmd_addstock(chat_id: str, parts: list) -> None:
    if len(parts) < 3:
        send(chat_id, "Usage: `/addstock CODE NAME`\nExample: `/addstock 2412 中華電`")
        return
    code = parts[1].strip()
    name = " ".join(parts[2:]).strip()
    if not code or not name:
        send(chat_id, "Please provide both a stock code and name.")
        return
    stocks = load_json(TRACKED_STOCKS)
    if code in stocks:
        send(chat_id, f"`{code}` is already in the watchlist as {stocks[code]}.")
        return
    stocks[code] = name
    save_json(TRACKED_STOCKS, stocks)
    log.info("Added stock %s (%s) to watchlist.", code, name)
    send(chat_id, f"Added `{code}` {name} to watchlist. Total: {len(stocks)} stocks.")


def cmd_removestock(chat_id: str, parts: list) -> None:
    if len(parts) < 2:
        send(chat_id, "Usage: `/removestock CODE`\nExample: `/removestock 6443`")
        return
    code = parts[1].strip()
    stocks = load_json(TRACKED_STOCKS)
    if code not in stocks:
        send(chat_id, f"`{code}` not found in watchlist.")
        return
    name = stocks.pop(code)
    save_json(TRACKED_STOCKS, stocks)
    log.info("Removed stock %s (%s) from watchlist.", code, name)
    send(chat_id, f"Removed `{code}` {name} from watchlist. Remaining: {len(stocks)} stocks.")


def cmd_portfolio(chat_id: str, _parts: list) -> None:
    portfolio = load_json(PORTFOLIO_FILE)
    if not portfolio:
        send(chat_id, "Portfolio is empty.")
        return
    lines = [f"*Portfolio ({len(portfolio)} positions)*\n"]
    for code, info in portfolio.items():
        name   = info.get("name", code)
        shares = info.get("shares", 0)
        cost   = info.get("cost_basis", 0)
        lines.append(f"• `{code}` {name} — {shares:,} shares, cost {cost:,}")
    send(chat_id, "\n".join(lines))


def cmd_report(chat_id: str, parts: list) -> None:
    valid_modes = ("morning", "closing", "all")
    mode = parts[1].lower() if len(parts) >= 2 else ""
    if mode not in valid_modes:
        send(chat_id, "Usage: `/report morning|closing|all`")
        return

    send(chat_id, f"Triggering `{mode}` report now... this may take a minute.")
    log.info("Manual report trigger: mode=%s", mode)

    if mode == "all":
        result = None
        for m in ("morning", "closing"):
            result = subprocess.run([sys.executable, str(BASE_DIR / "twse_daily_report.py"), f"--mode={m}"], cwd=BASE_DIR)
    else:
        result = subprocess.run([sys.executable, str(BASE_DIR / "twse_daily_report.py"), f"--mode={mode}"], cwd=BASE_DIR)

    status = "done" if result.returncode == 0 else f"failed (exit {result.returncode})"
    send(chat_id, f"`{mode}` report {status}.")


COMMANDS = {
    "/help":        cmd_help,
    "/status":      cmd_status,
    "/watchlist":   cmd_watchlist,
    "/addstock":    cmd_addstock,
    "/removestock": cmd_removestock,
    "/portfolio":   cmd_portfolio,
    "/report":      cmd_report,
}


# ── Main polling loop ─────────────────────────────────────────────────────────

def handle_message(msg: dict) -> None:
    chat_id = str(msg.get("chat", {}).get("id", ""))
    text    = msg.get("text", "").strip()

    if chat_id != ALLOWED_CHAT_ID:
        log.warning("Ignored message from unauthorized chat_id: %s", chat_id)
        return

    if not text:
        return

    if text.startswith("/"):
        parts   = text.split("@")[0].split()
        command = parts[0].lower()
        handler = COMMANDS.get(command)
        if handler:
            log.info("Slash command from %s: %s", chat_id, text)
            handler(chat_id, parts)
        else:
            send(chat_id, f"Unknown command `{command}`. Send /help for a list.")
    else:
        log.info("Natural language from %s: %s", chat_id, text)
        action = call_llm(text)
        log.info("LLM action: %s", action)
        handle_llm_action(chat_id, action)


def main() -> None:
    if not CONTROL_BOT_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN is not set. Add it to your .env file.")
        sys.exit(1)
    if not ALLOWED_CHAT_ID or ALLOWED_CHAT_ID == "PENDING":
        log.error("CONTROL_CHAT_ID is not set. Cannot enforce security whitelist.")
        sys.exit(1)

    llm_status = "with Haiku LLM" if OPENROUTER_KEY else "slash commands only (no OPENROUTER_API_KEY)"
    log.info("OpenClaw Control Bot started (%s). Listening for commands...", llm_status)
    offset = 0

    while True:
        updates = get_updates(offset)
        for update in updates:
            offset = update["update_id"] + 1
            if "message" in update:
                handle_message(update["message"])
        time.sleep(1)


if __name__ == "__main__":
    main()
