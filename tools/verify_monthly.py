#!/usr/bin/env python3
"""Checks for the month-end total-wealth push (monthly_wealth.py + the scheduler's
monthly_due rule). Runs against a THROWAWAY COPY of data/, sends nothing.

    .venv/bin/python3.11 tools/verify_monthly.py

Exit code 0 = all passed.
"""
import os
import sys
import shutil
import tempfile
import datetime

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix='frb_verify_monthly_')
_src = os.path.join(REPO, 'data')
for name in ('portfolio.json', 'bot_config.json', 'tracked_stocks.json', 'cash_ledger.jsonl'):
    if os.path.exists(os.path.join(_src, name)):
        shutil.copy(os.path.join(_src, name), _tmp)
if os.path.exists(os.path.join(_src, 'diary')):
    shutil.copytree(os.path.join(_src, 'diary'), os.path.join(_tmp, 'diary'))
os.environ['FRB_DATA_DIR'] = _tmp
os.environ['SANDBOX_MODE'] = 'true'
os.environ['TZ'] = 'UTC'
import time
time.tzset()
sys.path.insert(0, REPO)

import dashboard as d                    # noqa: E402
import monthly_wealth as mw              # noqa: E402
import report_voice as rv                # noqa: E402

_results = []


def check(name, ok, detail=''):
    _results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ''))


D = datetime.date

# 1. The calendar rule: last CALENDAR day, holiday or not.
check('2026-10-31 (a Saturday) is a month end', mw.is_last_day_of_month(D(2026, 10, 31)))
check('2026-10-30 is not', not mw.is_last_day_of_month(D(2026, 10, 30)))
check('2026-02-28 is a month end', mw.is_last_day_of_month(D(2026, 2, 28)))
check('2024-02-29 is a month end (leap year)', mw.is_last_day_of_month(D(2024, 2, 29)))
check('2026-12-31 is a month end', mw.is_last_day_of_month(D(2026, 12, 31)))

# 2. The scheduler's "which month is owed" rule, with the archive marker faked.
import importlib.util                                           # noqa: E402
spec = importlib.util.spec_from_file_location('sched_rules', os.path.join(REPO, 'scheduler.py'))
# scheduler.py runs its loop at import; lift only the pure functions out of its source.
src = open(os.path.join(REPO, 'scheduler.py'), encoding='utf-8').read()
start = src.index('def _monthly_sent(')
end = src.index('def run_monthly_wealth(')
ns = {'os': os, 'sys': sys, 'datetime': datetime, '__file__': os.path.join(REPO, 'scheduler.py')}
exec(src[start:end], ns)
monthly_due, _monthly_sent = ns['monthly_due'], ns['_monthly_sent']
rep = os.path.join(_tmp, 'reports')
os.makedirs(rep, exist_ok=True)

check('on the last day, this month is owed', monthly_due(D(2026, 10, 31)) == '2026-10')
check('mid-month, nothing is owed', monthly_due(D(2026, 10, 15)) is None)
check('mid-October never catches up September (no total-wealth data then)',
      monthly_due(D(2026, 10, 3)) is None)
check('2 days into November, a missing October push IS owed', monthly_due(D(2026, 11, 2)) == '2026-10')
open(os.path.join(rep, 'wealth_2026-10.md'), 'w').write('sent')
check('…but not once its archive exists', monthly_due(D(2026, 11, 2)) is None)
check('on 10-31 with the archive present, nothing fires twice', monthly_due(D(2026, 10, 31)) is None)
os.remove(os.path.join(rep, 'wealth_2026-10.md'))
check('8 days late is left alone (not surfaced as news)', monthly_due(D(2026, 11, 8)) is None)

# 3. Honest history.
h = d.wealth_history()
check('history is sorted and non-empty', h and all(a['date'] < b['date'] for a, b in zip(h, h[1:])))
first_tw = next((r for r in h if r['total_wealth'] is not None), None)
check('total wealth begins where cash was first recorded (2026-10-05), never earlier',
      first_tw is not None and first_tw['date'] == '2026-10-05'
      and all(r['total_wealth'] is None for r in h if r['date'] < '2026-10-05'))
me = d.month_end_rows(h)
check('one row per month, each the last date of that month',
      [r['month'] for r in me] == sorted({r['date'][:7] for r in h})
      and all(r['date'] == max(x['date'] for x in h if x['date'][:7] == r['month']) for r in me))

# 4. The text, from the real October data.
s = mw.summarise(h, '2026-10')
text = mw.build_text(s, month_end=True)
check('the text names the total wealth from the last snapshot', f"{s['total_wealth']:,.0f}" in text)
check('the text states the real snapshot date, not the calendar day', s['date'][5:].replace('-', '/') in text)
check('the stock-only September month-end gives no fake wealth comparison',
      s['wealth_change'] is None and '首月不作月比' in text)
check('split percentages add to 100', abs(s['equity_pct'] + s['cash_pct'] - 100) < 0.01)
check('no total-wealth percentage return anywhere in the text',
      '總資產' in text and not any(tok in text for tok in ('總資產報酬', '總報酬率', 'NAV')))

# 5. The picture.
check('a Traditional-Chinese-capable font was found on this Mac', mw.cjk_font_path() is not None,
      mw.cjk_font_path() or 'none')
try:
    png = mw.build_png(h, s, months_back=12, month_end=True)
    check('the picture is a PNG', png[:8] == b'\x89PNG\r\n\x1a\n', f'{len(png):,} bytes')
    check('the picture is not tiny (something was drawn)', len(png) > 20_000)
except Exception as exc:                                          # noqa: BLE001
    check('the picture renders', False, f'{type(exc).__name__}: {exc}')

# 6. Voice: the month-end text is read in full, and nothing else changed.
check('the month-end push is spoken in full', rv.for_speech(text) == text)
closing = open(max((os.path.join(REPO, 'data', 'reports', f) for f in os.listdir(os.path.join(REPO, 'data', 'reports'))
                    if f.endswith('_closing.md')), key=os.path.getmtime), encoding='utf-8').read()
spoken = rv.for_speech(closing)
check('the closing push is still a slice, not the whole thing', len(spoken) < len(closing))

# 7. The month-end roll-up file.
p = d.record_month_end(dict(month='2026-10', **{k: s[k] for k in
                            ('date', 'stocks', 'cost', 'cash', 'total_wealth', 'net_flow', 'cash_as_of')}))
d.record_month_end(dict(month='2026-10', **{k: s[k] for k in
                        ('date', 'stocks', 'cost', 'cash', 'total_wealth', 'net_flow', 'cash_as_of')}))
rows = open(p, encoding='utf-8').read().strip().splitlines()
check('roll-up has a header and exactly one row per month after a rerun', len(rows) == 2 and rows[1].startswith('2026-10,'))

# 8. A sandbox run end to end sends nothing and writes the preview files.
before = set(os.listdir(_tmp))
out = mw.run(month='2026-10', send=True, force=True)
after = set(os.listdir(_tmp)) - before
check('sandbox run produced a text preview and a picture preview',
      any(f.startswith('sandbox_preview_') for f in after) and any(f.startswith('sandbox_photo_') for f in after),
      ', '.join(sorted(after)))
check('sandbox run archived nothing (no once-per-month marker written)', not mw.already_sent('2026-10'))

shutil.rmtree(_tmp, ignore_errors=True)
failed = _results.count(False)
print(f"\n{len(_results) - failed}/{len(_results)} passed" + (f", {failed} FAILED" if failed else ''))
sys.exit(1 if failed else 0)
