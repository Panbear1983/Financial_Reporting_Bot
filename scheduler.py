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
    data_dir   = os.getenv('FRB_DATA_DIR', os.path.join(script_dir, 'data'))
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
    """True when the Taiwan exchange actually trades today.

    Named for Mon–Fri, which is all it used to check — and on 2026-09-25 (中秋節)
    that published a full morning report against a closed market, quoting the
    previous session's prices as today's. It now also consults TWSE's own
    holiday calendar via the data layer, falling back to the weekday test if
    that calendar can't be reached.
    """
    taiwan_now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    if taiwan_now.weekday() >= 5:
        return False
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from dashboard import market_holiday_name
        holiday = market_holiday_name(taiwan_now.date())
    except Exception as exc:                                       # noqa: BLE001
        print(f"Holiday check unavailable ({type(exc).__name__}: {exc}) — "
              f"treating as a trading day.", flush=True)
        return True
    if holiday:
        print(f"Taiwan market closed today — {holiday}.", flush=True)
        return False
    return True


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


def run_streak_alert():
    """Post-close push naming holdings on a 3+ session run (streak_alert.py)."""
    if not is_taiwan_weekday():
        print("Skipping streak alert — weekend in Taiwan.", flush=True)
        return
    print(f"Executing streak alert...{_sandbox_label()}", flush=True)
    result = subprocess.run([sys.executable, 'streak_alert.py'])
    status = 'successfully' if result.returncode == 0 else f'failed (code {result.returncode})'
    print(f"Streak alert executed {status}.", flush=True)


def _report_exists_today(mode):
    """True if a report for today's UTC date and this mode was already archived."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    data_dir   = os.getenv('FRB_DATA_DIR', os.path.join(script_dir, 'data'))
    reports    = os.path.join(data_dir, 'reports')
    today      = datetime.datetime.utcnow().strftime('%Y-%m-%d')
    try:
        return any(f.startswith(f'twse_{today}_') and f.endswith(f'_{mode}.md')
                   for f in os.listdir(reports))
    except FileNotFoundError:
        return False


def _streak_ran_today():
    """True if streak_alert.py already ran for today's Taipei date (its state file)."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    data_dir   = os.getenv('FRB_DATA_DIR', os.path.join(script_dir, 'data'))
    taipei_today = (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime('%Y-%m-%d')
    try:
        with open(os.path.join(data_dir, 'streak_alert_state.json')) as f:
            return json.load(f).get('last_run') == taipei_today
    except (FileNotFoundError, ValueError):
        return False


def catch_up_missed(morning_utc, closing_utc, streak_utc):
    """Run any slot that already passed today without leaving its record.

    `schedule` has no catch-up: a restart or clock step past a slot silently
    skips that run until the next day. On startup we backfill once so a missed
    morning/closing push (or streak alert) self-heals. SANDBOX is honoured via
    run_report() / streak_alert.py.
    """
    now = datetime.datetime.utcnow()
    slots = (
        ('morning', morning_utc, lambda: _report_exists_today('morning'),
         lambda: run_report(mode='morning')),
        ('closing', closing_utc, lambda: _report_exists_today('closing'),
         lambda: run_report(mode='closing')),
        ('streak',  streak_utc,  _streak_ran_today, run_streak_alert),
    )
    for name, at, done_today, run in slots:
        try:
            hh, mm = map(int, at.split(':'))
        except ValueError:
            continue
        slot = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if now >= slot and is_taiwan_weekday() and not done_today():
            print(f"Catch-up: {name} {at} UTC slot passed with nothing recorded today — running now.",
                  flush=True)
            run()


# ---------------------------------------------------------------------------
# --now: run a single report immediately then exit (useful for sandbox tests)
# ---------------------------------------------------------------------------

if NOW_MODE:
    label = f'[{"SANDBOX" if SANDBOX else "LIVE"}]'
    print(f"{label} Running {NOW_MODE} report immediately...", flush=True)
    if NOW_MODE in ('morning', 'closing'):
        run_report(mode=NOW_MODE)
    elif NOW_MODE == 'streak':
        run_streak_alert()
    elif NOW_MODE == 'all':
        run_report(mode='morning')
        run_report(mode='closing')
        run_streak_alert()
    else:
        print(f"Unknown --now mode: {NOW_MODE}. Valid: morning, closing, streak, all", flush=True)
        sys.exit(1)
    sys.exit(0)


# ---------------------------------------------------------------------------
# Scheduled loop
# ---------------------------------------------------------------------------

# Schedule times loaded from bot_config.json (falls back to hardcoded defaults)
_morning_utc  = _sched.get('morning_utc',  '01:30')
_closing_utc  = _sched.get('closing_utc',  '08:00')
_streak_utc   = _sched.get('streak_utc',   '08:05')   # after the closing report

schedule.every().day.at(_morning_utc).do(run_report, mode='morning')
schedule.every().day.at(_closing_utc).do(run_report, mode='closing')
schedule.every().day.at(_streak_utc).do(run_streak_alert)

if SANDBOX:
    print("━" * 55, flush=True)
    print("  SANDBOX MODE ACTIVE — no messages will reach Telegram", flush=True)
    print("  Preview files saved to FRB_DATA_DIR on each run", flush=True)
    print("━" * 55, flush=True)

print("Scheduler started. Waiting for next scheduled run...", flush=True)
print(f"  - TWSE morning:   {_morning_utc} UTC — weekdays, yfinance live", flush=True)
print(f"  - TWSE closing:   {_closing_utc} UTC — weekdays, TWSE official", flush=True)
print(f"  - Streak alert:   {_streak_utc} UTC — weekdays, holdings on a 3+ session run", flush=True)

# Backfill a slot we slept through (restart / clock step) so a missed push self-heals.
catch_up_missed(_morning_utc, _closing_utc, _streak_utc)

while True:
    schedule.run_pending()
    time.sleep(60)
