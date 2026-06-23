"""
Sandbox Push Report Feedback Loop
==================================
Runs the full report pipeline (real data, real generation) but intercepts
the Telegram push and renders it to terminal + saves a preview file.

No messages are sent to the real Telegram chat.

Usage:
    python3 sandbox_run.py --mode=morning
    python3 sandbox_run.py --mode=closing
    python3 sandbox_run.py --mode=all
"""

import os
import sys
import datetime

# Must be set before importing report modules so send_telegram_report()
# picks up the flag at import time.
os.environ['SANDBOX_MODE'] = 'true'

from twse_daily_report import generate_daily_report


def _banner(title):
    bar = '═' * 55
    print(f"\n{bar}", flush=True)
    print(f"  [SANDBOX MODE]  {title}", flush=True)
    print(f"  {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    print(f"  No messages will be sent to Telegram.", flush=True)
    print(f"{bar}\n", flush=True)


def run_morning():
    _banner('TWSE Morning Report (01:30 UTC / 09:30 Taiwan)')
    generate_daily_report(mode='morning', send=True)


def run_closing():
    _banner('TWSE Closing Report (08:00 UTC / 16:00 Taiwan)')
    generate_daily_report(mode='closing', send=True)


def main():
    args = sys.argv[1:]
    mode = 'closing'
    for a in args:
        if a.startswith('--mode='):
            mode = a.split('=', 1)[1]

    if mode == 'morning':
        run_morning()
    elif mode == 'closing':
        run_closing()
    elif mode == 'all':
        run_morning()
        run_closing()
    else:
        print(f"Unknown mode: {mode}", flush=True)
        print("Valid options: morning, closing, all", flush=True)
        sys.exit(1)

    data_dir = os.getenv('OPENCLAW_DATA_DIR', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data'))
    print(f"\n[SANDBOX DONE] Preview files saved in: {data_dir}", flush=True)


if __name__ == '__main__':
    main()
