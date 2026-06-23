#!/usr/bin/env python3
"""
TWSE Daily Report — Config & Orchestration TUI
----------------------------------------------
Interactive terminal UI scoped to the single TWSE daily-report cron routine
(morning 開盤 + closing 收盤). Every panel orchestrates that routine's content,
structure, timing, AI, or delivery.

Panels: Portfolio (+CSV import) · Watchlist · Report Schedule · Layout & Content ·
        AI & Indicators · Preview & Send · Delivery & Keys

Run:
    python3 config_tui.py
    OPENCLAW_DATA_DIR=/path/to/data python3 config_tui.py
"""

import json
import os
import sys
import glob
import re
import csv
import datetime
import subprocess
import shutil
from pathlib import Path

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich import box
    from rich.prompt import Prompt, Confirm
    from rich.rule import Rule
except ImportError:
    print("Missing dependency: pip install rich")
    sys.exit(1)

console = Console()

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).parent

def _resolve_data_dir():
    """Find the persistent config/data directory."""
    candidates = []
    env = os.getenv('OPENCLAW_DATA_DIR', '')
    if env:
        candidates.append(Path(env))
    candidates += [
        SCRIPT_DIR / 'data',
        Path.home() / 'openclaw-infra' / 'agents' / 'financial-bot' / 'data',
        Path('/app/data'),
    ]
    for p in candidates:
        if p.exists() and p.is_dir():
            return p
    first = candidates[0] if candidates else SCRIPT_DIR / 'data'
    first.mkdir(parents=True, exist_ok=True)
    return first

DATA_DIR  = _resolve_data_dir()
DOWNLOADS = Path.home() / 'Downloads'


def _resolve_env_path():
    """Find the canonical .env file (outside DATA_DIR, sibling to it on disk)."""
    candidates = [
        Path(os.getenv('OPENCLAW_ENV_FILE', '')) if os.getenv('OPENCLAW_ENV_FILE') else None,
        DATA_DIR.parent / '.env',
        SCRIPT_DIR / '.env',
    ]
    for p in candidates:
        if p and p.exists():
            return p
    return DATA_DIR.parent / '.env'

ENV_PATH = _resolve_env_path()


def _config_path(filename):
    """Prefer DATA_DIR (persistent volume) over SCRIPT_DIR (image layer)."""
    p = DATA_DIR / filename
    return p if p.exists() else SCRIPT_DIR / filename


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------

def load_json(path):
    p = Path(path)
    if not p.exists():
        return {}
    with open(p, encoding='utf-8') as f:
        return json.load(f)


def save_json(path, data):
    """Atomic write so a scheduled report can never read a half-written file.

    Serialize to a temp file in the same directory, fsync, then os.replace
    (atomic on the same filesystem). Keeps a .bak of the previous contents so a
    bad edit can be rolled back. Guards the diagnosed race with the live cron
    report's unguarded json.load (twse_daily_report.py).
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():                                   # preserve last-known-good
        try:
            shutil.copy2(p, p.with_suffix(p.suffix + '.bak'))
        except Exception:
            pass
    tmp = p.with_suffix(p.suffix + '.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)                               # atomic swap


# ---------------------------------------------------------------------------
# bot_config validation — clamp known fields so a bad edit can't break the
# scheduled report or run up AI cost, then persist atomically (+.bak rollback).
# ---------------------------------------------------------------------------

def _clamp(val, lo, hi):
    try:
        v = float(val)
    except (TypeError, ValueError):
        return None
    return max(lo, min(hi, v))


def _sanitize_bot_config(cfg):
    """Mutate cfg in place, clamping numeric fields to safe ranges."""
    ai = cfg.get('ai')
    if isinstance(ai, dict):
        for k in ('max_tokens_reason', 'max_tokens_outlook', 'max_tokens_research'):
            if k in ai:
                c = _clamp(ai[k], 1, 4000)
                if c is not None:
                    ai[k] = int(c)
    tech = cfg.get('technicals')
    if isinstance(tech, dict):
        for k in ('rsi_overbought', 'rsi_oversold'):
            if k in tech:
                c = _clamp(tech[k], 0, 100)
                if c is not None:
                    tech[k] = int(c)
        if 'period_days' in tech:
            c = _clamp(tech['period_days'], 5, 365)
            if c is not None:
                tech['period_days'] = int(c)
        if 'divergence_threshold' in tech:
            c = _clamp(tech['divergence_threshold'], 0, 100)
            if c is not None:
                tech['divergence_threshold'] = round(c, 4)
    return cfg


def save_bot_config(cfg):
    """Validate + atomically persist bot_config.json (with .bak rollback)."""
    _sanitize_bot_config(cfg)
    save_json(_config_path('bot_config.json'), cfg)


def restore_bot_config_bak():
    """Revert bot_config.json to the last-known-good .bak. Returns True on success."""
    p = _config_path('bot_config.json')
    bak = Path(str(p) + '.bak')
    if not bak.exists():
        return False
    shutil.copy2(bak, p)
    return True


# ---------------------------------------------------------------------------
# .env helpers (simple KEY=VALUE format, comments/blank lines preserved)
# ---------------------------------------------------------------------------

def load_env_file(path):
    """Parse a simple .env file into a dict. Skips comments/blank lines."""
    env = {}
    p = Path(path)
    if not p.exists():
        return env
    with open(p, encoding='utf-8') as f:
        for line in f:
            stripped = line.rstrip('\n')
            if not stripped.strip() or stripped.strip().startswith('#'):
                continue
            if '=' in stripped:
                k, _, v = stripped.partition('=')
                env[k.strip()] = v.strip()
    return env


def save_env_file(path, env: dict):
    """Write KEY=VALUE pairs, preserving comments/ordering from the original file."""
    p = Path(path)
    lines_out = []
    seen = set()
    if p.exists():
        with open(p, encoding='utf-8') as f:
            for line in f:
                stripped = line.rstrip('\n')
                if not stripped.strip() or stripped.strip().startswith('#'):
                    lines_out.append(stripped)
                    continue
                if '=' in stripped:
                    k = stripped.split('=', 1)[0].strip()
                    if k in env:
                        lines_out.append(f'{k}={env[k]}')
                        seen.add(k)
                        continue
                lines_out.append(stripped)
    for k, v in env.items():
        if k not in seen:
            lines_out.append(f'{k}={v}')
    with open(p, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines_out) + '\n')


# ---------------------------------------------------------------------------
# TWSE code validation (cached)
# ---------------------------------------------------------------------------

_twse_cache: dict = {}

def validate_code(code: str):
    """Return stock name if code exists on TWSE/TPEX, else None."""
    global _twse_cache
    if not _twse_cache:
        try:
            import requests
            r = requests.get(
                'https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL',
                timeout=10)
            _twse_cache = {s['Code']: s['Name'] for s in r.json()}
            r2 = requests.get(
                'https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes',
                timeout=10)
            for s in r2.json():
                c = s.get('SecuritiesCompanyCode', '').strip()
                n = s.get('CompanyName', '').strip()
                if c and n:
                    _twse_cache.setdefault(c, n)
        except Exception:
            pass
    return _twse_cache.get(code.upper())


def _twse_name_to_code() -> dict:
    """Return inverted map: name → code, from TWSE + TPEX."""
    global _twse_cache
    if not _twse_cache:
        validate_code('__warmup__')
    return {v: k for k, v in _twse_cache.items()}


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------

def header(subtitle=''):
    console.clear()
    body = f"[bold cyan]Financial Report Config[/bold cyan]"
    if subtitle:
        body += f"\n[dim]{subtitle}[/dim]"
    console.print(Panel(
        body,
        subtitle=f"[dim]data: {DATA_DIR}[/dim]",
        box=box.DOUBLE_EDGE,
        border_style='cyan',
    ))


def pause(msg='Press Enter to continue...'):
    console.input(f"\n[dim]{msg}[/dim]")


# ---------------------------------------------------------------------------
# Main menu
# ---------------------------------------------------------------------------

def main_menu():
    while True:
        portfolio = load_json(_config_path('portfolio.json'))
        tracked   = load_json(_config_path('tracked_stocks.json'))
        total_cost = sum(v.get('cost_basis', 0) for v in portfolio.values())

        header('TWSE Daily Report — orchestration')
        t = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
        t.add_column(style='bold cyan', width=4)
        t.add_column(style='white', width=26)
        t.add_column(style='dim')
        t.add_row('[1]', 'Portfolio Holdings',
                  f'{len(portfolio)} stocks · {total_cost:,.0f}元 · [i] import CSV')
        t.add_row('[2]', 'Watchlist',
                  f'{len(tracked)} monitored (non-holding)')
        t.add_row('[3]', 'Report Schedule',
                  'morning 開盤 · closing 收盤 (UTC)')
        t.add_row('[4]', 'Layout & Content',
                  'sections · order · indices · news')
        t.add_row('[5]', 'AI & Indicators',
                  'model · tokens · RSI/divergence')
        t.add_row('[6]', 'Preview & Send',
                  'sandbox preview · ⚠ live send')
        t.add_row('[7]', 'Delivery & Keys',
                  f'Telegram · OpenRouter · Brave ({ENV_PATH.name})')
        t.add_row('[q]', 'Quit', '')
        console.print(t)

        choice = Prompt.ask('\nSelect', choices=['1','2','3','4','5','6','7','q'], default='q')

        if   choice == '1': menu_portfolio()
        elif choice == '2': menu_watchlist()
        elif choice == '3': menu_schedule()
        elif choice == '4': menu_report_layout()
        elif choice == '5': menu_ai_settings()
        elif choice == '6': menu_sandbox()
        elif choice == '7': menu_api_keys()
        else:
            console.print('\n[dim]Goodbye.[/dim]\n')
            break


# ---------------------------------------------------------------------------
# [1] Portfolio
# ---------------------------------------------------------------------------

def _portfolio_table(portfolio):
    t = Table(title='Portfolio Holdings', box=box.ROUNDED, show_lines=True)
    t.add_column('#',             style='dim',         width=4,  justify='right')
    t.add_column('Code',          style='bold cyan',   width=8)
    t.add_column('Name',                               width=16)
    t.add_column('Shares',        justify='right')
    t.add_column('Cost Basis (元)',justify='right')
    t.add_column('Avg/股 (元)',   justify='right',     style='yellow')

    for i, (code, pos) in enumerate(portfolio.items(), 1):
        sh   = pos.get('shares', 0)
        cost = pos.get('cost_basis', 0)
        avg  = f'{cost/sh:,.2f}' if sh else '—'
        t.add_row(str(i), code, pos.get('name',''),
                  f'{sh:,}', f'{int(cost):,}', avg)

    total = sum(v.get('cost_basis', 0) for v in portfolio.values())
    console.print(t)
    console.print(f'[dim]Total invested: [bold]{total:,.0f}元[/bold] across {len(portfolio)} positions[/dim]')


def menu_portfolio():
    while True:
        portfolio = load_json(_config_path('portfolio.json'))
        header('Portfolio Holdings')
        _portfolio_table(portfolio)

        console.print('\n[cyan][a][/cyan] Add  [cyan][e][/cyan] Edit  '
                      '[cyan][d][/cyan] Delete  [cyan][i][/cyan] Import CSV  '
                      '[cyan][b][/cyan] Back')
        choice = Prompt.ask('Action', choices=['a','e','d','i','b'], default='b')

        if   choice == 'a': _portfolio_add(portfolio)
        elif choice == 'e': _portfolio_edit(portfolio)
        elif choice == 'd': _portfolio_delete(portfolio)
        elif choice == 'i': menu_import_csv()
        else: break


def _portfolio_add(portfolio):
    console.print('\n[bold]Add holding[/bold]')
    code = Prompt.ask('Stock code').strip().upper()
    if code in portfolio:
        console.print(f'[yellow]{code} already in portfolio — use Edit.[/yellow]')
        pause(); return

    console.print(f'[dim]Validating {code}...[/dim] ', end='')
    api_name = validate_code(code)
    if api_name:
        console.print(f'[green]✓ {api_name}[/green]')
        name = Prompt.ask('Name', default=api_name)
    else:
        console.print('[yellow]⚠ not found on TWSE/TPEX (fund/ETF?)[/yellow]')
        name = Prompt.ask('Name (Chinese)')

    shares_s = Prompt.ask('Shares (total units held)')
    cost_s   = Prompt.ask('Total cost paid (付出成本, 元)')
    try:
        shares = int(shares_s.replace(',', ''))
        cost   = float(cost_s.replace(',', ''))
    except ValueError:
        console.print('[red]Invalid number.[/red]'); pause(); return

    avg = cost / shares if shares else 0
    console.print(f'\n[dim]Implied avg/股: [bold yellow]{avg:,.2f}元[/bold yellow][/dim]')

    if Confirm.ask(f'Add {code} {name} — {shares:,} shares, cost {int(cost):,}元?'):
        portfolio[code] = {'name': name, 'shares': shares, 'cost_basis': cost}
        save_json(_config_path('portfolio.json'), portfolio)
        console.print(f'[green]✓ {code} added.[/green]')
    pause()


def _portfolio_edit(portfolio):
    code = Prompt.ask('Code to edit').strip().upper()
    if code not in portfolio:
        console.print(f'[red]{code} not found.[/red]'); pause(); return

    pos = portfolio[code]
    console.print(f'\nEditing [bold cyan]{code}[/bold cyan] {pos.get("name","")}')
    name     = Prompt.ask('Name',            default=pos.get('name', ''))
    shares_s = Prompt.ask('Shares',          default=str(pos.get('shares', 0)))
    cost_s   = Prompt.ask('Cost basis (元)', default=str(int(pos.get('cost_basis', 0))))

    try:
        shares = int(shares_s.replace(',', ''))
        cost   = float(cost_s.replace(',', ''))
    except ValueError:
        console.print('[red]Invalid number.[/red]'); pause(); return

    avg = cost / shares if shares else 0
    console.print(f'[dim]New avg/股: [bold yellow]{avg:,.2f}元[/bold yellow][/dim]')

    if Confirm.ask(f'Save {code}?'):
        portfolio[code] = {'name': name, 'shares': shares, 'cost_basis': cost}
        save_json(_config_path('portfolio.json'), portfolio)
        console.print(f'[green]✓ {code} updated.[/green]')
    pause()


def _portfolio_delete(portfolio):
    code = Prompt.ask('Code to remove').strip().upper()
    if code not in portfolio:
        console.print(f'[red]{code} not found.[/red]'); pause(); return

    pos = portfolio[code]
    if Confirm.ask(f'[red]Remove {code} {pos.get("name","")} ({pos.get("shares",0):,} shares)?[/red]'):
        del portfolio[code]
        save_json(_config_path('portfolio.json'), portfolio)
        console.print(f'[green]✓ {code} removed.[/green]')
    pause()


# ---------------------------------------------------------------------------
# [2] Watchlist
# ---------------------------------------------------------------------------

def menu_watchlist():
    while True:
        tracked   = load_json(_config_path('tracked_stocks.json'))
        portfolio = load_json(_config_path('portfolio.json'))
        header('Watchlist — stocks monitored but not held')

        if tracked:
            t = Table(box=box.ROUNDED)
            t.add_column('Code', style='bold cyan')
            t.add_column('Name')
            t.add_column('Note', style='dim')
            for code, name in tracked.items():
                note = '⚠ also in portfolio' if code in portfolio else 'watch only'
                t.add_row(code, name, note)
            console.print(t)
        else:
            console.print('[dim]Empty — only portfolio holdings appear in the report.[/dim]')

        console.print('\n[cyan][a][/cyan] Add  [cyan][d][/cyan] Delete  [cyan][b][/cyan] Back')
        choice = Prompt.ask('Action', choices=['a','d','b'], default='b')

        if choice == 'a':
            code = Prompt.ask('Stock code').strip().upper()
            if code in portfolio:
                console.print(f'[yellow]{code} is a portfolio holding — already in report.[/yellow]')
            elif code in tracked:
                console.print(f'[yellow]{code} already in watchlist.[/yellow]')
            else:
                api_name = validate_code(code)
                name = Prompt.ask('Name', default=api_name or '')
                tracked[code] = name
                save_json(_config_path('tracked_stocks.json'), tracked)
                console.print(f'[green]✓ {code} added to watchlist.[/green]')
            pause()

        elif choice == 'd':
            code = Prompt.ask('Code to remove').strip().upper()
            if code not in tracked:
                console.print(f'[red]{code} not in watchlist.[/red]')
            elif Confirm.ask(f'Remove {code} from watchlist?'):
                del tracked[code]
                save_json(_config_path('tracked_stocks.json'), tracked)
                console.print(f'[green]✓ {code} removed.[/green]')
            pause()

        else:
            break


# ---------------------------------------------------------------------------
# [3] Schedule
# ---------------------------------------------------------------------------

def _utc_to_taiwan(utc_str):
    try:
        h, m = map(int, utc_str.split(':'))
        return f'{(h+8)%24:02d}:{m:02d}'
    except Exception:
        return '?'


def menu_schedule():
    cfg   = load_json(_config_path('bot_config.json'))
    sched = cfg.get('schedule', {})
    header('Schedule Settings')

    t = Table(box=box.ROUNDED)
    t.add_column('Report',          style='bold cyan')
    t.add_column('UTC',             style='yellow', justify='center')
    t.add_column('Taiwan (UTC+8)',  style='dim',    justify='center')

    m_utc = sched.get('morning_utc',  '01:30')
    c_utc = sched.get('closing_utc',  '08:00')

    t.add_row('Morning 開盤', m_utc, _utc_to_taiwan(m_utc))
    t.add_row('Closing 收盤', c_utc, _utc_to_taiwan(c_utc))
    console.print(t)
    console.print('[dim]Scheduler must be restarted inside the container for changes to apply.[/dim]\n')

    if Confirm.ask('Edit times?', default=False):
        new_m = Prompt.ask('Morning UTC (HH:MM)', default=m_utc)
        new_c = Prompt.ask('Closing UTC (HH:MM)', default=c_utc)

        if not (re.match(r'^\d{2}:\d{2}$', new_m) and re.match(r'^\d{2}:\d{2}$', new_c)):
            console.print('[red]Invalid format — must be HH:MM.[/red]')
        else:
            cfg.setdefault('schedule', {})
            cfg['schedule']['morning_utc'] = new_m
            cfg['schedule']['closing_utc'] = new_c
            save_bot_config(cfg)
            console.print(f'[green]✓ Saved — morning {new_m} UTC ({_utc_to_taiwan(new_m)} TW), '
                          f'closing {new_c} UTC ({_utc_to_taiwan(new_c)} TW).[/green]')
            console.print('[dim]Restart scheduler: docker exec openclaw-financial-bot '
                          'pkill -f scheduler.py && docker exec -d openclaw-financial-bot bash start.sh[/dim]')

    pause()


# ---------------------------------------------------------------------------
# [4] Import CSV
# ---------------------------------------------------------------------------

NAME_TO_CODE_FILE = DATA_DIR / 'name_to_code.json'


def _load_name_cache() -> dict:
    return load_json(NAME_TO_CODE_FILE) if NAME_TO_CODE_FILE.exists() else {}


def _save_name_cache(mapping: dict):
    save_json(NAME_TO_CODE_FILE, mapping)


def _parse_brokerage_csv(path: str) -> list:
    """Parse 證券未實現彙總 CSV → list of {name, shares, cost_basis}."""
    rows = []
    with open(path, encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = row.get('股票名稱', '').strip()
            if not name or name.startswith('總'):
                continue
            try:
                shares = int(row.get('股數', '0').replace(',', ''))
                cost   = float(row.get('付出成本', '0').replace(',', ''))
            except ValueError:
                continue
            if shares > 0:
                rows.append({'name': name, 'shares': shares, 'cost_basis': cost})
    return rows


def _resolve_names(rows: list):
    """Map stock names → codes. Returns (resolved_list, unknown_list)."""
    console.print('[dim]Fetching TWSE/TPEX name→code map...[/dim]')
    name_to_code = _twse_name_to_code()
    name_to_code.update(_load_name_cache())   # layer in cached manual mappings

    resolved, unknown = [], []
    for row in rows:
        code = name_to_code.get(row['name'])
        if code:
            resolved.append({**row, 'code': code})
        else:
            unknown.append(row)
    return resolved, unknown


def menu_import_csv():
    header('Import CSV — 證券未實現彙總')

    pattern = str(DOWNLOADS / '證券未實現彙總*.csv')
    files   = sorted(glob.glob(pattern), reverse=True)

    if not files:
        console.print(f'[yellow]No matching CSV files found in {DOWNLOADS}[/yellow]')
        console.print('[dim]Export from your brokerage app and drop into ~/Downloads/[/dim]')
        pause(); return

    t = Table(box=box.SIMPLE, show_header=True)
    t.add_column('#',    style='dim', width=3)
    t.add_column('File', width=50)
    t.add_column('Modified', style='dim')
    for i, f in enumerate(files[:8], 1):
        mtime = datetime.datetime.fromtimestamp(os.path.getmtime(f)).strftime('%Y-%m-%d %H:%M')
        t.add_row(str(i), os.path.basename(f), mtime)
    console.print(t)

    idx = Prompt.ask('Select file number', default='1')
    try:
        selected = files[int(idx) - 1]
    except (ValueError, IndexError):
        console.print('[red]Invalid selection.[/red]'); pause(); return

    console.print(f'\n[dim]Parsing {os.path.basename(selected)}...[/dim]')
    rows = _parse_brokerage_csv(selected)
    if not rows:
        console.print('[red]No valid rows found.[/red]'); pause(); return

    console.print(f'Found [bold]{len(rows)}[/bold] positions. Resolving codes...')
    resolved, unknown = _resolve_names(rows)

    # Prompt for unresolved names
    cache = _load_name_cache()
    if unknown:
        console.print(f'\n[yellow]{len(unknown)} stock(s) need manual code entry:[/yellow]')
        for row in unknown:
            code = Prompt.ask(
                f'  Code for [bold]{row["name"]}[/bold] '
                f'({row["shares"]:,} shares, cost {int(row["cost_basis"]):,}元)'
            ).strip().upper()
            if code:
                resolved.append({**row, 'code': code})
                cache[row['name']] = code
        _save_name_cache(cache)

    if not resolved:
        console.print('[red]Nothing to import.[/red]'); pause(); return

    # Build new portfolio dict
    new_portfolio = {
        r['code']: {'name': r['name'], 'shares': r['shares'], 'cost_basis': r['cost_basis']}
        for r in resolved
    }

    # Show diff
    current = load_json(_config_path('portfolio.json'))
    console.print('\n[bold]Changes to be applied:[/bold]')

    diff = Table(box=box.ROUNDED, show_lines=False)
    diff.add_column('Code',        style='bold cyan', width=8)
    diff.add_column('Name',        width=16)
    diff.add_column('Shares',      justify='right')
    diff.add_column('Cost (元)',   justify='right')
    diff.add_column('Status',      justify='center')

    for code, pos in new_portfolio.items():
        if code not in current:
            status = '[green]NEW[/green]'
        elif (current[code].get('shares') != pos['shares'] or
              current[code].get('cost_basis') != pos['cost_basis']):
            status = '[yellow]UPDATED[/yellow]'
        else:
            status = '[dim]unchanged[/dim]'
        diff.add_row(code, pos['name'], f"{pos['shares']:,}", f"{int(pos['cost_basis']):,}", status)

    for code, pos in current.items():
        if code not in new_portfolio:
            diff.add_row(code, pos.get('name',''), f"{pos.get('shares',0):,}",
                         f"{int(pos.get('cost_basis',0)):,}", '[red]REMOVED[/red]')

    console.print(diff)
    new_total = sum(r['cost_basis'] for r in resolved)
    console.print(f'[dim]New total cost basis: [bold]{new_total:,.0f}元[/bold] · {len(new_portfolio)} holdings[/dim]')

    if Confirm.ask('\nApply to portfolio.json?'):
        save_json(_config_path('portfolio.json'), new_portfolio)
        console.print(f'[green]✓ Portfolio updated — {len(new_portfolio)} holdings, {new_total:,.0f}元[/green]')

    pause()


# ---------------------------------------------------------------------------
# [5] Sandbox Run
# ---------------------------------------------------------------------------

def menu_sandbox():
    header('Sandbox Run — preview report (no Telegram push)')
    console.print('[cyan][1][/cyan] Morning  開盤快報')
    console.print('[cyan][2][/cyan] Closing  收盤報告')
    console.print('[cyan][3][/cyan] All reports')
    console.print('[red][4][/red] Send Now (LIVE — pushes to real Telegram)')
    console.print('[cyan][b][/cyan] Back\n')

    choice = Prompt.ask('Select', choices=['1','2','3','4','b'], default='b')
    if choice == 'b':
        return

    env = {**os.environ, 'OPENCLAW_DATA_DIR': str(DATA_DIR)}

    if choice == '4':
        sub = Prompt.ask('Send which? [1] Morning [2] Closing [3] All', choices=['1','2','3'], default='1')
        live_mode = {'1': 'morning', '2': 'closing', '3': 'all'}[sub]

        console.print(f"\n[bold red]⚠ This sends a REAL Telegram message for '{live_mode}'.[/bold red]")
        console.print('[dim]Note: scheduler.py skips on Taiwan weekends regardless of this confirmation.[/dim]')
        if not Confirm.ask(f"Confirm LIVE send for '{live_mode}'?", default=False):
            console.print('[dim]Cancelled.[/dim]'); pause(); return

        scheduler_path = SCRIPT_DIR / 'scheduler.py'
        if not scheduler_path.exists():
            console.print(f'[red]scheduler.py not found at {scheduler_path}[/red]')
            pause(); return

        live_env = {k: v for k, v in env.items() if k != 'SANDBOX_MODE'}
        console.print(f'\n[dim]Running scheduler.py --now={live_mode} (LIVE) …[/dim]')
        console.print(Rule(style='red'))
        try:
            subprocess.run([sys.executable, str(scheduler_path), f'--now={live_mode}'],
                           cwd=str(SCRIPT_DIR), env=live_env)
        except KeyboardInterrupt:
            console.print('\n[yellow]Stopped.[/yellow]')
        console.print(Rule(style='red'))
        pause()
        return

    mode_map = {'1': 'morning', '2': 'closing', '3': 'all'}
    mode = mode_map[choice]

    sandbox = SCRIPT_DIR / 'sandbox_run.py'
    if not sandbox.exists():
        console.print(f'[red]sandbox_run.py not found at {sandbox}[/red]')
        pause(); return

    console.print(f'\n[dim]Running --mode={mode} … (Ctrl+C to stop)[/dim]')
    console.print(Rule(style='dim'))
    try:
        subprocess.run([sys.executable, str(sandbox), f'--mode={mode}'],
                       cwd=str(SCRIPT_DIR), env=env)
    except KeyboardInterrupt:
        console.print('\n[yellow]Stopped.[/yellow]')
    console.print(Rule(style='dim'))
    pause()


# ---------------------------------------------------------------------------
# [6] AI Settings
# ---------------------------------------------------------------------------

AVAILABLE_MODELS = [
    'anthropic/claude-haiku-3-5',
    'anthropic/claude-sonnet-4',
    'deepseek/deepseek-chat',
    'google/gemini-2.0-flash-001',
]


def menu_ai_settings():
    while True:
        cfg  = load_json(_config_path('bot_config.json'))
        ai   = cfg.setdefault('ai', {})
        tech = cfg.setdefault('technicals', {})
        header('AI Settings')

        t = Table(box=box.ROUNDED)
        t.add_column('Setting', style='bold cyan')
        t.add_column('Value', style='yellow')
        t.add_row('Model',                          ai.get('model', 'anthropic/claude-haiku-3-5'))
        t.add_row('Max tokens — 個股原因 (reason)',   str(ai.get('max_tokens_reason', 100)))
        t.add_row('Max tokens — 開盤展望 (outlook)',  str(ai.get('max_tokens_outlook', 200)))
        t.add_row('Max tokens — 收盤研究 (research)', str(ai.get('max_tokens_research', 350)))
        console.print(t)
        console.print()

        t2 = Table(title='Technical thresholds', box=box.ROUNDED)
        t2.add_column('Setting', style='bold cyan')
        t2.add_column('Value', style='yellow')
        t2.add_row('RSI overbought ≥',          str(tech.get('rsi_overbought', 80)))
        t2.add_row('RSI oversold ≤',            str(tech.get('rsi_oversold', 30)))
        t2.add_row('Divergence threshold (%)',  str(tech.get('divergence_threshold', 1.5)))
        t2.add_row('Technicals lookback (days)', str(tech.get('period_days', 20)))
        console.print(t2)

        console.print('\n[cyan][1][/cyan] Edit model  [cyan][2][/cyan] Edit token limits  '
                      '[cyan][3][/cyan] Edit thresholds  [cyan][b][/cyan] Back')
        choice = Prompt.ask('Action', choices=['1','2','3','b'], default='b')

        if choice == '1':
            console.print('\nAvailable models:')
            for i, m in enumerate(AVAILABLE_MODELS, 1):
                console.print(f'  [{i}] {m}')
            sel = Prompt.ask('Model # (or type a custom model id)', default='')
            if sel.isdigit() and 1 <= int(sel) <= len(AVAILABLE_MODELS):
                ai['model'] = AVAILABLE_MODELS[int(sel) - 1]
            elif sel:
                ai['model'] = sel.strip()
            if sel:
                save_bot_config(cfg)
                console.print(f"[green]✓ Model set to {ai['model']}.[/green]")
            pause()

        elif choice == '2':
            for key, label in [('max_tokens_reason', '個股原因'),
                                ('max_tokens_outlook', '開盤展望'),
                                ('max_tokens_research', '收盤研究')]:
                val = Prompt.ask(f'{label} max_tokens', default=str(ai.get(key, 100)))
                try:
                    ai[key] = int(val)
                except ValueError:
                    console.print(f'[red]Invalid number for {label}, skipped.[/red]')
            save_bot_config(cfg)
            console.print('[green]✓ Token limits saved.[/green]')
            pause()

        elif choice == '3':
            for key, label, cast in [('rsi_overbought', 'RSI overbought', int),
                                      ('rsi_oversold', 'RSI oversold', int),
                                      ('divergence_threshold', 'Divergence threshold (%)', float),
                                      ('period_days', 'Lookback days', int)]:
                val = Prompt.ask(label, default=str(tech.get(key, '')))
                try:
                    tech[key] = cast(val)
                except ValueError:
                    console.print(f'[red]Invalid value for {label}, skipped.[/red]')
            save_bot_config(cfg)
            console.print('[green]✓ Thresholds saved.[/green]')
            pause()

        else:
            break


# ---------------------------------------------------------------------------
# [7] API Keys
# ---------------------------------------------------------------------------

# Scoped to ONLY the secrets the TWSE daily report actually reads
# (twse_daily_report.py): Telegram delivery + OpenRouter (AI) + Brave (news).
# Infra/other-routine keys (Robinhood, OpenClaw, network, alt models) are out of scope.
_ENV_KEY_ORDER = [
    'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID',
    'OPENROUTER_API_KEY',
    'BRAVE_API_KEY',
]


def _mask_secret(value):
    if not value:
        return '[dim](empty)[/dim]'
    if len(value) <= 8:
        return '*' * len(value)
    return value[:4] + '*' * (len(value) - 8) + value[-4:]


def menu_api_keys():
    while True:
        env = load_env_file(ENV_PATH)
        header('Delivery & Keys — secrets used by the daily report')
        console.print(f'[dim]File: {ENV_PATH}[/dim]')
        console.print('[yellow]⚠ Edits here update the .env on disk only — '
                      'the running container picks them up on next restart, '
                      'not immediately.[/yellow]\n')

        t = Table(box=box.ROUNDED)
        t.add_column('#', style='dim', width=3, justify='right')
        t.add_column('Key', style='bold cyan', width=24)
        t.add_column('Value (masked)')
        for i, k in enumerate(_ENV_KEY_ORDER, 1):
            t.add_row(str(i), k, _mask_secret(env.get(k, '')))
        console.print(t)

        console.print('\n[cyan][e][/cyan] Edit a key  [cyan][b][/cyan] Back')
        choice = Prompt.ask('Action', choices=['e','b'], default='b')

        if choice == 'e':
            idx = Prompt.ask('Key # to edit', default='')
            try:
                key = _ENV_KEY_ORDER[int(idx) - 1]
            except (ValueError, IndexError):
                console.print('[red]Invalid #.[/red]'); pause(); continue

            console.print(f'\nEditing [bold cyan]{key}[/bold cyan]')
            console.print(f'[dim]Current: {_mask_secret(env.get(key, ""))}[/dim]')
            new_val = Prompt.ask('New value (blank to cancel)', password=True, default='')
            if new_val and Confirm.ask(f'Save new value for {key}?'):
                env[key] = new_val
                save_env_file(ENV_PATH, env)
                console.print(f'[green]✓ {key} updated.[/green]')
                console.print('[dim]Restart the container for this to take effect:[/dim]')
                console.print('[dim]  docker compose restart openclaw-financial-bot[/dim]')
            pause()
        else:
            break


# ---------------------------------------------------------------------------
# [8] Report Layout
# ---------------------------------------------------------------------------

MORNING_SECTION_LABELS = [
    ('market_overview', '市場總覽'),
    ('global_markets',  '全球市場'),
    ('holdings',        '持倉列表'),
    ('cost_line',       '  └ 損益摘要行'),
    ('watchlist',       '觀察清單'),
    ('ai_outlook',      'AI 開盤展望'),
]

CLOSING_SECTION_LABELS = [
    ('market_overview', '市場總覽'),
    ('global_markets',  '全球市場'),
    ('hotlist',         '市場熱點掃描'),
    ('holdings',        '持倉列表'),
    ('cost_line',       '  └ 損益摘要行'),
    ('watchlist',       '觀察清單'),
    ('news',            '財經新聞'),
    ('ai_research',     'AI 產業研究/推薦'),
]


def _sections_table(title, labels, section_cfg):
    t = Table(title=title, box=box.ROUNDED)
    t.add_column('#', style='dim', width=3, justify='right')
    t.add_column('Section', width=22)
    t.add_column('Shown', justify='center')
    for i, (key, label) in enumerate(labels, 1):
        on = section_cfg.get(key, True)
        t.add_row(str(i), label, '[green]✓[/green]' if on else '[red]✗[/red]')
    console.print(t)


def _edit_sections(labels, section_cfg):
    """Toggle individual section keys via repeated number prompts."""
    while True:
        idx = Prompt.ask('Toggle # (blank to finish)', default='')
        if not idx:
            break
        try:
            key, label = labels[int(idx) - 1]
        except (ValueError, IndexError):
            console.print('[red]Invalid #.[/red]')
            continue
        new_val = not section_cfg.get(key, True)
        if not new_val:   # guard: never let the report become empty
            on_count = sum(1 for k, _ in labels if section_cfg.get(k, True))
            if on_count <= 1:
                console.print('[red]At least one section must stay enabled — '
                              'cannot disable the last one.[/red]')
                continue
        section_cfg[key] = new_val
        console.print(f"{label}: {'[green]ON[/green]' if section_cfg[key] else '[red]OFF[/red]'}")
    return section_cfg


def _effective_order(labels, current_order):
    """Known keys from current_order (in order), then any missing appended in default order."""
    valid_keys = [k for k, _ in labels]
    order = [k for k in (current_order or []) if k in valid_keys]
    for k in valid_keys:
        if k not in order:
            order.append(k)
    return order


def _edit_order(labels, current_order):
    """Reorder sections → returns a new permutation of known keys, or None to cancel.

    Guardrail: result must be a full permutation (every section exactly once), so a
    reorder can never drop or duplicate a section in the live report.
    """
    label_map = dict(labels)
    order = _effective_order(labels, current_order)
    console.print('\n[bold]Current order:[/bold]')
    for i, k in enumerate(order, 1):
        console.print(f'  [{i}] {label_map[k]}')
    raw = Prompt.ask(f'New order — comma-separated #s covering 1..{len(order)} '
                     '(blank to cancel)', default='')
    if not raw.strip():
        return None
    try:
        idxs = [int(x) for x in raw.replace(' ', '').split(',') if x]
    except ValueError:
        console.print('[red]Invalid — use numbers like 3,1,2,...[/red]')
        return None
    if sorted(idxs) != list(range(1, len(order) + 1)):
        console.print(f'[red]Must be a permutation of 1..{len(order)} '
                      '(each section exactly once).[/red]')
        return None
    return [order[i - 1] for i in idxs]


def _edit_news(cfg):
    """Edit Brave news queries + headline count (report's 財經新聞 section)."""
    news = cfg.setdefault('news', {})
    console.print('\n[bold]News settings (Brave search)[/bold]')
    news['query_morning'] = Prompt.ask('Morning query',
                                        default=news.get('query_morning', '台股 今日 財經'))
    news['query_closing'] = Prompt.ask('Closing query',
                                        default=news.get('query_closing', '台股 今日 財經 股市'))
    c = _clamp(Prompt.ask('Headline count (1-20)', default=str(news.get('count', 5))), 1, 20)
    if c is not None:
        news['count'] = int(c)


def _menu_stock_notes(cfg):
    """Per-stock notes — free-text annotations rendered under each holding in the report."""
    notes = cfg.setdefault('notes', {})
    portfolio = load_json(_config_path('portfolio.json')) or {}
    tracked   = load_json(_config_path('tracked_stocks.json')) or {}
    name_of = {code: pos.get('name', code) for code, pos in portfolio.items()}
    for code, name in tracked.items():
        name_of.setdefault(code, name)
    while True:
        header('Per-stock Notes — shown under each holding (📝)')
        t = Table(box=box.ROUNDED)
        t.add_column('Code', style='cyan', width=8)
        t.add_column('Name', width=14)
        t.add_column('Note')
        for code in sorted(set(name_of) | set(notes)):
            t.add_row(code, name_of.get(code, '—'), notes.get(code) or '[dim]—[/dim]')
        console.print(t)
        console.print('\n[cyan][a][/cyan] Add/Edit  [cyan][d][/cyan] Delete  [cyan][b][/cyan] Back')
        choice = Prompt.ask('Action', choices=['a', 'd', 'b'], default='b')
        if choice == 'a':
            code = Prompt.ask('Stock code').strip()
            if code:
                text = Prompt.ask('Note (blank to clear)', default=notes.get(code, '')).strip()
                if text:
                    notes[code] = text
                else:
                    notes.pop(code, None)
                save_bot_config(cfg)
                console.print('[green]✓ Saved.[/green]')
            pause()
        elif choice == 'd':
            code = Prompt.ask('Code to remove note').strip()
            if notes.pop(code, None) is not None:
                save_bot_config(cfg)
                console.print(f'[green]✓ Removed note for {code}.[/green]')
            else:
                console.print('[yellow]No note for that code.[/yellow]')
            pause()
        else:
            break


def _edit_report_style(cfg):
    """Custom header (above the report title) and footer (at the very bottom) text."""
    style = cfg.setdefault('report_style', {})
    header('Header / Footer — custom text wrapping the report')
    console.print('[dim]Header prints above the 📊 title line; footer at the very bottom. '
                  'Applies to both morning + closing. Blank to clear.[/dim]\n')
    style['header'] = Prompt.ask('Header text', default=style.get('header', '')).strip()
    style['footer'] = Prompt.ask('Footer text', default=style.get('footer', '')).strip()
    save_bot_config(cfg)
    console.print('[green]✓ Header/footer saved.[/green]')
    pause()


def menu_report_layout():
    while True:
        cfg = load_json(_config_path('bot_config.json'))
        cfg.setdefault('sections', {})
        cfg['sections'].setdefault('morning', {})
        cfg['sections'].setdefault('closing', {})

        header('Layout & Content — sections · order · indices · news')
        _sections_table('Morning Report 開盤快報', MORNING_SECTION_LABELS, cfg['sections']['morning'])
        m_order = _effective_order(MORNING_SECTION_LABELS, cfg['sections'].get('morning_order'))
        console.print(f'[dim]Morning order: {" → ".join(m_order)}[/dim]')
        console.print()
        _sections_table('Closing Report 收盤報告', CLOSING_SECTION_LABELS, cfg['sections']['closing'])
        c_order = _effective_order(CLOSING_SECTION_LABELS, cfg['sections'].get('closing_order'))
        console.print(f'[dim]Closing order: {" → ".join(c_order)}[/dim]')
        console.print()
        console.print('[dim]Toggles control what is displayed in the report text only — '
                      'underlying data fetches/AI calls still run regardless.[/dim]\n')

        idx_list = cfg.get('global_indices', [])
        gi = Table(title='Global Indices (全球市場)', box=box.SIMPLE)
        gi.add_column('#', style='dim', width=3, justify='right')
        gi.add_column('Label')
        gi.add_column('Ticker', style='cyan')
        for i, pair in enumerate(idx_list, 1):
            gi.add_row(str(i), pair[0], pair[1])
        console.print(gi)

        console.print('\n[cyan][1][/cyan] Edit Morning sections  '
                      '[cyan][2][/cyan] Edit Closing sections  '
                      '[cyan][3][/cyan] Reorder sections')
        console.print('[cyan][4][/cyan] Manage Global Indices  '
                      '[cyan][5][/cyan] News settings')
        console.print('[cyan][6][/cyan] Per-stock notes  '
                      '[cyan][7][/cyan] Header/Footer text  '
                      '[cyan][r][/cyan] Revert to last good  '
                      '[cyan][b][/cyan] Back')
        choice = Prompt.ask('Action', choices=['1','2','3','4','5','6','7','r','b'], default='b')

        if choice == '1':
            _edit_sections(MORNING_SECTION_LABELS, cfg['sections']['morning'])
            save_bot_config(cfg)
        elif choice == '2':
            _edit_sections(CLOSING_SECTION_LABELS, cfg['sections']['closing'])
            save_bot_config(cfg)
        elif choice == '3':
            which = Prompt.ask('Reorder which? [1] Morning [2] Closing', choices=['1','2'], default='1')
            if which == '1':
                new_order = _edit_order(MORNING_SECTION_LABELS, cfg['sections'].get('morning_order'))
                if new_order:
                    cfg['sections']['morning_order'] = new_order
                    save_bot_config(cfg)
                    console.print('[green]✓ Morning order saved.[/green]')
            else:
                new_order = _edit_order(CLOSING_SECTION_LABELS, cfg['sections'].get('closing_order'))
                if new_order:
                    cfg['sections']['closing_order'] = new_order
                    save_bot_config(cfg)
                    console.print('[green]✓ Closing order saved.[/green]')
            pause()
        elif choice == '4':
            _menu_global_indices(cfg)
        elif choice == '5':
            _edit_news(cfg)
            save_bot_config(cfg)
            console.print('[green]✓ News settings saved.[/green]')
            pause()
        elif choice == '6':
            _menu_stock_notes(cfg)
        elif choice == '7':
            _edit_report_style(cfg)
        elif choice == 'r':
            if restore_bot_config_bak():
                console.print('[green]✓ Reverted bot_config.json to last-known-good (.bak).[/green]')
            else:
                console.print('[yellow]No .bak found yet — nothing to revert.[/yellow]')
            pause()
        else:
            break


def _menu_global_indices(cfg):
    idx_list = cfg.setdefault('global_indices', [])
    while True:
        header('Global Indices')
        t = Table(box=box.ROUNDED)
        t.add_column('#', style='dim', width=3, justify='right')
        t.add_column('Label')
        t.add_column('Ticker', style='cyan')
        for i, pair in enumerate(idx_list, 1):
            t.add_row(str(i), pair[0], pair[1])
        console.print(t)

        console.print('\n[cyan][a][/cyan] Add  [cyan][d][/cyan] Delete  [cyan][b][/cyan] Back')
        choice = Prompt.ask('Action', choices=['a','d','b'], default='b')

        if choice == 'a':
            label = Prompt.ask('Label (display name)').strip()
            sym   = Prompt.ask('Ticker (Yahoo Finance symbol, e.g. ^GSPC)').strip()
            if label and sym:
                idx_list.append([label, sym])
                save_bot_config(cfg)
                console.print(f'[green]✓ Added {label} ({sym}).[/green]')
            pause()
        elif choice == 'd':
            num = Prompt.ask('Index # to remove', default='')
            try:
                removed = idx_list.pop(int(num) - 1)
                save_bot_config(cfg)
                console.print(f'[green]✓ Removed {removed[0]} ({removed[1]}).[/green]')
            except (ValueError, IndexError):
                console.print('[red]Invalid #.[/red]')
            pause()
        else:
            break


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    try:
        main_menu()
    except KeyboardInterrupt:
        console.print('\n[dim]Interrupted.[/dim]\n')
