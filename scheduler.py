import schedule
import time
import subprocess
import os
import sys
import datetime
import json

# Pin the process clock to UTC so `schedule`'s local-time .at() always means UTC,
# regardless of host timezone. A host UTC→Asia/Taipei shift (and the clock step it
# caused on restart) previously made the scheduler skip a day's closing run.
os.environ['TZ'] = 'UTC'
time.tzset()


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


def _report_exists_today(mode):
    """True if a report for today's UTC date and this mode was already archived."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    data_dir   = os.getenv('OPENCLAW_DATA_DIR', os.path.join(script_dir, 'data'))
    reports    = os.path.join(data_dir, 'reports')
    today      = datetime.datetime.utcnow().strftime('%Y-%m-%d')
    try:
        return any(f.startswith(f'twse_{today}_') and f.endswith(f'_{mode}.md')
                   for f in os.listdir(reports))
    except FileNotFoundError:
        return False


def catch_up_missed(morning_utc, closing_utc):
    """Run any slot that already passed today without producing a report.

    `schedule` has no catch-up: a restart or clock step past a slot silently
    skips that run until the next day. On startup we backfill once so a missed
    morning/closing push self-heals. SANDBOX is honoured via run_report().
    """
    now = datetime.datetime.utcnow()
    for mode, at in (('morning', morning_utc), ('closing', closing_utc)):
        try:
            hh, mm = map(int, at.split(':'))
        except ValueError:
            continue
        slot = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if now >= slot and is_taiwan_weekday() and not _report_exists_today(mode):
            print(f"Catch-up: {mode} {at} UTC slot passed with no report today — running now.",
                  flush=True)
            run_report(mode=mode)


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

# Backfill a slot we slept through (restart / clock step) so a missed push self-heals.
catch_up_missed(_morning_utc, _closing_utc)

while True:
    schedule.run_pending()
    time.sleep(60)
