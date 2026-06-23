import schedule
import time
import subprocess
import os
import sys
import datetime
import json


def _load_bot_config():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    data_dir   = os.getenv('OPENCLAW_DATA_DIR', os.path.join(script_dir, 'data'))
    for directory in (data_dir, script_dir):
        path = os.path.join(directory, 'bot_config.json')
        if os.path.exists(path):
            try:
                with open(path) as f:
                    return json.load(f)
            except Exception:
                pass
    return {}

_cfg   = _load_bot_config()
_sched = _cfg.get('schedule', {})


# ---------------------------------------------------------------------------
# Flags — parsed before anything runs
# ---------------------------------------------------------------------------

args    = sys.argv[1:]
SANDBOX = '--sandbox' in args
NOW_MODE = next((a.split('=', 1)[1] for a in args if a.startswith('--now=')), None)

if SANDBOX:
    os.environ['SANDBOX_MODE'] = 'true'   # inherited by all subprocess.run() calls


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def is_taiwan_weekday():
    taiwan_now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    return taiwan_now.weekday() < 5  # Mon=0, Fri=4


def _sandbox_label():
    return '  [SANDBOX — no Telegram push]' if SANDBOX else ''


def run_report(mode='closing'):
    if not is_taiwan_weekday():
        print(f"Skipping {mode} report — weekend in Taiwan.", flush=True)
        return
    print(f"Executing TWSE {mode} report...{_sandbox_label()}", flush=True)
    result = subprocess.run([sys.executable, 'twse_daily_report.py', f'--mode={mode}'])
    status = 'successfully' if result.returncode == 0 else f'failed (code {result.returncode})'
    print(f"TWSE {mode} report executed {status}.", flush=True)


# ---------------------------------------------------------------------------
# --now: run a single report immediately then exit (useful for sandbox tests)
# ---------------------------------------------------------------------------

if NOW_MODE:
    label = f'[{"SANDBOX" if SANDBOX else "LIVE"}]'
    print(f"{label} Running {NOW_MODE} report immediately...", flush=True)
    if NOW_MODE in ('morning', 'closing'):
        run_report(mode=NOW_MODE)
    elif NOW_MODE == 'all':
        run_report(mode='morning')
        run_report(mode='closing')
    else:
        print(f"Unknown --now mode: {NOW_MODE}. Valid: morning, closing, all", flush=True)
        sys.exit(1)
    sys.exit(0)


# ---------------------------------------------------------------------------
# Scheduled loop
# ---------------------------------------------------------------------------

# Schedule times loaded from bot_config.json (falls back to hardcoded defaults)
_morning_utc  = _sched.get('morning_utc',  '01:30')
_closing_utc  = _sched.get('closing_utc',  '08:00')

schedule.every().day.at(_morning_utc).do(run_report, mode='morning')
schedule.every().day.at(_closing_utc).do(run_report, mode='closing')

if SANDBOX:
    print("━" * 55, flush=True)
    print("  SANDBOX MODE ACTIVE — no messages will reach Telegram", flush=True)
    print("  Preview files saved to OPENCLAW_DATA_DIR on each run", flush=True)
    print("━" * 55, flush=True)

print("Scheduler started. Waiting for next scheduled run...", flush=True)
print(f"  - TWSE morning:   {_morning_utc} UTC — weekdays, yfinance live", flush=True)
print(f"  - TWSE closing:   {_closing_utc} UTC — weekdays, TWSE official", flush=True)

while True:
    schedule.run_pending()
    time.sleep(60)
