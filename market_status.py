#!/usr/bin/env python3
"""Is the Taiwan market open? — the same answer the scheduler gates on.

    python market_status.py            # today, right now
    python market_status.py 2026-09-28 # any date
    python market_status.py --month    # the next 30 days at a glance

Reads TWSE's published holiday schedule (開休市日期) through the data layer, so
it answers from the local cache and only reaches the exchange when the year's
calendar isn't cached yet.
"""

import datetime
import sys
from zoneinfo import ZoneInfo

from dashboard import fetch_market_holidays, is_trading_day, market_holiday_name, taiwan_market_open

TZ = ZoneInfo('Asia/Taipei')


def describe(day):
    if day.weekday() >= 5:
        return 'closed', '週末 weekend'
    name = market_holiday_name(day)
    if name is None:
        return 'unknown', 'holiday calendar unavailable — treated as a trading day'
    if name:
        return 'closed', name
    return 'open', '交易日 trading day'


def main(argv):
    if '--month' in argv:
        today = datetime.datetime.now(TZ).date()
        fetch_market_holidays(today.year - 1911)      # warm the cache once
        print(f'Taiwan market, next 30 days (from {today}):\n')
        for i in range(30):
            day = today + datetime.timedelta(days=i)
            state, why = describe(day)
            mark = '·' if state == 'open' else '✕'
            print(f'  {mark} {day:%Y-%m-%d %a}  {state:<7} {why if state != "open" else ""}'.rstrip())
        return 0

    arg = next((a for a in argv if not a.startswith('-')), None)
    if arg:
        try:
            day = datetime.date.fromisoformat(arg)
        except ValueError:
            print(f'Not a date: {arg}  (use YYYY-MM-DD)')
            return 1
        state, why = describe(day)
        print(f'{day:%Y-%m-%d %A}: market {state} — {why}')
        return 0

    now = datetime.datetime.now(TZ)
    state, why = describe(now.date())
    print(f'Taipei {now:%Y-%m-%d %A %H:%M}')
    print(f'  Today: market {state} — {why}')
    if state == 'open':
        print(f'  Right now: {"OPEN, trading 09:00–13:30" if taiwan_market_open() else "closed (outside 09:00–13:30)"}')
    nxt = now.date() + datetime.timedelta(days=1)
    while not is_trading_day(nxt):
        nxt += datetime.timedelta(days=1)
    print(f'  Next trading day: {nxt:%Y-%m-%d %A}')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
