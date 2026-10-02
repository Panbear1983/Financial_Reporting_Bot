#!/usr/bin/env python3
"""Streak alert — one Telegram push after the close naming every holding that
has closed in the same direction STREAK_THRESHOLD sessions in a row.

The live board shows this as its Streak / Run% columns; this is the same number
(dashboard.close_streak — one calculation, two displays) pushed to the phone so
the sell-into-a-run / buy-into-a-dip call can be made without opening the TUI.

Scheduled by scheduler.py after the closing report (bot_config
schedule.streak_utc, default 08:05 UTC = 16:05 Taipei). By hand:

    python streak_alert.py             # send if anything qualifies, once per day
    python streak_alert.py --dry-run   # print the message, send nothing
    python streak_alert.py --force     # resend even if today's alert went out

Sends nothing while the market is open (the count is closed sessions only), on
a day the market did not trade (no holding has a bar dated today), or when no
holding is at the threshold. Delivery goes through deliver_report(), so it
lands wherever the daily reports do — same bot, same chat, same voice bubble.
"""

import datetime
import json
import os
import sys
from zoneinfo import ZoneInfo

from dashboard import (DATA_DIR, _now, close_streak, fetch_history, load_bot_config,
                       load_portfolio, resolve_symbols, taiwan_market_open)
from twse_daily_report import deliver_report

STREAK_THRESHOLD = 3                 # bot_config {"streak_alert": {"threshold": N}} overrides
STATE_FILE = os.path.join(DATA_DIR, 'streak_alert_state.json')


def _taipei_today():
    return datetime.datetime.now(ZoneInfo('Asia/Taipei')).date()


def _load_state():
    try:
        with open(STATE_FILE, encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def _save_state(state):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def find_streaks(portfolio, threshold, today):
    """({code: {'name', 'run', 'pct'}} for holdings at/over the threshold,
    traded_today) — the flag is True when any holding has a bar dated today,
    i.e. the market actually traded, so a holiday never re-sends yesterday."""
    symbol_map = resolve_symbols(list(portfolio), DATA_DIR)
    bars = fetch_history(symbol_map.values(), '3mo', '1d')
    if not bars:
        raise RuntimeError('daily history fetch returned nothing (throttled?)')
    traded_today, hits = False, {}
    for code, pos in portfolio.items():
        sub = bars.get(symbol_map[code])
        if sub is None or 'Close' not in sub.columns:
            continue
        last = sub.index[-1]
        if (last.date() if hasattr(last, 'date') else last) == today:
            traded_today = True
        sk = close_streak(sub['Close'])
        if sk and abs(sk['run']) >= threshold:
            hits[code] = {'name': pos.get('name', ''), **sk}
    return hits, traded_today


def format_message(hits, today, threshold):
    # TW convention throughout the project: red = up, green = down.
    lines = [f'📊 持股連漲連跌提醒（{today:%Y-%m-%d} 收盤）',
             f'連續 {threshold} 個交易日以上同方向收盤：', '']
    for code, h in sorted(hits.items(), key=lambda kv: -abs(kv[1]['run'])):
        up = h['run'] > 0
        lines.append(f"{'🔴' if up else '🟢'} {code} {h['name']}　"
                     f"{'連漲' if up else '連跌'} {abs(h['run'])} 日　累計 {h['pct']:+.2f}%")
    lines += ['', '只計已收盤交易日；累計＝自連漲／連跌起點前一日收盤起算。']
    return '\n'.join(lines)


def main(argv):
    dry_run = '--dry-run' in argv
    force = '--force' in argv
    sandbox = os.getenv('SANDBOX_MODE', '').lower() == 'true'
    today = _taipei_today()

    if taiwan_market_open():
        print(f"[{_now()}] Streak alert: market is open — counts closed sessions only, "
              f"run after 13:30.")
        return 0
    state = _load_state()
    if state.get('last_run') == str(today) and not (force or dry_run):
        print(f"[{_now()}] Streak alert already ran today "
              f"({state.get('sent', 0)} holding(s) sent) — skipping.")
        return 0

    portfolio = load_portfolio()
    if not portfolio:
        print(f"[{_now()}] Streak alert: portfolio is empty — nothing to check.")
        return 0
    cfg = load_bot_config()
    threshold = int((cfg.get('streak_alert') or {}).get('threshold', STREAK_THRESHOLD))

    hits, traded_today = find_streaks(portfolio, threshold, today)
    if not traded_today:
        print(f"[{_now()}] Streak alert: no holding has a bar dated {today} — "
              f"market did not trade today, not sending.")
        return 0
    if not hits:
        print(f"[{_now()}] Streak alert: no holding at {threshold}+ sessions — nothing to send.")
        if not (dry_run or sandbox):
            _save_state({'last_run': str(today), 'sent': 0, 'codes': []})
        return 0

    text = format_message(hits, today, threshold)
    print(text, flush=True)
    if dry_run:
        print(f"[{_now()}] --dry-run: not sent, state untouched.")
        return 0
    # deliver_report retries and falls back to plain text itself; like the daily
    # reports, a day is recorded as done once delivery has been attempted.
    deliver_report(text, cfg)
    if not sandbox:
        _save_state({'last_run': str(today), 'sent': len(hits), 'codes': sorted(hits)})
    print(f"[{_now()}] Streak alert done — {len(hits)} holding(s): {', '.join(sorted(hits))}")
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as exc:                                       # noqa: BLE001
        print(f"[{_now()}] Streak alert failed: {type(exc).__name__}: {exc}", flush=True)
        sys.exit(1)
