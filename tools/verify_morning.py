#!/usr/bin/env python3
"""Checks for the 09:05 morning rebuild and the brokerage cash ledger.

This project has no test suite, so this is the safety net for the 2026-10-02
rebuild (contract: docs/MORNING_REBUILD_2026-10-02.md). It runs against a
THROWAWAY COPY of data/, never sends anything, and never calls OpenRouter.

    .venv/bin/python3.11 tools/verify_morning.py            # everything
    .venv/bin/python3.11 tools/verify_morning.py --offline  # skip the live exchange checks

Exit code 0 = all passed.
"""
import os
import sys
import json
import shutil
import tempfile
import datetime

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OFFLINE = '--offline' in sys.argv

# Point the data layer at a scratch copy BEFORE importing it.
_tmp = tempfile.mkdtemp(prefix='frb_verify_')
_src = os.path.join(REPO, 'data')
for name in ('portfolio.json', 'bot_config.json', 'tracked_stocks.json'):
    if os.path.exists(os.path.join(_src, name)):
        shutil.copy(os.path.join(_src, name), _tmp)
if os.path.exists(os.path.join(_src, 'diary')):
    shutil.copytree(os.path.join(_src, 'diary'), os.path.join(_tmp, 'diary'))
os.environ['FRB_DATA_DIR'] = _tmp
os.environ['SANDBOX_MODE'] = 'true'
os.environ['TZ'] = 'UTC'                 # exactly as the scheduler runs the report
import time
time.tzset()
sys.path.insert(0, REPO)

import pandas as pd                      # noqa: E402
import dashboard as d                    # noqa: E402
import twse_daily_report as t            # noqa: E402
import report_voice as rv                # noqa: E402

_results = []


def check(name, ok, detail=''):
    _results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ''))


# 1. The exchange feed parser: a no-trade-yet row is DROPPED, never emitted with a
#    None price (compute_positions would crash on it).
row = {'c': '0050', 'y': '112.0', 'd': '20261002', 't': '09:01:00'}
check('no trade yet (all dashes) is dropped',
      d._mis_row(dict(row, z='-', o='-', b='')) is None)
check('junk row from an unservable code is dropped',
      d._mis_row({'c': '', 'z': '-', 's': '-', 'tv': '-'}) is None)
check('missing previous close is dropped',
      d._mis_row(dict(row, z='112.5', y='-', o='-')) is None)
check('price falls back to the top of the bid ladder',
      (d._mis_row(dict(row, z='-', o='-', b='112.4_112.3_')) or {}).get('price') == 112.4)
check('price falls back to the open',
      (d._mis_row(dict(row, z='-', o='112.6', b='')) or {}).get('price') == 112.6)
q = d._mis_row(dict(row, z='112.5', o='112.2', t='13:30:00'))
check('price time comes from d+t, not tlong',
      q and q['quote_at'].astimezone(d.ZoneInfo('Asia/Taipei')).strftime('%H:%M:%S') == '13:30:00')

# 2. The gate never reads an unknown answer as a closure.
_orig_get = d._mis_get
for payload, label in (({'rtcode': '9999'}, 'rtcode 9999'), (None, 'non-JSON body'),
                       ({'rtcode': '0000', 'msgArray': [{'c': '', 'z': '-'}]}, 'junk row only')):
    d._mis_get = lambda *a, _p=payload, **k: _p
    check(f'exchange_is_quoting() is None (unknown) on {label}', d.exchange_is_quoting() is None)
d._mis_get = lambda *a, **k: {'rtcode': '0000', 'msgArray': [{'c': 't00', 'd': '20200101', 't': '13:33:00'}]}
check('exchange_is_quoting() is False when the index is on an earlier session',
      d.exchange_is_quoting() is False)
d._mis_get = _orig_get

# 3. close_streak: the default rule is the one live since 2026-09-24.
idx = pd.date_range('2026-09-01', periods=6, freq='D')
up_flat = pd.Series([10, 11, 12, 12, 13, 14], index=idx)
check("flat mid-run BREAKS the run under the default 'break' rule",
      d.close_streak(up_flat, market_open=False, flat_rule='break')['run'] == 2)
check("flat mid-run is passed over under 'skip'",
      d.close_streak(up_flat, market_open=False, flat_rule='skip')['run'] == 4)
check("'skip' Run% is measured from where the run really began",
      abs(d.close_streak(up_flat, market_open=False, flat_rule='skip')['pct'] - 40.0) < 1e-9)
check('a flat newest close returns run 0 under the default rule',
      d.close_streak(pd.Series([10, 11, 11], index=idx[:3]), market_open=False)['run'] == 0)
check('the default rule resolves to break', d.streak_flat_rule({}) == 'break')
check('close_streak reports the date it counted through',
      d.close_streak(up_flat, market_open=False)['as_of'] == datetime.date(2026, 9, 6))

# 4. Repairing Yahoo's dropped sessions (measured: Yahoo dropped 2026-10-01 for every ETF checked).
gap = pd.Series([1.0, 2.0, 4.0], index=pd.to_datetime(['2026-09-29', '2026-09-30', '2026-10-02']))
_orig_close = d._official_close
d._official_close = lambda day, code: 3.0 if day == datetime.date(2026, 10, 1) else None
fixed = d.repair_daily_closes('0050', gap, datetime.date(2026, 10, 5), prev_close=4.5)
check('a dropped session is filled from the exchange',
      [round(v, 2) for v in fixed.tolist()] == [1.0, 2.0, 3.0, 4.5],
      f'got {fixed.tolist()}')
d._official_close = _orig_close
check("today's unsettled bar is dropped",
      d.repair_daily_closes('x', pd.Series([1.0, 2.0], index=pd.to_datetime(['2026-10-01', '2026-10-02'])),
                            datetime.date(2026, 10, 2)).index[-1].date() == datetime.date(2026, 10, 1))

# 5. The morning renderer: markers, wording, no AI.
asof = datetime.date(2026, 10, 1)
def holding(run, price, prev):
    return {'price': price, 'prev_cls': prev, 'pct': (price - prev) / prev * 100,
            'streak': {'run': run, 'pct': 1.0, 'as_of': asof}}
check('a 3+ up-run extending today is ⚡ with 賣出觀察',
      t._morning_run_lines(holding(5, 11, 10), 3)[0] == '⚡'
      and '賣出觀察' in t._morning_run_lines(holding(5, 11, 10), 3)[1])
check('a 3+ down-run breaking today is ⚠️ with 止跌觀察',
      t._morning_run_lines(holding(-4, 11, 10), 3)[0] == '⚠️'
      and '止跌觀察' in t._morning_run_lines(holding(-4, 11, 10), 3)[1])
check('a run under the threshold gets no marker',
      t._morning_run_lines(holding(2, 11, 10), 3)[0] == '•')
check("today's move is never folded into the count",
      '連漲 5 日' in t._morning_run_lines(holding(5, 11, 10), 3)[1])

# 6. Cash: with no ledger the totals keep their exact old shape.
pf = {'0050': {'shares': 1000, 'cost_basis': 100000, 'name': 'x'}}
quotes = {'0050': {'price': 110.0, 'prev_close': 100.0, 'intraday': True}}
check('no ledger → total{} has exactly the 7 original keys',
      sorted(d.compute_positions(pf, quotes, market_open=False)['total'])
      == sorted(['value', 'cost', 'daily_pnl', 'pnl', 'pnl_pct', 'n_priced', 'n_total']))
check('no ledger → no cash line in the report', t._cash_lines(
    d.compute_positions(pf, quotes, market_open=False)['total']) == [])

# 7. Cash ledger maths and guard rails (in the scratch copy).
d.add_cash_entry('open', 450000, '', date='2026-09-28')
d.add_cash_entry('deposit', 50000, '薪資轉入', date='2026-09-29')
d.add_cash_entry('update', 512400, '0050 配息入帳', date='2026-09-30')
d.add_cash_entry('withdraw', 20000, '個人開銷', date='2026-10-01')
st = d.cash_status()
check('ledger balance replays to 492,400', st['balance'] == 492400.0, str(st['balance']))
_hist = sorted(os.listdir(os.path.join(_tmp, d.CASH_HISTORY_DIR)))
check('every write after the first keeps a dated copy of the ledger before it',
      len(_hist) == 3 and _hist[0].startswith('cash_ledger_before_0002_'), str(_hist))
check('a dividend is NOT counted as money put in (net flow +30,000)', st['net_flow'] == 30000.0)
for args, why in ((('withdraw', 10**9, 'x'), 'overdraw'), (('deposit', 100, ''), 'missing note'),
                  (('open', 1, 'x'), 'second opening'), (('deposit', 1, 'x', '2099-01-01'), 'future date')):
    try:
        d.add_cash_entry(*args)
        check(f'{why} is refused', False)
    except ValueError:
        check(f'{why} is refused', True)
tot = d.compute_positions(pf, quotes, market_open=False)['total']
check('cash rides alongside: stock P/L is unchanged by cash', tot['pnl'] == 10000.0)
check('total wealth = stocks + cash', tot['total_wealth'] == 110000.0 + 492400.0)
check('a partial book hides the total and the split',
      d.compute_positions({**pf, '2330': {'shares': 1, 'cost_basis': 1}}, quotes,
                          market_open=False)['total']['total_wealth'] is None)
with open(d.cash_ledger_path(), 'a') as f:
    f.write('{not json\n')
d._cash_cache['key'] = None
check('a corrupt line is counted, not hidden', d.cash_status()['bad'] == 1)
try:
    d.add_cash_entry('deposit', 1, 'x')
    check('writes are refused while a line is unreadable', False)
except ValueError:
    check('writes are refused while a line is unreadable', True)

# 8. The morning voice reads the short slice only, and the closing voice is untouched.
morning = ("📊 2026-10-05 台股開盤快報\n🕐 價格時間 09:05:12（證交所即時）\n\n🌙 隔夜與開盤：\n"
           "• 加權指數：昨收 1 → 開盤 2 → 現 3（+1.00%）\n• S&P 500：1 (+1%)\n\n🎯 **持倉（今日）：**\n"
           "💰 今日損益 +1元 ｜ 總損益 +2元 (+3.0%)\n🏦 交割戶現金 9元（2026-10-05 更新）\n\n"
           "⚡ 甲 (0001)：1.00 → 開 1.00 → 現 1.10　+10.00%　今日 +1元\n"
           "     連漲 5 日 +9.15%（至 10/02 收盤）　→ 今日 +10.00%，連漲延續第 6 天 → 賣出觀察\n"
           "• 乙 (0002)：1.00 → 開 1.00 → 現 1.00　+0.00%　今日 +0元\n     連漲 1 日 +1.00%（至 10/02 收盤）\n")
spoken = rv.for_speech(morning)
check('morning voice is shorter than the report', len(spoken) < len(morning))
check('morning voice includes the run marker', '⚡ 甲 (0001) 連漲 5 日' in spoken)
check('morning voice never speaks cash', '🏦' not in spoken)
check('morning voice skips unflagged holdings', '乙' not in spoken)

if not OFFLINE:
    # 9. Live: the exchange feed and the whole morning snapshot (read-only).
    live = d.fetch_mis_quotes(['2330'], want_index=True)
    check("the index arrives under '_t00' and never leaks as 't00'",
          '_t00' in live and 't00' not in live and 'tse_t00' not in live)
    calls = []
    t.call_openrouter = lambda *a, **k: calls.append(1) or ''
    t._save_and_print = lambda *a, **k: None
    os.remove(d.cash_ledger_path()); d._cash_cache['key'] = None
    snap = d.snapshot('morning')
    check('morning TAIEX carries all six keys',
          set(snap['taiex'] or {}) >= {'close', 'last', 'open', 'prev_close', 'change', 'pct'})
    check('every morning holding has a numeric open or none',
          all(h['open_p'] is None or isinstance(h['open_p'], float) for h in snap['holdings']))
    d.snapshot = lambda mode, cfg=None, _s=snap: _s
    report = t.generate_morning_report()
    check('the morning push makes no OpenRouter call', not calls)
    check('the header shows the exchange price time', '🕐 價格時間' in report
          or snap['price_source'] not in ('mis', 'mixed'))

shutil.rmtree(_tmp, ignore_errors=True)
failed = _results.count(False)
print(f"\n{len(_results) - failed}/{len(_results)} passed" + (f", {failed} FAILED" if failed else ''))
sys.exit(1 if failed else 0)
