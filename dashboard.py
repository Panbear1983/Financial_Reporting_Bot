"""
dashboard — the single data layer for this project ("the brain").

Everything that fetches a number or works one out lives HERE, and has exactly one
implementation. Two things read from it and neither derives anything of its own:

  * live_portfolio.py — the dashboard screen (live holdings table + graphs)
  * twse_daily_report.py — the twice-daily Telegram push (pure rendering)

WHY: the board and the report used to be independent pipelines that happened to
share a holdings file. Each fetched its own prices and redid the arithmetic, so
they drifted. On 2026-09-11 the morning push reported 今日損益 -102,150元 while
the board showed about -88,000元, because the report derived 昨收 by counting
backwards through a Yahoo daily frame that silently omits ETF sessions. The board
had already hit and fixed that exact defect in August; the fix had nowhere to
propagate to. Two pipelines means two places to be wrong and one place to fix it.

So: if you are adding a number to the report, add it here and render it there.
A fetch or a calculation in a rendering module is a bug in the making.
"""

import os
import re
import csv
import json
import time
import datetime
import urllib.request
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

import requests
import numpy as np
import pandas as pd
import yfinance as yf
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from market_data_fetcher import TWSDDataSource
from custom_stock_lookup import get_yfinance_data


# ---------------------------------------------------------------------------
# Paths, environment, config
# ---------------------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


DATA_DIR   = os.getenv('FRB_DATA_DIR', os.path.join(SCRIPT_DIR, 'data'))


# Load .env for standalone/preview runs (OPENROUTER_* / TELEGRAM_* / BRAVE_API_KEY).
# Honours FRB_ENV_FILE, else the data-dir / script-dir .env. override=False so
# an already-populated container environment is never clobbered. Silent no-op if
# python-dotenv is absent — matches config_tui.py's pattern.
def _load_env_file():
    for cand in (os.getenv('FRB_ENV_FILE'),
                 os.path.join(DATA_DIR, '.env'),
                 os.path.join(SCRIPT_DIR, '.env')):
        if cand and os.path.exists(cand):
            try:
                from dotenv import load_dotenv
                load_dotenv(cand, override=False)
            except Exception:
                pass
            return


def _config_path(filename):
    """Prefer data-dir (persistent volume) over script-dir (image layer)."""
    data_path = os.path.join(DATA_DIR, filename)
    if os.path.exists(data_path):
        return data_path
    return os.path.join(SCRIPT_DIR, filename)


def _now():
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def load_tracked_stocks():
    path = _config_path('tracked_stocks.json')
    if not os.path.exists(path):
        return {}
    with open(path, 'r', encoding='utf-8') as f:
        raw = json.load(f)
    # Values may be a plain name (legacy) or {"name": ..., "note": ...}.
    # The report only needs the display name, so normalise to {code: name}.
    return {
        code: (val.get('name', code) if isinstance(val, dict) else val)
        for code, val in raw.items()
    }


def load_tracked_notes():
    """Return {code: note} for watchlist entries that carry a user note.

    Legacy string entries have no note and are omitted. Used by the closing
    report to surface the note under each watchlist line.
    """
    path = _config_path('tracked_stocks.json')
    if not os.path.exists(path):
        return {}
    with open(path, 'r', encoding='utf-8') as f:
        raw = json.load(f)
    return {
        code: val['note']
        for code, val in raw.items()
        if isinstance(val, dict) and val.get('note')
    }


def load_portfolio():
    path = _config_path('portfolio.json')
    if not os.path.exists(path):
        return {}
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    # Drop entries with zero shares so they're treated as untracked
    return {code: pos for code, pos in data.items() if pos.get('shares', 0) > 0}


def load_bot_config():
    path = _config_path('bot_config.json')
    if not os.path.exists(path):
        return {}
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


_load_env_file()



# ------------------------------------------------------------------
# Symbols, quotes and position maths (was: live_portfolio)
# ------------------------------------------------------------------


_QUOTE_URL = 'https://query1.finance.yahoo.com/v7/finance/quote'


_OHLC = ['Open', 'High', 'Low', 'Close']


def _suffix_cache_path(data_dir=None):
    """The one ticker-suffix cache. The board passed a data_dir; the report used
    DATA_DIR and had its own copy of this — same file, two implementations."""
    return Path(data_dir or DATA_DIR) / 'ticker_suffix_cache.json'


def resolve_symbols(codes, data_dir):
    """Return {code: full_symbol}. Probes .TW then .TWO once per unknown code
    and persists the result so refresh cycles never re-probe."""
    cache_path = _suffix_cache_path(data_dir)
    try:
        cache = json.loads(cache_path.read_text(encoding='utf-8'))
    except Exception:
        cache = {}
    dirty = False
    out = {}
    for code in codes:
        suffix = cache.get(code)
        if suffix not in ('.TW', '.TWO'):
            suffix = '.TW'
            for s in ('.TW', '.TWO'):
                try:
                    hist = yf.Ticker(f'{code}{s}').history(period='5d')
                    if not hist.empty:
                        suffix = s
                        break
                except Exception:
                    continue
            cache[code] = suffix
            dirty = True
        out[code] = f'{code}{suffix}'
    if dirty:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(cache, indent=2), encoding='utf-8')
        except Exception:
            pass
    return out


_TW_CODE_RE = re.compile(r'^\d{4,6}[A-Z]?$')


def resolve_any(text, data_dir):
    """Resolve arbitrary user input to a yfinance symbol. TW-style codes
    (2330, 00402A) probe .TW/.TWO via the cache; anything else (AAPL, NVDA,
    ^TWII, BTC-USD) passes through uppercased."""
    text = text.strip().upper()
    if not text:
        return None
    if _TW_CODE_RE.match(text):
        return resolve_symbols([text], data_dir)[text]
    return text


def _clean_bars(sub):
    """Drop all-NaN rows and null out non-positive prices (bad Yahoo ticks
    would otherwise plot as candles crashing to zero)."""
    sub = sub.dropna(how='all').copy()
    cols = [c for c in _OHLC if c in sub.columns]
    sub[cols] = sub[cols].where(sub[cols] > 0)
    sub = sub.dropna(subset=cols, how='any')
    # Normalize intraday timestamps to Taipei so charts show 09:00–13:30 and
    # cross-symbol index unions never mix timezones.
    if getattr(sub.index, 'tz', None) is not None:
        sub.index = sub.index.tz_convert('Asia/Taipei')
    return sub


def fetch_history(symbols, period, interval):
    """One batched download for all symbols → {symbol: DataFrame(OHLCV)}."""
    df = yf.download(list(symbols), period=period, interval=interval,
                     group_by='ticker', auto_adjust=False,
                     threads=True, progress=False)
    out = {}
    if df is None or df.empty:
        return out
    if isinstance(df.columns, pd.MultiIndex):
        for sym in symbols:
            if sym in df.columns.get_level_values(0):
                sub = _clean_bars(df[sym])
                if not sub.empty:
                    out[sym] = sub
    else:                                   # single-ticker downloads come back flat
        sub = _clean_bars(df)
        if not sub.empty:
            out[list(symbols)[0]] = sub
    return out


def _quote_time(epoch):
    """Yahoo's regularMarketTime (unix secs) → local-tz datetime, or None."""
    try:
        return (datetime.datetime.fromtimestamp(int(epoch), datetime.timezone.utc)
                .astimezone())
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def fetch_quote_batch(symbols):
    """{symbol: raw Yahoo quote} for every symbol in ONE authenticated request.

    yf.download — the path the live board used to take — issues one HTTP call per
    ticker, so quote cadence scaled with portfolio size and any refresh fast enough
    to look live risked the 429s that once blanked the board. This endpoint returns
    the whole book in a single call, so the cadence is independent of how many
    holdings there are.

    It also carries the authoritative regularMarketPreviousClose. Deriving that
    from a 5d daily frame's second-to-last ROW silently picked the wrong DAY
    whenever Yahoo's frame had a hole (0050/006208 were missing 2026-08-12), which
    quietly corrupted Chg% and Daily P/L. Returns {} on any failure — callers fall
    back to the frame path.
    """
    try:
        import yfinance.data as yfd
        r = yfd.YfData().get_raw_json(_QUOTE_URL, params={'symbols': ','.join(symbols)})
        return {q['symbol']: q for q in r.get('quoteResponse', {}).get('result', [])
                if q.get('symbol')}
    except Exception:
        return {}


def fetch_live_quotes(codes, symbol_map, daily=None):
    """{code: {price, prev_close, intraday, quote_at, ...}} for the live board.

    Primary path is one batched quote call (fetch_quote_batch). Anything it
    doesn't cover falls back to per-symbol OHLC frames — prev_close from the daily
    frame, price from the freshest 1-minute bar when one exists, else the daily
    close. `daily` lets a caller inject that frame instead of fetching it.

    quote['intraday'] records whether the price can still move this session: a
    batched market quote can, a price that fell back to the daily close cannot,
    and the board marks the latter rather than letting it read as a flat market.
    quote['quote_at'] is the exchange-side timestamp (datetime, when Yahoo gives
    one) — the honest answer to "how live is this number", since the TW feed is
    delayed and repainting the screen doesn't make a price newer.
    """
    syms = [symbol_map[c] for c in codes]
    batch = fetch_quote_batch(syms)

    quotes, missing = {}, []
    for code in codes:
        b = batch.get(symbol_map[code]) or {}
        price, prev_close = b.get('regularMarketPrice'), b.get('regularMarketPreviousClose')
        if price is None or prev_close is None:
            missing.append(code)
            continue
        price, prev_close = float(price), float(prev_close)
        qt = b.get('regularMarketTime')
        quotes[code] = {
            'name': b.get('shortName') or code, 'price': price, 'prev_close': prev_close,
            'intraday': True, 'quote_at': _quote_time(qt),
            'today_open': float(b.get('regularMarketOpen') or price),
            'change': price - prev_close, 'intraday_change': 0.0,
        }

    # Frame fallback, for the missing symbols only — usually nobody.
    if missing:
        msyms = [symbol_map[c] for c in missing]
        if daily is None:
            daily = fetch_history(msyms, period='5d', interval='1d')
        try:
            intra = fetch_history(msyms, period='1d', interval='1m')
        except Exception:
            intra = {}
        still_missing = []
        for code in missing:
            sym = symbol_map[code]
            d = daily.get(sym)
            closes = (d['Close'].dropna()
                      if d is not None and 'Close' in getattr(d, 'columns', []) else None)
            if closes is None or closes.empty:
                still_missing.append(code)
                continue
            prev_close = float(closes.iloc[-2]) if len(closes) >= 2 else float(closes.iloc[-1])
            price = float(closes.iloc[-1])
            live = False
            iv = intra.get(sym)
            if iv is not None and 'Close' in getattr(iv, 'columns', []):
                ic = iv['Close'].dropna()
                if not ic.empty:
                    price = float(ic.iloc[-1])   # freshest intraday price this session
                    live = True
            quotes[code] = {
                'name': code, 'price': price, 'prev_close': prev_close, 'intraday': live,
                'quote_at': None, 'today_open': price,
                'change': price - prev_close, 'intraday_change': 0.0,
            }
        missing = still_missing

    # Last-ditch per-stock fallback (hardened get_yfinance_data) for anything
    # neither the batch quote nor the OHLC frames could resolve — thin or
    # newly-listed symbols that Yahoo's bulk endpoints occasionally drop.
    if missing:
        def one(code):
            return code, get_yfinance_data(symbol_map[code])
        with ThreadPoolExecutor(max_workers=4) as ex:
            for code, q in ex.map(one, missing):
                if q:
                    # regularMarketPrice off the chart endpoint is a live quote,
                    # so this path is never the frozen-at-last-close case.
                    q.setdefault('intraday', True)
                    quotes[code] = q
    return quotes


_HOLIDAY_URL   = 'https://www.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule'
_HOLIDAY_CACHE = {}          # ROC year (int) -> {date_str: name}


def _holiday_cache_path():
    return os.path.join(DATA_DIR, 'market_holidays.json')


def fetch_market_holidays(roc_year, refresh=False):
    """{'YYYY-MM-DD': name} of days the exchange is CLOSED, from TWSE's own
    published holiday schedule (開休市日期), cached to disk.

    A weekday can still be a non-trading day — 中秋節, 國慶日, Lunar New Year.
    Nothing in this project knew that: every job gated on Mon–Fri alone and so
    published a full report on a closed market, quoting the previous session's
    prices as if they were today's.

    Returns {} if the calendar has never been fetched and the network fails —
    callers must treat that as "unknown", not as "no holidays", so a failed
    lookup can never silently take the whole schedule offline.
    """
    roc_year = int(roc_year)
    if not refresh and roc_year in _HOLIDAY_CACHE:
        return _HOLIDAY_CACHE[roc_year]

    path, disk = _holiday_cache_path(), {}
    try:
        with open(path, encoding='utf-8') as f:
            disk = json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        disk = {}
    cached = disk.get(str(roc_year))
    if cached and not refresh:
        _HOLIDAY_CACHE[roc_year] = cached
        return cached

    try:
        resp = requests.get(_HOLIDAY_URL, params={'response': 'json', 'queryYear': str(roc_year)},
                            timeout=20, verify=False)
        rows = resp.json().get('data') or []
        # ['2026-09-25', '中秋節', '依規定放假1日。'] — the exchange publishes the
        # Gregorian date in column 0 even though the query is by ROC year.
        found = {r[0].strip(): (r[1].strip() if len(r) > 1 else '')
                 for r in rows if r and re.match(r'^\d{4}-\d{2}-\d{2}$', str(r[0]).strip())}
    except Exception as exc:                                       # noqa: BLE001
        print(f"[{_now()}] Holiday calendar fetch failed ({roc_year}): "
              f"{type(exc).__name__}: {exc}")
        return cached or {}
    if not found:
        return cached or {}

    disk[str(roc_year)] = found
    disk['fetched_at'] = _now()
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(disk, f, ensure_ascii=False, indent=2)
    except OSError:
        pass
    _HOLIDAY_CACHE[roc_year] = found
    return found


def market_holiday_name(day=None):
    """Name of the holiday closing the exchange on `day`, '' if it trades, or
    None when the calendar is unavailable (unknown — never assume open)."""
    day = day or datetime.datetime.now(ZoneInfo('Asia/Taipei')).date()
    cal = fetch_market_holidays(day.year - 1911)
    if not cal:
        return None
    return cal.get(day.isoformat(), '')


def is_trading_day(day=None):
    """True when the exchange trades on `day`: a weekday that is not on TWSE's
    published holiday list. An unavailable calendar falls back to the weekday
    test alone — the old behaviour — so a network failure cannot mute the
    schedule outright."""
    day = day or datetime.datetime.now(ZoneInfo('Asia/Taipei')).date()
    if day.weekday() >= 5:
        return False
    return not market_holiday_name(day)


def taiwan_market_open(now=None):
    if os.getenv('LIVE_PORTFOLIO_FORCE_OPEN') == '1':
        return True
    now = now or datetime.datetime.now(ZoneInfo('Asia/Taipei'))
    if not is_trading_day(now.date()):
        return False
    t = now.time()
    return datetime.time(9, 0) <= t <= datetime.time(13, 30)


def compute_positions(portfolio, quotes, market_open=None):
    """THE position maths for this project — rows + totals, no rendering.

    Single source of truth, shared by the live board and by the TWSE morning
    report's 持倉 block. The report used to fetch its own prices and redo this
    arithmetic itself, so the two could (and did) disagree: on 2026-09-11 the
    Telegram push said 今日損益 -102,150元 against the board's -88,000元, because
    the report derived 昨收 by counting backwards through a Yahoo daily frame
    that silently omits ETF sessions. One fetch + one calculation removes that
    whole class of drift by construction.

    Returns {'rows': [...], 'total': {...}, 'n_unpriced': int, 'frozen': [codes]}.
    A holding with no quote is carried in rows with price=None and is excluded
    from the totals — its cost too, so one failed quote cannot book a position
    as a total loss.
    """
    if market_open is None:
        market_open = taiwan_market_open()

    rows, frozen, n_unpriced = [], [], 0
    tot_val = tot_cost = tot_daily_pnl = 0.0
    for code, pos in portfolio.items():
        sh, cost = pos.get('shares', 0), pos.get('cost_basis', 0)
        q = quotes.get(code)
        if not q:
            n_unpriced += 1
            rows.append({'code': code, 'name': pos.get('name', ''), 'shares': sh,
                         'cost': cost, 'price': None, 'prev_close': None,
                         'change': None, 'chg_pct': None, 'value': None,
                         'pnl': None, 'pnl_pct': None, 'daily_pnl': None,
                         'intraday': None, 'stale': False})
            continue

        price, prev = q['price'], q['prev_close']
        change    = (price - prev) if prev else 0.0
        chg_pct   = (change / prev * 100) if prev else 0.0
        daily_pnl = change * sh if prev else 0.0
        val       = price * sh
        pnl       = val - cost
        # A price that cannot tick this session (no intraday feed) is still a
        # real number, but the board and the report both have to say so.
        stale = bool(market_open and not q.get('intraday', True))
        if stale:
            frozen.append(code)

        tot_val       += val
        tot_cost      += cost
        tot_daily_pnl += daily_pnl
        rows.append({'code': code, 'name': pos.get('name', ''), 'shares': sh,
                     'cost': cost, 'price': price, 'prev_close': prev,
                     'change': change, 'chg_pct': chg_pct, 'value': val,
                     'pnl': pnl, 'pnl_pct': (pnl / cost * 100) if cost else 0.0,
                     'daily_pnl': daily_pnl, 'intraday': q.get('intraday', True),
                     'stale': stale})

    total = {
        'value': tot_val, 'cost': tot_cost, 'daily_pnl': tot_daily_pnl,
        'pnl': tot_val - tot_cost,
        'pnl_pct': ((tot_val - tot_cost) / tot_cost * 100) if tot_cost else 0.0,
        'n_priced': len(portfolio) - n_unpriced, 'n_total': len(portfolio),
    }
    return {'rows': rows, 'total': total, 'n_unpriced': n_unpriced, 'frozen': frozen}


def close_streak(closes, market_open=None, today=None):
    """Trailing run of same-direction daily closes → {'run': int, 'pct': float}, or None.

    run is signed: +3 = three straight higher closes, -2 = two straight lower,
    0 = the newest close was flat against the one before (a flat day resets).
    pct is the total move over that run, from the close just BEFORE it began
    to the newest close, in percent.

    Only closed sessions count. While the market is open Yahoo's daily frame
    carries today's in-progress bar; it is dropped here because it can still
    flip before 13:30 — today's live move is already on the board as Chg%.
    Returns None when fewer than two closes remain (nothing to compare).
    """
    if market_open is None:
        market_open = taiwan_market_open()
    s = pd.Series(closes).dropna()
    if market_open and len(s):
        today = today or datetime.datetime.now(ZoneInfo('Asia/Taipei')).date()
        last = s.index[-1]
        if (last.date() if hasattr(last, 'date') else last) == today:
            s = s.iloc[:-1]
    if len(s) < 2:
        return None
    vals = s.tolist()
    diffs = [b - a for a, b in zip(vals, vals[1:])]
    if diffs[-1] == 0:
        return {'run': 0, 'pct': 0.0}
    up = diffs[-1] > 0
    run = 0
    for d in reversed(diffs):
        if d == 0 or (d > 0) != up:
            break
        run += 1
    start = vals[-1 - run]
    pct = (vals[-1] / start - 1.0) * 100 if start else 0.0
    return {'run': run if up else -run, 'pct': pct}



# ------------------------------------------------------------------
# Market data: history, indices, technicals, official close, fundamentals
# ------------------------------------------------------------------


_HISTORY_CACHE = {}     # bare code -> DataFrame (daily OHLCV, up to 1y)


_INDEX_CACHE   = {}     # index symbol (^GSPC…) -> DataFrame (daily OHLCV)


_QUOTE_CACHE   = {}     # full symbol (2330.TW) -> get_yfinance_data dict|None


def _load_suffix_cache():
    try:
        with open(_suffix_cache_path(), encoding='utf-8') as f:
            c = json.load(f)
            return c if isinstance(c, dict) else {}
    except Exception:
        return {}


def _save_suffix_cache(cache):
    try:
        with open(_suffix_cache_path(), 'w', encoding='utf-8') as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        if os.getenv('FRB_DEBUG_FETCH'):
            print(f"[{_now()}] suffix cache save failed: {e}")


def _suffix_order(code):
    """Preferred suffix probe order for a code: cached suffix first, then the other."""
    cached = _load_suffix_cache().get(code)
    order = [cached] if cached in ('.TW', '.TWO') else []
    order += [s for s in ('.TW', '.TWO') if s not in order]
    return order


def _split_download(df, symbols):
    """Split a group_by='ticker' yf.download frame into {symbol: non-empty DataFrame}."""
    out = {}
    if df is None or getattr(df, 'empty', True):
        return out
    if isinstance(df.columns, pd.MultiIndex):
        level0 = set(df.columns.get_level_values(0))
        for s in symbols:
            if s in level0:
                sub = df[s].dropna(how='all')
                if not sub.empty:
                    out[s] = sub
    else:  # single-ticker downloads come back flat
        sub = df.dropna(how='all')
        if not sub.empty and symbols:
            out[symbols[0]] = sub
    return out


def prefetch_stock_histories(codes, period='1y'):
    """One batched yf.download for every watchlist/holding code → _HISTORY_CACHE.

    Codes with a known suffix (ticker_suffix_cache.json) download that symbol;
    unknown codes download BOTH .TW and .TWO in the same batch and keep whichever
    Yahoo returns data for, persisting the resolved suffix. auto_adjust=True matches
    yfinance's Ticker.history() default so downstream RSI / 量比 / 績效 values are
    unchanged vs the old per-stock calls. Best-effort: on failure the cache stays
    empty and callers fall back to individual fetches."""
    codes = list(dict.fromkeys(c for c in codes if c))   # dedupe, keep order
    if not codes:
        return
    cache = _load_suffix_cache()
    symbols = []
    for code in codes:
        suf = cache.get(code)
        if suf in ('.TW', '.TWO'):
            symbols.append(f"{code}{suf}")
        else:
            symbols += [f"{code}.TW", f"{code}.TWO"]
    try:
        df = yf.download(symbols, period=period, interval='1d', group_by='ticker',
                         auto_adjust=True, threads=True, progress=False)
    except Exception as e:
        if os.getenv('FRB_DEBUG_FETCH'):
            print(f"[{_now()}] batch history download failed: {e}")
        return
    parts = _split_download(df, symbols)
    dirty = False
    for code in codes:
        suf = cache.get(code)
        cands = ([f"{code}{suf}"] if suf in ('.TW', '.TWO')
                 else [f"{code}.TW", f"{code}.TWO"])
        chosen = next((s for s in cands if s in parts), None)
        if chosen:
            _HISTORY_CACHE[code] = parts[chosen]
            new_suf = chosen[len(code):]
            if cache.get(code) != new_suf:
                cache[code] = new_suf
                dirty = True
    if dirty:
        _save_suffix_cache(cache)
    if os.getenv('FRB_DEBUG_FETCH'):
        print(f"[{_now()}] prefetch: {len(_HISTORY_CACHE)}/{len(codes)} codes have history")


def _slice_period(df, period):
    """Mimic yfinance Ticker.history(period=…) from a cached longer frame.
    'Nd' → last N rows (yfinance '20d' empirically returns 20 trading rows, so
    tail(N) preserves the exact RSI / 量比 window); anything else → full frame."""
    if df is None or getattr(df, 'empty', True):
        return df
    if period.endswith('d'):
        try:
            return df.tail(int(period[:-1]))
        except ValueError:
            return df
    return df


def _quote_from_history(code):
    """Build a get_yfinance_data-shaped quote from cached daily history (no network).
    Used to avoid the 429-throttled urllib chart endpoint for per-stock quotes."""
    h = _HISTORY_CACHE.get(code)
    if h is None or getattr(h, 'empty', True) or 'Close' not in h.columns:
        return None
    closes = h['Close'].dropna()
    if len(closes) < 2:
        return None
    price, prev = float(closes.iloc[-1]), float(closes.iloc[-2])
    opens = h['Open'].dropna() if 'Open' in h.columns else pd.Series(dtype=float)
    today_open = float(opens.iloc[-1]) if len(opens) else prev
    return {
        'name': code, 'price': price, 'prev_close': prev, 'today_open': today_open,
        'change': price - prev, 'intraday_change': price - today_open,
    }


def _cached_get_yfinance(symbol):
    """get_yfinance_data with an in-run cache so a symbol is never fetched twice."""
    if symbol in _QUOTE_CACHE:
        return _QUOTE_CACHE[symbol]
    d = get_yfinance_data(symbol)
    _QUOTE_CACHE[symbol] = d
    return d


def fetch_taiex():
    """TAIEX from yfinance. Returns dict or None."""
    try:
        hist = yf.Ticker('^TWII').history(period='3d')
        if len(hist) < 2:
            return None
        today = hist.iloc[-1]
        prev  = hist.iloc[-2]
        close = float(today['Close'])
        change = close - float(prev['Close'])
        pct    = change / float(prev['Close']) * 100
        return {
            'close':  close,
            'open':   float(today['Open']),
            'change': change,
            'pct':    pct,
        }
    except Exception as e:
        print(f"[{_now()}] TAIEX fetch error: {e}")
        return None


def _prefetch_indices(symbols):
    """One batched yf.download for global indices → _INDEX_CACHE. The urllib chart
    path is 429-throttled on this host; the yfinance batch path is not, so this is
    what actually populates the global-market section."""
    symbols = [s for s in dict.fromkeys(symbols) if s and s not in _INDEX_CACHE]
    if not symbols:
        return
    try:
        df = yf.download(symbols, period='5d', interval='1d', group_by='ticker',
                         auto_adjust=True, threads=True, progress=False)
    except Exception as e:
        if os.getenv('FRB_DEBUG_FETCH'):
            print(f"[{_now()}] index batch download failed: {e}")
        return
    _INDEX_CACHE.update(_split_download(df, symbols))


def fetch_global_indices(indices=None):
    """Returns list of formatted strings — scraped from Yahoo Finance (batched)."""
    if not indices:
        indices = [
            ('S&P 500',    '^GSPC'),
            ('Nasdaq',     '^IXIC'),
            ('費城半導體', '^SOX'),
            ('日經225',    '^N225'),
        ]
    _prefetch_indices([s for _, s in indices])
    lines = []
    for name, sym in indices:
        d = None
        sub = _INDEX_CACHE.get(sym)
        if sub is not None and not sub.empty and 'Close' in sub.columns:
            closes = sub['Close'].dropna()
            if len(closes) >= 2:
                price, prev = float(closes.iloc[-1]), float(closes.iloc[-2])
                d = {'price': price, 'prev_close': prev, 'change': price - prev}
        if d is None:   # last-ditch single call (usually 429 on this host)
            d = _cached_get_yfinance(sym)
        if d and d.get('prev_close'):
            pct = d['change'] / d['prev_close'] * 100
            lines.append(f"• {name}：{d['price']:,.2f} ({pct:+.2f}%)")
        else:
            lines.append(f"• {name}：資料暫時無法取得")
    return lines


def _ticker_history(code, period='20d'):
    """yfinance daily history for a Taiwan code.

    Serves from the batched _HISTORY_CACHE (populated by prefetch_stock_histories)
    when available — this is the common path and avoids per-stock network calls.
    Falls back to an individual fetch that probes the cached/known suffix first
    (then the other), so a .TWO stock no longer wastes a .TW 404."""
    cached = _HISTORY_CACHE.get(code)
    if cached is not None and not cached.empty:
        return _slice_period(cached, period)
    for suffix in _suffix_order(code):
        try:
            hist = yf.Ticker(f"{code}{suffix}").history(period=period)
        except Exception:
            hist = pd.DataFrame()
        if not hist.empty:
            return hist
    return pd.DataFrame()


def fetch_stock_technicals(code, opening_mode=False, period_days=20):
    """RSI-14 and 量比. opening_mode=True skips today's partial volume bar.
    Returns (rsi, vol_ratio) or (None, None)."""
    try:
        hist = _ticker_history(code, period=f'{period_days}d')
        if len(hist) < 15:
            return None, None
        closes = hist['Close']
        delta  = closes.diff()

        # Flat-price edge case: all deltas are zero → RSI ≈ 50
        if delta.abs().sum() == 0:
            rsi = 50.0
        else:
            gain = delta.clip(lower=0).rolling(14).mean()
            loss = (-delta.clip(upper=0)).rolling(14).mean()
            rs   = gain / loss
            rsi  = float((100 - 100 / (1 + rs)).iloc[-1])
            if np.isnan(rsi) or np.isinf(rsi):
                return None, None

        volumes = hist['Volume']
        if opening_mode:
            # At market open, today's volume is near-zero — use yesterday's completed bar
            ref_vol = float(volumes.iloc[-2]) if len(volumes) >= 2 else None
            avg_vol = float(volumes.iloc[:-2].mean()) if len(volumes) >= 3 else None
        else:
            ref_vol = float(volumes.iloc[-1])
            avg_vol = float(volumes.iloc[:-1].mean()) if len(volumes) >= 2 else None

        if avg_vol and avg_vol > 0 and ref_vol is not None:
            vol_ratio = round(ref_vol / avg_vol, 2)
        else:
            vol_ratio = None

        return round(rsi, 1), vol_ratio
    except Exception as e:
        print(f"[{_now()}] Technicals error {code}: {e}")
        return None, None


def fetch_yfinance_stock(code):
    """Price data for a single Taiwan stock.

    Prefers a quote derived from the batched history cache (no network, dodges the
    429-throttled chart endpoint); falls back to the urllib chart API, probing the
    cached/known suffix first (then the other) with an in-run quote cache."""
    q = _quote_from_history(code)
    if q:
        return q
    for suffix in _suffix_order(code):
        d = _cached_get_yfinance(f"{code}{suffix}")
        if d:
            return d
    return None


def fetch_prev_closes(max_lookback=10):
    """{code: 昨收} — official closes for the most recent session STRICTLY BEFORE today.

    The morning report derived 昨收 from the cached daily history frame's
    second-to-last row (_quote_from_history). Yahoo silently OMITS daily bars for
    Taiwan ETFs — 0050/009802/006208/009816/009828/00402A had no 2026-09-10 bar at
    all, the frame jumping 09-09 → 09-11 — so "the row before last" was two sessions
    back. Every ETF was then measured against the wrong day: on 2026-09-11 the push
    said 今日損益 -102,150元 when the real figure was about -88,000元. Ordinary
    stocks were unaffected, which is why the error looked arbitrary.

    Listed board from MI_INDEX, 上櫃 from the TPEX mainboard feed (used only when
    its single published session is the one resolved here). Returns {} if nothing
    resolves, in which case callers keep the Yahoo value.
    """
    today = datetime.datetime.now().date()
    for back in range(1, max_lookback + 1):
        day = today - datetime.timedelta(days=back)
        if day.weekday() >= 5:                      # skip Sat/Sun outright
            continue
        ymd  = day.strftime('%Y%m%d')
        rows = fetch_twse_mi_index(ymd)
        if not rows:                                # holiday or not published
            continue

        out = {}
        for r in rows:
            try:
                out[r['Code']] = float(str(r['ClosingPrice']).replace(',', ''))
            except (ValueError, TypeError):
                continue

        roc = _roc_date(ymd)
        try:
            for s in fetch_tpex_all():
                if s.get('Date') != roc or s.get('Code') in out:
                    continue
                try:
                    out[s['Code']] = float(str(s['ClosingPrice']).replace(',', ''))
                except (ValueError, TypeError):
                    continue
        except Exception as e:
            print(f"[{_now()}] Prev-close TPEX leg failed: {e}")

        print(f"[{_now()}] Prev-session closes: {roc} ({len(out)} codes)")
        return out

    print(f"[{_now()}] Prev-session closes: none resolved — keeping Yahoo 昨收")
    return {}


def _roc_date(yyyymmdd):
    """'20260904' -> '1150904' (ROC year = Gregorian - 1911), matching STOCK_DAY_ALL."""
    try:
        return f"{int(yyyymmdd[:4]) - 1911}{yyyymmdd[4:8]}"
    except (TypeError, ValueError, IndexError):
        return ''


def fetch_twse_mi_index(date_yyyymmdd):
    """Whole-market 每日收盤行情 from MI_INDEX, shaped like a STOCK_DAY_ALL row.

    MI_INDEX (www.twse.com.tw) carries the *current* session within ~1h of the
    13:30 close. The openapi STOCK_DAY_ALL mirror lags a full day — at 16:30 on
    2026-09-04 it was still serving 1150903 — which silently turned every closing
    report into the previous session's prices (and its 今日損益). This is the
    primary source; STOCK_DAY_ALL is now only the fallback.

    Returns [] on any failure (non-trading day, not yet published, network).
    """
    try:
        r = requests.get(
            'https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX',
            params={'date': date_yyyymmdd, 'type': 'ALLBUT0999', 'response': 'json'},
            headers={'User-Agent': 'Mozilla/5.0'}, timeout=20, verify=False,
        )
        payload = r.json()
    except Exception as e:
        print(f"[{_now()}] MI_INDEX fetch failed: {e}")
        return []

    if str(payload.get('stat', '')).upper() != 'OK':
        print(f"[{_now()}] MI_INDEX {date_yyyymmdd}: {payload.get('stat')}")
        return []

    # The per-stock table is the one whose fields start with 證券代號; its index
    # shifts as TWSE adds/removes index tables, so find it rather than hard-code 8.
    table = next((t for t in payload.get('tables', [])
                  if (t.get('fields') or [None])[0] == '證券代號'), None)
    if not table:
        print(f"[{_now()}] MI_INDEX {date_yyyymmdd}: 每日收盤行情 table absent")
        return []

    roc = _roc_date(str(payload.get('date') or date_yyyymmdd))

    def _num(v):
        return str(v or '').replace(',', '').strip()

    out = []
    for row in table.get('data', []):
        try:
            close = _num(row[8])
            if not close or close == '--':      # no trades today — nothing to report
                continue
            # 漲跌(+/-) arrives as an HTML span: <p style= color:red>+</p>.
            sign = re.sub(r'<[^>]*>', '', str(row[9])).strip()
            mag  = float(_num(row[10]) or '0')
            change = -mag if sign == '-' else mag
            out.append({
                'Date':         roc,
                'Code':         str(row[0]).strip(),
                'Name':         str(row[1]).strip(),
                'TradeVolume':  _num(row[2]),
                'Transaction':  _num(row[3]),
                'TradeValue':   _num(row[4]),
                'OpeningPrice': _num(row[5]),
                'HighestPrice': _num(row[6]),
                'LowestPrice':  _num(row[7]),
                'ClosingPrice': close,
                'Change':       f"{change:.4f}",
            })
        except (IndexError, ValueError, TypeError):
            continue
    return out


def fetch_twse_all():
    """Full TWSE scan. Returns list of stock dicts or None.

    MI_INDEX first (has today's close); STOCK_DAY_ALL only as fallback because
    that mirror can still be serving the previous session hours after the close.
    """
    today = datetime.datetime.now().strftime('%Y%m%d')
    rows = fetch_twse_mi_index(today)
    if rows:
        print(f"[{_now()}] TWSE data date: {rows[0]['Date']} ({len(rows)} stocks) [MI_INDEX]")
        return rows

    try:
        result = TWSDDataSource().fetch_data()
        data   = result['data']
        date   = data[0].get('Date', '') if data else ''
        print(f"[{_now()}] TWSE data date: {date} ({len(data)} stocks) [STOCK_DAY_ALL fallback]")
        return data
    except Exception as e:
        print(f"[{_now()}] TWSE fetch failed: {e}")
        return None


def _get_json_retry(url, attempts=4, timeout=30, **kw):
    """GET → parsed JSON, retrying transient failures with a short backoff.

    The TPEX openapi host drops a connection mid-body ("Response ended
    prematurely") on roughly 1 call in 4. A single-shot fetch made every 上櫃
    holding silently degrade to a Yahoo quote tagged 「yfinance 報價」 with
    成交量 0, on a random subset of days. Raises the last error if all fail.
    """
    last = None
    for i in range(attempts):
        try:
            r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'},
                             timeout=timeout, **kw)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            if i < attempts - 1:
                print(f"[{_now()}] retry {i + 1}/{attempts - 1} ({url.rsplit('/', 1)[-1]}): {e}")
                time.sleep(1.5 * (i + 1))
    raise last


def fetch_tpex_all():
    """TPEX (上櫃) daily data, normalized to match TWSE field names."""
    try:
        raw = _get_json_retry(
            'https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes')
        normalized = [{
            'Code':         s.get('SecuritiesCompanyCode', ''),
            'Name':         s.get('CompanyName', ''),
            'Date':         s.get('Date', ''),
            'OpeningPrice': s.get('Open', '0'),
            'ClosingPrice': s.get('Close', '0'),
            'Change':       s.get('Change', '0').strip(),
            'TradeVolume':  s.get('TradingShares', '0'),
        } for s in raw]
        date = normalized[0]['Date'] if normalized else ''
        print(f"[{_now()}] TPEX data date: {date} ({len(normalized)} stocks)")
        return normalized
    except Exception as e:
        print(f"[{_now()}] TPEX fetch failed: {e}")
        return []


def fetch_tpex_emerging():
    """TPEX 興櫃 (emerging board) latest stats → {code: TWSE-row-shaped dict}.

    Covers codes absent from both STOCK_DAY_ALL (listed) and the TPEX mainboard feed
    — e.g. 3595 山太士, 7924 台灣微脂體 — which otherwise render 今日無交易數據. The
    emerging board trades on a 加權平均價 (no opening auction), so 開盤/收盤 both use
    Average and the day change is Average vs PreviousAveragePrice. Rows carry
    _fallback='tpex_esb' so the line is tagged 興櫃均價 (official TPEX data, not Yahoo).
    Best-effort → {}."""
    def _f(v):
        try:
            return float(str(v).replace(',', '').strip())
        except (TypeError, ValueError):
            return None
    out = {}
    try:
        for s in _get_json_retry(
                'https://www.tpex.org.tw/openapi/v1/tpex_esb_latest_statistics'):
            code = (s.get('SecuritiesCompanyCode') or '').strip()
            avg  = _f(s.get('Average'))
            if not code or avg is None or avg <= 0:
                continue
            prev   = _f(s.get('PreviousAveragePrice'))
            prevc  = prev if (prev is not None and prev > 0) else avg
            change = avg - prevc
            out[code] = {
                'Code': code, 'Name': (s.get('CompanyName') or '').strip(),
                'Date': (s.get('Date') or '').strip(),
                'OpeningPrice': f"{avg:.2f}", 'ClosingPrice': f"{avg:.2f}",
                'TradeVolume': str(int(_f(s.get('TransactionVolume')) or 0)),
                '_change': change, '_close': avg,
                '_pct': (change / prevc * 100) if prevc else 0.0,
                '_vol': int(_f(s.get('TransactionVolume')) or 0),
                '_fallback': 'tpex_esb',
            }
        print(f"[{_now()}] TPEX emerging (興櫃): {len(out)} stocks")
    except Exception as e:
        print(f"[{_now()}] TPEX emerging fetch failed: {e}")
    return out


def fetch_valuation():
    """{code: {'pe','yield','pb'}} valuation from TWSE BWIBBU_ALL. Best-effort → {}."""
    def _f(v):
        try:
            return float(str(v).replace(',', '').strip())
        except (TypeError, ValueError):
            return None
    out = {}
    try:
        r = requests.get('https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL',
                         headers={'User-Agent': 'Mozilla/5.0'}, timeout=15, verify=False)
        for s in r.json():
            code = (s.get('Code') or '').strip()
            if code:
                out[code] = {'pe': _f(s.get('PEratio')), 'yield': _f(s.get('DividendYield')),
                             'pb': _f(s.get('PBratio'))}
        print(f"[{_now()}] Valuation (P/E, 殖利率, P/B): {len(out)} stocks")
    except Exception as e:
        print(f"[{_now()}] Valuation fetch failed: {e}")
    return out


def fetch_margin():
    """{code: {'bal','chg'}} 融資今日餘額(張) + day-change from TWSE MI_MARGN. Best-effort → {}."""
    def _i(v):
        try:
            return int(str(v).replace(',', '').strip())
        except (TypeError, ValueError):
            return None
    out = {}
    try:
        r = requests.get('https://openapi.twse.com.tw/v1/exchangeReport/MI_MARGN',
                         headers={'User-Agent': 'Mozilla/5.0'}, timeout=20, verify=False)
        for s in r.json():
            code = (s.get('股票代號') or '').strip()
            if not code:
                continue
            bal, prev = _i(s.get('融資今日餘額')), _i(s.get('融資前日餘額'))
            chg = (bal - prev) if (bal is not None and prev is not None) else None
            out[code] = {'bal': bal, 'chg': chg}
        print(f"[{_now()}] Margin (融資餘額): {len(out)} stocks")
    except Exception as e:
        print(f"[{_now()}] Margin fetch failed: {e}")
    return out


def _attach_signals(contexts, valuation, margin):
    """Attach fundamental signals (valuation + margin) onto each per-stock context in place."""
    for c in contexts:
        v = valuation.get(c['code']) or {}
        m = margin.get(c['code']) or {}
        c['val'] = {'pe': v.get('pe'), 'yield': v.get('yield'), 'pb': v.get('pb'),
                    'margin_chg': m.get('chg')}


def parse_twse_valid(all_stocks):
    """Attach computed floats to each TWSE stock dict. Returns list of enhanced dicts."""
    valid = []
    for s in all_stocks:
        try:
            change = float(s.get('Change', '0').replace(',', '').strip() or '0')
            close  = float(s.get('ClosingPrice', '0').replace(',', '').strip() or '0')
            vol    = int(s.get('TradeVolume', '0').replace(',', '').strip() or '0')
            prev   = close - change
            pct    = change / prev * 100 if prev else 0.0
            s = dict(s)
            s['_change'] = change
            s['_close']  = close
            s['_vol']    = vol
            s['_pct']    = pct
            valid.append(s)
        except (ValueError, ZeroDivisionError):
            continue
    return valid


def fetch_period_returns(code):
    """1M / 3M / 1Y price returns (%) for a watchlist stock, from a single yfinance
    history call (reuses _ticker_history → .TW then .TWO). Returns
    {'1月': pct|None, '3月': pct|None, '1年': pct|None}; a window is None when the stock
    lacks enough history (newly listed / 興櫃). Each window aligns to the nearest trading
    day on or before the cutoff, so holidays/weekends don't skew it."""
    try:
        hist = _ticker_history(code, period='1y')
        if hist is None or hist.empty:
            return {}
        closes = hist['Close'].dropna()
        if len(closes) < 2:
            return {}
        cur       = float(closes.iloc[-1])
        last_date = closes.index[-1]
        out = {}
        for label, days in (('1月', 30), ('3月', 90), ('1年', 365)):
            prior = closes[closes.index <= last_date - pd.Timedelta(days=days)]
            if len(prior) == 0 or not cur:
                out[label] = None
            else:
                ref = float(prior.iloc[-1])
                out[label] = ((cur - ref) / ref * 100) if ref else None
        return out
    except Exception as e:
        print(f"[{_now()}] Period-returns error {code}: {e}")
        return {}


def _twse_row_from_yfinance(code):
    """Closing-report fallback for codes missing from the bulk TWSE/TPEX feeds.

    The closing report's twse_by_code comes from fetch_twse_all (STOCK_DAY_ALL,
    listed board) + fetch_tpex_all (TPEX 上櫃). Stocks not on either — e.g.
    emerging-board 興櫃 codes like 3595 山太士 — would otherwise render as
    "今日無交易數據". This builds a TWSE-row-shaped dict from the same per-code
    yfinance source the morning report uses (.TW then .TWO), so the existing
    closing render works unchanged. Returns None if yfinance also has no data.
    """
    d = fetch_yfinance_stock(code)
    if not d:
        return None
    price  = d['price']
    prev   = d['prev_close']
    change = d.get('change', price - prev)
    pct    = (change / prev * 100) if prev else 0.0
    return {
        'Code': code,
        'OpeningPrice': f"{d.get('today_open', prev):.2f}",
        'ClosingPrice': f"{price:.2f}",
        'TradeVolume': '0',        # yfinance chart feed carries no share volume
        '_change': change,
        '_close': price,
        '_pct': pct,
        '_vol': 0,
        '_fallback': 'yfinance',   # source marker (yfinance, not TWSE official)
    }


def fetch_brave_news(query='台股 今日 財經', count=5):
    """Fetch latest financial news headlines via Brave Search API."""
    api_key = os.getenv('BRAVE_API_KEY')
    if not api_key:
        print(f"[{_now()}] Brave news skipped: BRAVE_API_KEY not set.")
        return []
    try:
        resp = requests.get(
            'https://api.search.brave.com/res/v1/web/search',
            headers={
                'X-Subscription-Token': api_key,
                'Accept': 'application/json',
            },
            params={'q': query, 'count': count, 'search_lang': 'zh-hant', 'freshness': 'pd'},
            timeout=10,
        )
        if resp.ok:
            results = resp.json().get('web', {}).get('results', [])
            return [r.get('title', '') for r in results if r.get('title')]
    except Exception as e:
        print(f"[{_now()}] Brave Search error: {e}")
    return []


# ---------------------------------------------------------------------------
# The snapshot — one call, everything a report needs
# ---------------------------------------------------------------------------

def _quote_from_official(row):
    """Official TWSE/TPEX close row → the same quote shape the live board uses,
    so BOTH modes feed one calculation (compute_positions) instead of two."""
    close, change = row.get('_close'), row.get('_change')
    if close is None:
        return None
    prev = close - (change or 0.0)
    try:
        open_p = float(str(row.get('OpeningPrice', '')).replace(',', ''))
    except (TypeError, ValueError):
        open_p = prev
    return {'price': close, 'prev_close': prev, 'today_open': open_p,
            'change': change or 0.0, 'intraday': True}


def _row_from_quote(code, q):
    """Live quote → a TWSE-row-shaped dict, for a closing report run before the
    exchange has published. Built from the quote (which carries an authoritative
    previousClose) rather than from a daily history frame, whose ETF holes are
    exactly what corrupted 昨收 on 2026-09-11."""
    price, prev = q['price'], q['prev_close']
    change = price - prev if prev else 0.0
    return {
        'Code': code,
        'OpeningPrice': f"{q.get('today_open', prev):.2f}",
        'ClosingPrice': f"{price:.2f}",
        'TradeVolume': '0',                 # a quote carries no official 成交量
        '_change': change, '_close': price, '_vol': 0,
        '_pct': (change / prev * 100) if prev else 0.0,
        '_fallback': 'quote',
    }


def _official_board():
    """Whole-market official close: {code: row}, the hotlist, and whether the
    feed is actually today's. Listed (MI_INDEX, else the lagging STOCK_DAY_ALL)
    + 上櫃 + 興櫃."""
    all_stocks = fetch_twse_all()
    if not all_stocks:
        return None

    feed_date = all_stocks[0].get('Date', '')
    today_roc = _roc_date(datetime.datetime.now().strftime('%Y%m%d'))
    is_stale  = feed_date != today_roc

    valid = parse_twse_valid(all_stocks)
    by_code = {s['Code']: s for s in valid}
    hotlist = {'top_volume': sorted(valid, key=lambda x: x['_vol'], reverse=True)[:5],
               'top_losers': sorted(valid, key=lambda x: x['_pct'])[:5]}

    for s in parse_twse_valid(fetch_tpex_all() or []):
        by_code.setdefault(s['Code'], s)
    for code, row in (fetch_tpex_emerging() or {}).items():
        by_code.setdefault(code, row)

    return {'by_code': by_code, 'hotlist': hotlist,
            'is_stale': is_stale, 'feed_date': feed_date}


def _context(code, name, row, quote, period_days, opening_mode, pos=None):
    """One stock's complete numbers. Every field any report line prints."""
    rsi, vol_ratio = fetch_stock_technicals(code, opening_mode=opening_mode,
                                            period_days=period_days)
    price = quote['price']
    prev  = quote['prev_close']
    change = price - prev if prev else 0.0
    return {
        'code': code, 'name': name, 'pos': pos, 'row': row,
        'price': price, 'prev_cls': prev,
        'change': change, 'pct': (change / prev * 100) if prev else 0.0,
        'open_p': (row or {}).get('OpeningPrice', 'N/A'),
        'close_p': (row or {}).get('ClosingPrice', 'N/A'),
        'zhang': format_zhang((row or {}).get('TradeVolume', '0')) if row else 'N/A',
        'rsi': rsi, 'vol_ratio': vol_ratio,
    }


def format_zhang(volume):
    """Raw share volume (int or comma-string) → 張 string."""
    try:
        return f"{int(str(volume).replace(',', '')) // 1000:,}"
    except (TypeError, ValueError):
        return str(volume)


def snapshot(mode, cfg=None):
    """EVERYTHING the twice-daily report prints, fetched once and derived once.

    The reports call this and render the result. They do not fetch and they do
    not calculate — that is the whole point of this module. Both modes end up in
    the same compute_positions(), so the morning push, the afternoon push and
    the dashboard's own Total row cannot disagree about your P&L.

    Returns None only when the closing feed is unavailable (caller aborts).
    """
    cfg = cfg if cfg is not None else load_bot_config()
    period_days = cfg.get('technicals', {}).get('period_days', 20)
    opening_mode = (mode == 'morning')

    portfolio     = load_portfolio()
    tracked       = load_tracked_stocks()
    tracked_notes = load_tracked_notes()
    all_codes = list(portfolio) + [c for c in tracked if c not in portfolio]

    print(f"[{_now()}] [{mode.upper()}] TAIEX + global indices...")
    taiex = fetch_taiex()
    global_lines = fetch_global_indices(cfg.get('global_indices'))

    print(f"[{_now()}] [{mode.upper()}] Prefetching histories ({len(all_codes)} codes)...")
    prefetch_stock_histories(all_codes)

    official, hotlist = None, {'top_volume': [], 'top_losers': []}
    is_stale, feed_date = False, ''
    if mode == 'closing':
        print(f"[{_now()}] [CLOSING] Official whole-market close...")
        official = _official_board()
        if official is None:
            print(f"[{_now()}] TWSE data unavailable — aborting closing report.")
            return None
        hotlist, is_stale, feed_date = (official['hotlist'], official['is_stale'],
                                        official['feed_date'])
        if is_stale:
            # Never price a holding off a previous session: 今日損益 would be that
            # day's P&L reported as today's. The hotlist is whole-market and has
            # no live equivalent, so it alone stays on the older feed (and says so).
            print(f"[{_now()}] [CLOSING] feed is {feed_date}, not today — "
                  f"per-stock lines use live quotes")

    # --- quotes: one source per mode, one shape either way ---
    use_live = (mode == 'morning') or is_stale
    if use_live:
        print(f"[{_now()}] [{mode.upper()}] Live quotes ({len(all_codes)} codes)...")
        quotes = fetch_live_quotes(all_codes, resolve_symbols(all_codes, DATA_DIR))
        rows = {c: _row_from_quote(c, q) for c, q in quotes.items()} if mode == 'closing' else {}
    else:
        by_code = official['by_code']
        rows   = {c: by_code[c] for c in all_codes if c in by_code}
        quotes = {c: q for c, q in ((c, _quote_from_official(r)) for c, r in rows.items()) if q}
        # Anything the official feeds don't carry (興櫃 gaps) still needs a price.
        missing = [c for c in all_codes if c not in quotes]
        if missing:
            print(f"[{_now()}] [CLOSING] {len(missing)} code(s) absent from official feeds "
                  f"— live quotes for those")
            for c, q in fetch_live_quotes(missing, resolve_symbols(missing, DATA_DIR)).items():
                quotes[c] = q
                rows[c] = _row_from_quote(c, q)

    # --- THE calculation, for every mode ---
    positions = compute_positions(portfolio, quotes)

    # --- cross-check 昨收 against the exchange; never alters a printed number ---
    prev_official = fetch_prev_closes() if mode == 'morning' else {}
    prev_mismatch = sorted(
        r['code'] for r in positions['rows']
        if r['prev_close'] and prev_official.get(r['code'])
        and abs(r['prev_close'] - prev_official[r['code']]) > 0.005
    )
    if prev_mismatch:
        print(f"[{_now()}] ⚠ 昨收 disagrees with the exchange for: {', '.join(prev_mismatch)}")

    print(f"[{_now()}] [{mode.upper()}] Fundamentals + per-stock technicals...")
    valuation, margin = fetch_valuation(), fetch_margin()

    holdings, missing_holdings = [], []
    for code, pos in portfolio.items():
        q = quotes.get(code)
        if not q:
            missing_holdings.append((code, pos.get('name', code)))
            continue
        c = _context(code, pos.get('name', code), rows.get(code), q,
                     period_days, opening_mode, pos=pos)
        r = next((x for x in positions['rows'] if x['code'] == code), None)
        c['daily_pnl'] = r['daily_pnl'] if r else None
        c['stale'] = r['stale'] if r else False
        holdings.append(c)

    watchlist, missing_watch = [], []
    for code, name in tracked.items():
        if code in portfolio:
            continue
        q = quotes.get(code)
        if not q:
            missing_watch.append((code, name))
            continue
        c = _context(code, name, rows.get(code), q, period_days, opening_mode)
        c['rets'] = fetch_period_returns(code)
        c['note'] = tracked_notes.get(code, '')
        watchlist.append(c)

    _attach_signals(holdings + watchlist, valuation, margin)

    news = []
    if mode == 'closing':
        news_cfg = cfg.get('news', {})
        print(f"[{_now()}] [CLOSING] News headlines...")
        news = fetch_brave_news(query=news_cfg.get('query_closing', '台股 今日 財經 股市'),
                                count=news_cfg.get('count', 5))

    now = datetime.datetime.now()
    return {
        'mode': mode, 'cfg': cfg, 'period_days': period_days,
        'date_str': now.strftime('%Y-%m-%d'), 'time_str': now.strftime('%H:%M'),
        'portfolio': portfolio, 'tracked': tracked, 'tracked_notes': tracked_notes,
        'taiex': taiex, 'taiex_pct': (taiex['pct'] if taiex else 0.0),
        'global_lines': global_lines,
        'quotes': quotes, 'positions': positions,
        'holdings': holdings, 'watchlist': watchlist,
        'missing_holdings': missing_holdings, 'missing_watch': missing_watch,
        'hotlist': hotlist, 'news': news,
        'is_stale': is_stale, 'feed_date': feed_date,
        'prev_mismatch': prev_mismatch,
    }
