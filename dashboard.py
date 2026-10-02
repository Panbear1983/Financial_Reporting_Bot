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
import subprocess
import urllib.parse
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


# ---------------------------------------------------------------------------
# The exchange's OWN real-time quotes (mis.twse.com.tw)
#
# Yahoo is on a hard ~20-minute delay for TWSE. Measured 2026-09-30: at 09:15
# every Yahoo quote was still stamped with the previous session's 13:30 close and
# today's 1-minute bars did not exist; the feed only rolled over at 09:20:37. So a
# 09:05 push cannot be built on it. The exchange publishes its own feed with no
# delay, and this is it. Full field semantics, every one of them measured from
# this Mac, are in docs/MORNING_REBUILD_2026-10-02.md — read that before touching
# this block, because three of the fields do not mean what their names suggest.
# ---------------------------------------------------------------------------

_MIS_URL      = 'https://mis.twse.com.tw/stock/api/getStockInfo.jsp'
_MIS_INDEX_CH = 'tse_t00.tw'            # 發行量加權股價指數
_MIS_INDEX_C  = 't00'                   # ...which comes back keyed 't00', NOT '_t00'
_MIS_HEADERS  = {
    # Both headers are required; without the Referer the service refuses.
    'User-Agent': ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
                   '(KHTML, like Gecko) Chrome/120.0 Safari/537.36'),
    'Referer': 'https://mis.twse.com.tw/stock/index.jsp',
}
_MIS_PREFIX_FILE = 'mis_prefix_cache.json'
# 3595 answers on NEITHER tse_ nor otc_ (measured 2026-10-02 — both channels came
# back as the junk row). Seeded so it never costs a probe and never counts as a
# fallback that would fire the delayed-prices warning on every single run.
_MIS_PREFIX_SEED = {'3595': 'none'}


def _f(v):
    """MIS numeric field -> float, or None. '-' is its no-trade-yet marker.

    Every numeric field goes through this. Before a stock's first match of the day
    MIS serves '-' in z, o AND the bid ladder, and bare float() on any of them
    raises ValueError — at 09:05, which is the only time this code runs.
    """
    if v is None:
        return None
    s = str(v).strip()
    if s in ('', '-'):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _mis_prefix_path():
    return os.path.join(DATA_DIR, _MIS_PREFIX_FILE)


def _mis_prefixes():
    """{code: 'tse'|'otc'|'none'} — which channel each code answers on."""
    try:
        with open(_mis_prefix_path(), 'r', encoding='utf-8') as f:
            cached = json.load(f)
    except (OSError, ValueError):
        cached = {}
    merged = dict(_MIS_PREFIX_SEED)
    if isinstance(cached, dict):
        merged.update(cached)           # a later successful probe may override a seed
    return merged


def _save_mis_prefixes(prefixes):
    path = _mis_prefix_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(prefixes, f, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(tmp, path)           # atomic — the board reads this while we write
    except OSError:
        pass


def _mis_get(ex_ch, timeout=20):
    """Raw MIS payload for one pipe-joined ex_ch string, or None.

    requests first, curl as the retry: Python's TLS has failed certificate
    verification on this Mac before, and curl is the proven path.
    """
    params = {'ex_ch': ex_ch, 'json': '1', 'delay': '0'}
    try:
        r = requests.get(_MIS_URL, params=params, headers=_MIS_HEADERS, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception:                                                  # noqa: BLE001
        pass
    try:
        url = _MIS_URL + '?' + urllib.parse.urlencode(params)
        done = subprocess.run(
            ['curl', '-sS', '-m', str(timeout), '-A', _MIS_HEADERS['User-Agent'],
             '-H', 'Referer: ' + _MIS_HEADERS['Referer'], url],
            capture_output=True, text=True, timeout=timeout + 5)
        # A malformed ex_ch answers with ~20 bytes of newlines and no JSON at all.
        return json.loads(done.stdout)
    except Exception:                                                  # noqa: BLE001
        return None


def _mis_row(m):
    """One msgArray entry -> a quote dict in fetch_live_quotes' shape, or None.

    Returns None when the row carries no usable price, and the CALLER must then
    leave that code out of its result entirely so it falls through to Yahoo.
    Emitting a quote with price=None instead crashes compute_positions, which
    guards only `if not q` — a dict with a None price is truthy and reaches
    `price - prev`.
    """
    code = (m.get('c') or '').strip()
    if not code:
        return None                     # the junk row an unservable channel returns
    price = _f(m.get('z'))
    if price is None:                                 # between matches / before the first
        price = _f((m.get('b') or '').split('_')[0])  # top of the bid ladder
    if price is None:
        price = _f(m.get('o'))
    prev = _f(m.get('y'))
    if price is None or prev is None:
        return None
    op = _f(m.get('o'))
    # The price time is d + t. NOT tlong: for stocks tlong is the 14:30 盤後定價
    # stamp (measured — 2330 returns t=13:30:00, ot=14:30:00, tlong -> 14:30:00),
    # so building the "honest timestamp" on it would overstate freshness by an hour.
    day, clock = m.get('d'), m.get('t')
    quote_at = None
    if day and clock:
        try:
            quote_at = (datetime.datetime.strptime(day + clock, '%Y%m%d%H:%M:%S')
                        .replace(tzinfo=ZoneInfo('Asia/Taipei')).astimezone())
        except ValueError:
            quote_at = None
    today = datetime.datetime.now(ZoneInfo('Asia/Taipei')).strftime('%Y%m%d')
    return {
        'name': m.get('n') or code,
        'price': price,
        'prev_close': prev,
        'today_open': op if op is not None else prev,
        'exch_open': op,                # None until the stock's first match
        'change': price - prev,
        'intraday_change': (price - op) if op is not None else 0.0,
        'quote_at': quote_at,
        'intraday': bool(day == today and clock and clock <= '13:35:00'),
        'src': 'mis',
        'session_date': day,
    }


def fetch_mis_quotes(codes, want_index=False, timeout=20, chunk=40, unmatched=None):
    """{code: quote} from the exchange's real-time feed, plus '_t00' for the index.

    A code is absent from the result when the exchange has no usable price for it —
    unservable, or not yet matched today. Pass a list as `unmatched` to learn
    which: codes the exchange ANSWERED for but could not price are appended to
    it. Those must NOT be filled from Yahoo at 09:05 — Yahoo has not rolled over
    by then, so it would put yesterday's move into today's figures. Only codes
    the exchange did not answer for at all fall through to Yahoo (as delayed).
    """
    codes = [str(c) for c in codes]
    prefixes = _mis_prefixes()
    out, learned = {}, dict(prefixes)

    def ask(channels):
        """Pipe-joined channels -> {bare code: row}. 19 symbols measured at 1.33s."""
        rows = {}
        for i in range(0, len(channels), chunk):
            payload = _mis_get('|'.join(channels[i:i + chunk]), timeout=timeout)
            if not isinstance(payload, dict) or payload.get('rtcode') != '0000':
                continue                # rtcode 9999 = 參數不足; None = transport/JSON failure
            for m in payload.get('msgArray') or []:
                c = (m.get('c') or '').strip()
                if c:
                    rows[c] = m
        return rows

    # Pass 1 — known prefixes as known, unknown codes tried on tse_ (the common case).
    first, pending = [], []
    if want_index:
        first.append(_MIS_INDEX_CH)
    for c in codes:
        p = prefixes.get(c)
        if p == 'none':
            continue                    # measured unservable; don't waste a channel
        first.append(f'{p if p in ("tse", "otc") else "tse"}_{c}.tw')
        if p not in ('tse', 'otc'):
            pending.append(c)
    rows = ask(first) if first else {}

    # Pass 2 — anything unknown that tse_ did not answer gets one otc_ attempt.
    retry = [c for c in pending if c not in rows]
    if retry:
        rows.update(ask([f'otc_{c}.tw' for c in retry]))

    for c in pending:
        learned[c] = 'tse' if c in rows and f'tse_{c}.tw' in first else (
            'otc' if c in rows else 'none')
    if learned != prefixes:
        _save_mis_prefixes(learned)

    # The index is special-cased BEFORE the generic keying, so '_t00' is never
    # mistaken for a holding code and the caller's pop() always finds it.
    if want_index and _MIS_INDEX_C in rows:
        idx = _mis_row(rows.pop(_MIS_INDEX_C))
        if idx:
            out['_t00'] = idx
    for c, m in rows.items():
        q = _mis_row(m)
        if q:
            out[c] = q
        elif unmatched is not None:
            unmatched.append(c)
    return out


def exchange_is_quoting(timeout=10):
    """True / False / None — is the exchange publishing TODAY's session right now?

    None means UNKNOWN (probe failed, or the payload was not well formed) and a
    caller must never read it as a closure. Only an index row carrying a parseable
    session date strictly earlier than today answers False — that is the 颱風假
    case, announced the night before and never present in TWSE's published
    holiday calendar.

    Neither rtcode nor queryTime can answer this: measured 2026-10-02 at 17:32,
    with the market long shut, rtcode was still '0000' and queryTime read
    '20261002 17:32:50' — the server's wall clock. Only d/t carry the session.

    Deliberately NOT reachable from taiwan_market_open() / is_trading_day():
    taiwan_market_open() sits on the live board's render path at ~4 calls/second,
    and one network round trip there freezes the screen.
    """
    payload = _mis_get(_MIS_INDEX_CH, timeout=timeout)
    if not isinstance(payload, dict) or payload.get('rtcode') != '0000':
        return None
    row = next((m for m in (payload.get('msgArray') or [])
                if (m.get('c') or '').strip() == _MIS_INDEX_C), None)
    if row is None:
        return None
    try:
        day = datetime.datetime.strptime(row.get('d'), '%Y%m%d').date()
    except (TypeError, ValueError):
        return None
    today = datetime.datetime.now(ZoneInfo('Asia/Taipei')).date()
    if day == today:
        return True
    return False if day < today else None


def morning_gate(retries=2, wait=60):
    """Should the 09:05 push go out? -> {'skip': bool, 'quoting': True|False|None, 'reason': str}

    The published holiday calendar is checked first, by the scheduler. This
    catches what the calendar never learns about: a 颱風假 announced the night
    before. It skips ONLY on a definite False from exchange_is_quoting(), asked
    again `retries` times `wait` seconds apart so a slow first index print at
    09:00 can never be mistaken for a closure. None (unknown) never skips — a
    failed probe is not a closed market.

    Not applied outside a calendar trading day after 09:00 Taipei: before the
    open the index still carries yesterday's date by design, and on a weekend
    the scheduler has already stopped the run.
    """
    now = datetime.datetime.now(ZoneInfo('Asia/Taipei'))
    if not is_trading_day(now.date()) or now.time() < datetime.time(9, 0):
        return {'skip': False, 'quoting': None, 'reason': ''}
    q = exchange_is_quoting()
    for _ in range(retries):
        if q is not False:
            break
        print(f"[{_now()}] [MORNING] exchange index still on a previous session — "
              f"asking again in {wait}s", flush=True)
        time.sleep(wait)
        q = exchange_is_quoting()
    if q is False:
        return {'skip': True, 'quoting': False,
                'reason': ('證交所今日沒有開盤報價（可能為颱風假或臨時休市），'
                           '今日不發送早盤報告。')}
    return {'skip': False, 'quoting': q, 'reason': ''}


_HOLIDAY_URL   = 'https://www.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule'
_HOLIDAY_CACHE  = {}         # ROC year (int) -> {'days': {date: name}, 'fetched': 'YYYY-MM-DD'}
_HOLIDAY_TRY_AT = {}         # ROC year (int) -> datetime of the last network attempt
_HOLIDAY_MAX_AGE_DAYS = 30   # re-ask this often, so mid-year amendments land
_HOLIDAY_RETRY_SECS   = 3600 # ...but at most this often when the ask is failing


def _holiday_cache_path():
    return os.path.join(DATA_DIR, 'market_holidays.json')


def _read_holiday_cache():
    """The on-disk calendar as {'115': {'fetched': 'YYYY-MM-DD', 'days': {...}}}.

    Migrates the first format, which stored the day map directly under the year
    with no fetch date: those are treated as age-unknown, hence stale, so the
    first lookup after an upgrade re-asks once and stamps them.
    """
    try:
        with open(_holiday_cache_path(), encoding='utf-8') as f:
            raw = json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        return {}
    out = {}
    for key, val in raw.items():
        if not key.isdigit() or not isinstance(val, dict):
            continue                                   # 'fetched_at' and friends
        if 'days' in val:
            out[key] = val
        else:                                          # legacy: bare day map
            out[key] = {'fetched': None, 'days': val}
    return out


def _stale(fetched):
    if not fetched:
        return True
    try:
        age = datetime.date.today() - datetime.date.fromisoformat(fetched)
    except (TypeError, ValueError):
        return True
    return age.days >= _HOLIDAY_MAX_AGE_DAYS or age.days < 0


def fetch_market_holidays(roc_year, refresh=False):
    """{'YYYY-MM-DD': name} of days the exchange is CLOSED, from TWSE's own
    published holiday schedule (開休市日期), cached to disk.

    A weekday can still be a non-trading day — 中秋節, 國慶日, Lunar New Year.
    Nothing in this project knew that: every job gated on Mon–Fri alone and so
    published a full report on a closed market, quoting the previous session's
    prices as if they were today's.

    The cached copy is re-asked every _HOLIDAY_MAX_AGE_DAYS, so a year that was
    unpublished when we first looked (next year's, before TWSE issues it) is
    picked up without anyone intervening, and a mid-year amendment lands too.
    A failed re-ask keeps serving the copy we already have.

    Returns {} if the calendar has never been fetched and the network fails —
    callers must treat that as "unknown", not as "no holidays", so a failed
    lookup can never silently take the whole schedule offline.
    """
    roc_year = int(roc_year)
    # 1. A still-fresh answer already in this process settles it without touching
    #    the disk — taiwan_market_open() runs off this and the live board calls it
    #    several times a second.
    mem = _HOLIDAY_CACHE.get(roc_year)
    if mem and not refresh and not _stale(mem.get('fetched')):
        return mem['days']

    path = _holiday_cache_path()
    disk = _read_holiday_cache()
    entry = disk.get(str(roc_year)) or {}
    cached = entry.get('days')
    if cached and not refresh and not _stale(entry.get('fetched')):
        _HOLIDAY_CACHE[roc_year] = entry
        return cached

    # 2. We have to ask. Throttle the attempt so a stale copy plus an unreachable
    #    exchange can't turn every open/closed check into a 20-second timeout —
    #    that would freeze the live board rather than merely leave it out of date.
    last = _HOLIDAY_TRY_AT.get(roc_year)
    if not refresh and last and (datetime.datetime.now() - last).total_seconds() < _HOLIDAY_RETRY_SECS:
        return cached or {}
    _HOLIDAY_TRY_AT[roc_year] = datetime.datetime.now()

    try:
        resp = requests.get(_HOLIDAY_URL, params={'response': 'json', 'queryYear': str(roc_year)},
                            timeout=20, verify=False)
        rows = resp.json().get('data') or []
        # ['2026-09-25', '中秋節', '依規定放假1日。'] — the exchange publishes the
        # Gregorian date in column 0 even though the query is by ROC year.
        #
        # Keep only dates that really fall in the year asked for. Querying a year
        # TWSE has not published yet does NOT return empty — it silently serves the
        # CURRENT year's calendar, so an unguarded fetch of next year would cache
        # this year's holidays under next year's key and mis-gate every job for the
        # following twelve months.
        gregorian = roc_year + 1911
        found = {r[0].strip(): (r[1].strip() if len(r) > 1 else '')
                 for r in rows
                 if r and re.match(r'^\d{4}-\d{2}-\d{2}$', str(r[0]).strip())
                 and str(r[0]).strip().startswith(f'{gregorian}-')}
    except Exception as exc:                                       # noqa: BLE001
        # Couldn't ask. Whatever we already knew is still the best answer.
        print(f"[{_now()}] Holiday calendar fetch failed ({roc_year}): "
              f"{type(exc).__name__}: {exc}")
        return cached or {}
    if not found:
        # We DID ask and the exchange has nothing for this year — it isn't
        # published yet. Anything cached under this year came from the silent
        # current-year substitution above, so it is provably wrong: drop it
        # rather than keep gating on another year's holidays.
        if cached:
            disk.pop(str(roc_year), None)
            _HOLIDAY_CACHE.pop(roc_year, None)
            try:
                with open(path, 'w', encoding='utf-8') as f:
                    json.dump(disk, f, ensure_ascii=False, indent=2)
            except OSError:
                pass
            print(f"[{_now()}] Holiday calendar for {roc_year} is not published yet — "
                  f"dropped a stale cached copy.")
        return {}

    entry = {'fetched': datetime.date.today().isoformat(), 'days': found}
    disk[str(roc_year)] = entry
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(disk, f, ensure_ascii=False, indent=2)
    except OSError:
        pass
    _HOLIDAY_CACHE[roc_year] = entry
    return found


def warm_holiday_cache(today=None):
    """Pre-load this year's calendar, and next year's once TWSE has issued it.

    Called at scheduler start-up. Without it the year rollover would depend on
    the exchange being reachable at the exact moment the first job of January
    runs; with it, next year's holidays are already on disk from whenever it was
    first published (TWSE issues them in the autumn). Returns {roc_year: n_days}.
    """
    today = today or datetime.datetime.now(ZoneInfo('Asia/Taipei')).date()
    years = [today.year - 1911]
    if today.month >= 10:            # next year's schedule is out around then
        years.append(today.year - 1911 + 1)
    out = {}
    for y in years:
        try:
            out[y] = len(fetch_market_holidays(y))
        except Exception as exc:                                   # noqa: BLE001
            print(f"[{_now()}] Holiday warm-up failed for {y}: {type(exc).__name__}: {exc}")
            out[y] = 0
    return out


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


# ---------------------------------------------------------------------------
# Brokerage cash — an append-only ledger (added 2026-10-02)
#
# Peter's rule: cash never enters a performance number. It appears only as a
# balance, in a total, and as a share of that total. The repo tracks his
# brokerage settlement account (交割戶) and nothing else.
#
# Every change is a typed, dated, noted entry that is never edited or deleted —
# a mistake is fixed by a new entry saying so — so the ledger is a track
# record, and the balance is always replayed from it, never stored beside it.
#
#   open      the opening balance: day zero, counting starts here
#   deposit   new money in from outside   (an external flow — never a gain)
#   withdraw  money taken out to spend    (an external flow — never a loss)
#   update    set the balance to what the broker shows; the difference is
#             investment result (a dividend landing, a sale settling, a fee)
#
# amount is SIGNED (+ in, − out); for update it is the difference applied, so
# the balance is simply open + Σ amount. Lives ONLY in DATA_DIR: _config_path's
# repo-root fallback is not covered by .gitignore, and the repo is public.
# ---------------------------------------------------------------------------

CASH_LEDGER_FILE = 'cash_ledger.jsonl'
CASH_TYPES       = ('open', 'deposit', 'withdraw', 'update')
CASH_TYPE_LABEL  = {'open': '開帳', 'deposit': '存入', 'withdraw': '提出', 'update': '調整'}
CASH_STALE_DAYS  = 30
CASH_MAX_AMOUNT  = 1e10          # a typo guard, not a policy
CASH_HISTORY_DIR = 'cash_ledger_history'   # the ledger as it stood before each write
_cash_cache = {'key': None, 'status': None}


def cash_ledger_path():
    return os.path.join(DATA_DIR, CASH_LEDGER_FILE)


def load_cash_ledger():
    """(entries, bad_line_count), in the order written. Never raises: a scheduled
    push must not die over a side file, but a bad line is counted, not hidden."""
    entries, bad = [], 0
    try:
        with open(cash_ledger_path(), encoding='utf-8') as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    e = json.loads(line)
                    if not isinstance(e, dict) or e.get('type') not in CASH_TYPES:
                        raise ValueError('unknown entry')
                    e['amount'] = float(e['amount'])
                    if e['amount'] != e['amount'] or abs(e['amount']) == float('inf'):
                        raise ValueError('not a number')
                    datetime.date.fromisoformat(str(e['date']))
                    entries.append(e)
                except (ValueError, TypeError, KeyError):
                    bad += 1
    except FileNotFoundError:
        return [], 0
    except OSError:
        return [], 1
    return entries, bad


def _taipei_today():
    return datetime.datetime.now(ZoneInfo('Asia/Taipei')).date()


def cash_status():
    """None when there is no ledger (the cash feature is simply off). Otherwise
    {'balance', 'opened', 'as_of', 'age_days', 'stale', 'net_flow', 'n', 'bad'}.

    balance is None when the ledger has no opening entry. as_of is the day the
    figure was last touched, which is what staleness is about. net_flow is
    deposits − withdrawals since opening: the money Peter put in himself, which
    stage 3 subtracts from wealth growth so it never reads as a gain.

    Cached on the file's size + mtime, because the live board asks on every
    repaint (~4×/second)."""
    try:
        st = os.stat(cash_ledger_path())
    except OSError:
        return None
    key = (st.st_mtime_ns, st.st_size)
    if _cash_cache['key'] == key:
        return _cash_cache['status']
    entries, bad = load_cash_ledger()
    bal, opened, flow, last = None, None, 0.0, None
    for e in entries:
        if e['type'] == 'open':
            if bal is None:
                bal, opened = e['amount'], e['date']
            continue
        if bal is None:
            continue
        bal += e['amount']
        if e['type'] in ('deposit', 'withdraw'):
            flow += e['amount']
    for e in entries:
        stamp = str(e.get('entered_at') or e['date'])[:10]
        last = stamp if last is None or stamp > last else last
    age = None
    if last:
        try:
            age = (_taipei_today() - datetime.date.fromisoformat(last)).days
        except ValueError:
            age = None
    status = {'balance': bal, 'opened': opened, 'as_of': last, 'age_days': age,
              'stale': bool(age is not None and age > CASH_STALE_DAYS),
              'net_flow': flow, 'n': len(entries), 'bad': bad}
    _cash_cache.update(key=key, status=status)
    return status


def add_cash_entry(kind, value, note='', date=None):
    """Append one entry and return it, or raise ValueError with a reason in
    Chinese that the dashboard can show as-is.

    value is what Peter types: the opening balance for 'open', the amount moved
    for 'deposit' / 'withdraw' (always positive), and the balance the broker now
    shows for 'update'. The file is rewritten atomically with the old bytes
    untouched plus one new line, so a reader never sees half an entry and no
    earlier entry can change."""
    if kind not in CASH_TYPES:
        raise ValueError(f'未知的類型：{kind}')
    try:
        value = float(str(value).replace(',', '').strip())
    except ValueError:
        raise ValueError('金額必須是數字') from None
    if value != value or value < 0 or value > CASH_MAX_AMOUNT:
        raise ValueError('金額必須是 0 以上的合理數字')
    note = (note or '').strip()[:200]
    today = _taipei_today()
    try:
        day = datetime.date.fromisoformat(str(date)) if date else today
    except ValueError:
        raise ValueError('日期格式應為 YYYY-MM-DD') from None
    if day > today:
        raise ValueError('日期不能在未來')

    entries, bad = load_cash_ledger()
    if bad:
        raise ValueError(f'現金帳本有 {bad} 行無法讀取 — 請先檢查 {cash_ledger_path()}，'
                         f'不寫入新紀錄以免帳目錯亂')
    status = cash_status() if entries else None
    balance = status['balance'] if status else None

    if kind == 'open':
        if entries:
            raise ValueError('帳本已開帳；初始餘額只能設定一次（要修正請用「調整」）')
        amount, note = value, (note or '初始餘額')
    else:
        if balance is None:
            raise ValueError('請先開帳（設定初始餘額）')
        if day < datetime.date.fromisoformat(status['opened']):
            raise ValueError(f"日期不能早於開帳日 {status['opened']}")
        if not note:
            raise ValueError('請寫下這筆紀錄的原因')
        if kind == 'deposit':
            if value == 0:
                raise ValueError('存入金額必須大於 0')
            amount = value
        elif kind == 'withdraw':
            if value == 0:
                raise ValueError('提出金額必須大於 0')
            if value > balance + 1e-9:
                raise ValueError(f'提出金額超過目前餘額 {balance:,.0f}元')
            amount = -value
        else:                                   # update: set to what the broker shows
            amount = value - balance

    entry = {
        'id': max((int(e.get('id', 0)) for e in entries), default=0) + 1,
        'entered_at': datetime.datetime.now(ZoneInfo('Asia/Taipei')).strftime('%Y-%m-%d %H:%M:%S'),
        'date': day.isoformat(),
        'type': kind,
        'amount': round(amount, 2),
        'balance_after': round((balance or 0.0) + amount if kind != 'open' else amount, 2),
        'note': note,
    }
    path = cash_ledger_path()
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        with open(path, 'rb') as f:
            old = f.read()
    except FileNotFoundError:
        old = b''
    if old:
        # Keep the ledger as it stood before this write. Local only, by Peter's
        # rule (his financial records never go online); it guards against a
        # corrupted file or a slip, not against losing the Mac — the encrypted
        # Time Machine drive covers that.
        hist = os.path.join(DATA_DIR, CASH_HISTORY_DIR)
        os.makedirs(hist, exist_ok=True)
        stamp = datetime.datetime.now(ZoneInfo('Asia/Taipei')).strftime('%Y%m%d_%H%M%S')
        with open(os.path.join(hist, f"cash_ledger_before_{entry['id']:04d}_{stamp}.jsonl"), 'wb') as f:
            f.write(old)
    if old and not old.endswith(b'\n'):
        old += b'\n'
    tmp = path + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(old + (json.dumps(entry, ensure_ascii=False) + '\n').encode('utf-8'))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    _cash_cache['key'] = None
    return entry


def compute_positions(portfolio, quotes, market_open=None, with_cash=True):
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
    # Cash rides alongside, never inside: every key above stays stocks-only, so
    # P/L, P/L% and 今日損益 mean exactly what they meant before cash existed.
    # With no ledger nothing is added at all, and every caller sees the old dict.
    cs = cash_status() if with_cash else None
    if cs and cs.get('balance') is not None:
        cash = cs['balance']
        # A partial book would understate wealth and skew the split (measured:
        # 2 of 8 unpriced moved the cash share by 10.9 points), so the total and
        # the split are only offered when every holding is priced.
        valid = n_unpriced == 0 and (tot_val + cash) > 0
        total.update({
            'cash': cash, 'cash_as_of': cs['as_of'], 'cash_age_days': cs['age_days'],
            'cash_stale': cs['stale'], 'cash_bad_lines': cs['bad'],
            'cash_net_flow': cs['net_flow'], 'cash_opened': cs['opened'],
            'allocation_valid': valid,
            'total_wealth': (tot_val + cash) if valid else None,
            'equity_pct': (tot_val / (tot_val + cash) * 100) if valid else None,
            'cash_pct': (cash / (tot_val + cash) * 100) if valid else None,
        })
    return {'rows': rows, 'total': total, 'n_unpriced': n_unpriced, 'frozen': frozen}


STREAK_FLAT_RULES = ('break', 'skip')


def streak_flat_rule(cfg=None):
    """bot_config streak.flat_rule — 'break' (default, the rule live since
    2026-09-24) or 'skip'. Anything else reads as 'break'."""
    cfg = cfg if cfg is not None else load_bot_config()
    rule = str(((cfg or {}).get('streak') or {}).get('flat_rule', 'break')).lower()
    return rule if rule in STREAK_FLAT_RULES else 'break'


def close_streak(closes, market_open=None, today=None, flat_rule=None):
    """Trailing run of same-direction daily closes → {'run', 'pct', 'as_of'}, or None.

    run is signed: +3 = three straight higher closes, -2 = two straight lower.
    pct is the total move over that run, from the close just BEFORE it began
    to the newest close, in percent. as_of is the date of the newest close
    counted — the morning push prints the run as "through yesterday's close",
    and this is what makes that claim checkable.

    A flat close (exactly unchanged) is governed by flat_rule:
      'break' — the default, and the rule live since 2026-09-24: a flat newest
                close returns run 0, and a flat day mid-run ends the run.
      'skip'  — a flat day neither counts nor breaks; only a move in the
                opposite direction ends a run. Measured 2026-09-30: one ETF was
                flat on 5 of 23 sessions, so 'break' chops its runs short.
    None resolves from bot_config streak.flat_rule, else 'break'.

    Only closed sessions count. While the market is open Yahoo's daily frame
    carries today's in-progress bar; it is dropped here because it can still
    flip before 13:30 — today's live move is already on the board as Chg%.
    Returns None when fewer than two closes remain (nothing to compare).
    """
    if market_open is None:
        market_open = taiwan_market_open()
    if flat_rule is None:
        flat_rule = streak_flat_rule()
    s = pd.Series(closes).dropna()
    if market_open and len(s):
        today = today or datetime.datetime.now(ZoneInfo('Asia/Taipei')).date()
        last = s.index[-1]
        if (last.date() if hasattr(last, 'date') else last) == today:
            s = s.iloc[:-1]
    if len(s) < 2:
        return None
    last = s.index[-1]
    as_of = last.date() if hasattr(last, 'date') else None
    vals = s.tolist()
    diffs = [b - a for a, b in zip(vals, vals[1:])]
    skip = flat_rule == 'skip'

    newest = next((d for d in reversed(diffs) if d != 0), 0) if skip else diffs[-1]
    if newest == 0:
        return {'run': 0, 'pct': 0.0, 'as_of': as_of}
    up = newest > 0
    run, start_i = 0, len(diffs)
    for k in range(len(diffs) - 1, -1, -1):
        d = diffs[k]
        if d == 0:
            if skip:
                continue
            break
        if (d > 0) != up:
            break
        run += 1
        start_i = k            # diffs[k] runs vals[k] -> vals[k+1]; the run began at vals[k]
    start = vals[start_i]
    pct = (vals[-1] / start - 1.0) * 100 if start else 0.0
    return {'run': run if up else -run, 'pct': pct, 'as_of': as_of}



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


# ---------------------------------------------------------------------------
# The 09:05 morning push — exchange real-time prices, nothing yesterday-sourced.
# Contract: docs/MORNING_REBUILD_2026-10-02.md
# ---------------------------------------------------------------------------

_OFFICIAL_CLOSE_FILE = 'official_close_cache.json'
_OFFICIAL_CLOSE_KEEP = 90           # dates retained on disk
_official_close_mem = None          # {YYYYMMDD: {code: close}}, loaded once per process


def _as_date(i):
    return i.date() if hasattr(i, 'date') else i


def _official_close(day, code):
    """TWSE's own closing price for `code` on `day`, or None.

    One MI_INDEX call covers every listed code for that date, and a past close
    never changes, so each date is fetched once and kept on disk. A failed or
    unpublished date is not cached, so it is simply asked again next time."""
    global _official_close_mem
    path = os.path.join(DATA_DIR, _OFFICIAL_CLOSE_FILE)
    if _official_close_mem is None:
        try:
            with open(path, encoding='utf-8') as f:
                _official_close_mem = json.load(f)
            if not isinstance(_official_close_mem, dict):
                _official_close_mem = {}
        except (OSError, ValueError):
            _official_close_mem = {}
    key = day.strftime('%Y%m%d')
    if key not in _official_close_mem:
        rows = fetch_twse_mi_index(key)
        closes = {}
        for r in rows or []:
            px = _f(r.get('ClosingPrice'))
            if px is not None and r.get('Code'):
                closes[str(r['Code'])] = px
        if not closes:
            return None
        _official_close_mem[key] = closes
        for old in sorted(_official_close_mem)[:-_OFFICIAL_CLOSE_KEEP]:
            del _official_close_mem[old]
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(path + '.tmp', 'w', encoding='utf-8') as f:
                json.dump(_official_close_mem, f, ensure_ascii=False)
            os.replace(path + '.tmp', path)
        except OSError:
            pass
    return _official_close_mem[key].get(str(code))


def repair_daily_closes(code, closes, today, prev_close=None, lookback=30):
    """Daily closes fit to count a streak on, through the last settled session.

    Yahoo's daily frame silently omits whole sessions for Taiwan ETFs. Measured
    2026-10-02 over 65 sessions: every ETF checked was missing 2026-10-01,
    while the ordinary stocks checked were missing nothing. A dropped session merges two
    days into one step, so the run is miscounted — and the streak is the one
    figure the morning push asks Peter to act on.

    So: bars dated `today` or later are dropped (today is unsettled); any trading
    day inside the trailing `lookback` sessions that Yahoo lacks is filled from
    TWSE MI_INDEX; and the newest session is pinned to `prev_close`, the
    exchange's own 昨收, when one is given.
    """
    s = pd.Series(closes).dropna()
    if s.empty:
        return s
    s = s[[_as_date(i) < today for i in s.index]]
    if s.empty:
        return s
    first = _as_date(s.index[0])
    sessions, day = [], today - datetime.timedelta(days=1)
    while len(sessions) < lookback and day >= first:
        if is_trading_day(day):
            sessions.append(day)
        day -= datetime.timedelta(days=1)

    have = {_as_date(i) for i in s.index}
    fill = {}
    for day in sessions:
        if day not in have:
            px = _official_close(day, code)
            if px is not None:
                fill[day] = px
    if prev_close and sessions:
        fill[sessions[0]] = float(prev_close)
    if not fill:
        return s

    s = s.copy()
    for day, px in fill.items():
        hit = [i for i in s.index if _as_date(i) == day]
        if hit:
            s.loc[hit[-1]] = px
        else:
            ts = pd.Timestamp(day)
            if getattr(s.index, 'tz', None) is not None:
                ts = ts.tz_localize(s.index.tz)
            s.loc[ts] = px
    return s.sort_index()


def streak_marker_threshold(cfg=None):
    """How many sessions a run needs before the morning push flags it ⚡ / ⚠️.
    bot_config streak.marker_threshold, else streak_alert.threshold, else 3."""
    cfg = cfg if cfg is not None else load_bot_config()
    for section, key in (('streak', 'marker_threshold'), ('streak_alert', 'threshold')):
        try:
            v = int(((cfg or {}).get(section) or {}).get(key) or 0)
        except (TypeError, ValueError):
            v = 0
        if v > 0:
            return v
    return 3


def last_pool_record(mode='closing'):
    """The newest diary/pool.jsonl record for `mode`, or None.

    The morning push carries last night's closing facts forward (📌 昨日重點)
    from what the closing push already recorded, rather than fetching again."""
    last = None
    try:
        with open(os.path.join(DATA_DIR, 'diary', 'pool.jsonl'), encoding='utf-8') as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and rec.get('mode') == mode:
                    last = rec
    except OSError:
        return None
    return last


def _morning_snapshot(cfg):
    """Everything the 09:05 push prints. Holdings only — the morning push carries
    no watchlist — and no RSI / 量比 / 融資 / PE: those are all yesterday's at
    09:05, and last night's closing push already delivered them.

    Prices come from the exchange's own real-time feed. Yahoo fills only codes
    the exchange cannot price, and every such code is listed in delayed_codes so
    the push says so instead of presenting a delayed price as live.
    """
    portfolio = load_portfolio()
    codes = list(portfolio)
    today = datetime.datetime.now(ZoneInfo('Asia/Taipei')).date()

    print(f"[{_now()}] [MORNING] Exchange real-time quotes "
          f"({len(codes)} holdings + index)...")
    not_traded = []
    mis = fetch_mis_quotes(codes, want_index=True, unmatched=not_traded)
    idx = mis.pop('_t00', None)
    quotes = dict(mis)

    no_exchange = [c for c in codes if c not in quotes and c not in not_traded]
    if no_exchange:
        print(f"[{_now()}] [MORNING] exchange has no price for {', '.join(no_exchange)} "
              f"— Yahoo (delayed)")
        for c, q in fetch_live_quotes(no_exchange,
                                      resolve_symbols(no_exchange, DATA_DIR)).items():
            q = dict(q)
            q['src'], q['exch_open'] = 'yahoo', None
            quotes[c] = q
    delayed = [c for c in no_exchange if c in quotes]
    n_mis = sum(1 for q in quotes.values() if q.get('src') == 'mis')
    price_source = ('none' if not quotes else 'mis' if not delayed
                    else 'yahoo' if not n_mis else 'mixed')
    delayed_at = max((quotes[c]['quote_at'] for c in delayed
                      if quotes[c].get('quote_at')), default=None)

    if idx:
        taiex_src = 'mis'
        taiex = {'close': idx['price'], 'last': idx['price'],
                 'open': idx['today_open'], 'prev_close': idx['prev_close'],
                 'change': idx['change'],
                 'pct': (idx['change'] / idx['prev_close'] * 100) if idx['prev_close'] else 0.0}
    else:
        taiex_src = 'yahoo'
        t = fetch_taiex()
        taiex = dict(t, last=t['close'], prev_close=t['close'] - t['change']) if t else None

    stamps = [q['quote_at'] for q in quotes.values()
              if q.get('src') == 'mis' and q.get('quote_at')]
    if idx and idx.get('quote_at'):
        stamps.append(idx['quote_at'])
    price_time = max(stamps, default=None)

    print(f"[{_now()}] [MORNING] Overnight global indices...")
    global_lines = fetch_global_indices(cfg.get('global_indices'))

    positions = compute_positions(portfolio, quotes)

    print(f"[{_now()}] [MORNING] Daily closes for streaks...")
    flat_rule = streak_flat_rule(cfg)
    symbol_map = resolve_symbols(codes, DATA_DIR)
    bars = fetch_history(list(symbol_map.values()), '3mo', '1d') or {}
    streaks = {}
    for c in codes:
        sub = bars.get(symbol_map.get(c))
        if sub is None or 'Close' not in getattr(sub, 'columns', []):
            continue
        q = quotes.get(c) or {}
        # Only the exchange's 昨收 is trusted to pin the newest session: at 09:05
        # Yahoo has not rolled over yet, so its previousClose is a day stale.
        prev = q.get('prev_close') if q.get('src') == 'mis' else None
        closes = repair_daily_closes(c, sub['Close'], today, prev_close=prev)
        streaks[c] = close_streak(closes, market_open=False, flat_rule=flat_rule)

    holdings, missing_holdings = [], []
    for code, pos in portfolio.items():
        name = pos.get('name', code)
        q = quotes.get(code)
        if not q:
            missing_holdings.append((code, name))
            continue
        r = next((x for x in positions['rows'] if x['code'] == code), None)
        price, prev = q['price'], q['prev_close']
        change = price - prev if prev else 0.0
        holdings.append({
            'code': code, 'name': name, 'pos': pos,
            'price': price, 'prev_cls': prev, 'open_p': q.get('exch_open'),
            'change': change, 'pct': (change / prev * 100) if prev else 0.0,
            'daily_pnl': r['daily_pnl'] if r else None,
            'stale': r['stale'] if r else False,
            'src': q.get('src', 'yahoo'), 'quote_at': q.get('quote_at'),
            'streak': streaks.get(code),
        })

    now = datetime.datetime.now(ZoneInfo('Asia/Taipei'))   # TZ=UTC under the scheduler
    return {
        'mode': 'morning', 'cfg': cfg, 'period_days': None,
        'date_str': now.strftime('%Y-%m-%d'), 'time_str': now.strftime('%H:%M'),
        'price_time': price_time, 'price_source': price_source,
        'delayed_codes': delayed, 'delayed_at': delayed_at, 'not_traded': not_traded,
        'portfolio': portfolio, 'tracked': {}, 'tracked_notes': {},
        'taiex': taiex, 'taiex_pct': (taiex['pct'] if taiex else 0.0),
        'taiex_src': taiex_src,
        'global_lines': global_lines,
        'quotes': quotes, 'positions': positions,
        'holdings': holdings, 'watchlist': [],
        'missing_holdings': missing_holdings, 'missing_watch': [],
        'hotlist': {'top_volume': [], 'top_losers': []}, 'news': [],
        'is_stale': False, 'feed_date': '', 'prev_mismatch': [],
        'streak_threshold': streak_marker_threshold(cfg), 'flat_rule': flat_rule,
        'yesterday': last_pool_record('closing'),
    }


def snapshot(mode, cfg=None):
    """EVERYTHING the twice-daily report prints, fetched once and derived once.

    The reports call this and render the result. They do not fetch and they do
    not calculate — that is the whole point of this module. Both modes end up in
    the same compute_positions(), so the morning push, the afternoon push and
    the dashboard's own Total row cannot disagree about your P&L.

    Returns None only when the closing feed is unavailable (caller aborts).
    """
    cfg = cfg if cfg is not None else load_bot_config()
    if mode == 'morning':
        # Its own builder since the 09:05 rebuild. Everything below this line is
        # the closing path, deliberately left exactly as it was.
        return _morning_snapshot(cfg)
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
