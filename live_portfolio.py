"""
Live portfolio view — auto-refreshing holdings table + candlestick graphs.

Launched from config_tui.py ([g] Graphs under Portfolio Holdings). All network
I/O runs in a background worker thread; the UI repaints from shared state via
rich.Live, so the screen never blocks on a fetch.

Portfolio candles are CURRENT shares × historical prices ("what today's
holdings would have been worth") — purchase dates are not tracked. The summed
per-stock highs/lows slightly overstate true portfolio extremes because
per-stock extremes don't coincide within a bar; acceptable approximation.
"""

import json
import os
import re
import select
import sys
import threading
import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf
import plotext as plt
from rich.console import Group
from rich.table import Table
from rich.text import Text
from rich import box

from custom_stock_lookup import get_yfinance_data

# ---------------------------------------------------------------------------
# Ranges: key -> (label, yfinance period, yfinance interval, cache TTL secs)
# Intervals stay within yfinance limits (5m ~60d back, 30m ~60d, 1h ~730d).
# ---------------------------------------------------------------------------

RANGES = {
    '1': ('Day',   '1d',  '5m',  150),
    '2': ('Week',  '5d',  '30m', 3600),
    '3': ('Month', '1mo', '1d',  3600),
    '4': ('3M',    '3mo', '1d',  3600),
    '5': ('1Y',    '1y',  '1d',  3600),
    '6': ('3Y',    '3y',  '1wk', 3600),
    '7': ('5Y',    '5y',  '1wk', 3600),
}

# The live board's heartbeat. Affordable only because the quote path is ONE
# batched request for the whole book (see fetch_quote_batch) — yf.download costs
# one HTTP call per ticker, so a 15-holding portfolio on this cadence would be
# 45 req/min and straight into the 429s that used to blank the board.
QUOTE_TTL_OPEN   = 20    # live board cadence during market hours
QUOTE_TTL_CLOSED = 600

_QUOTE_URL = 'https://query1.finance.yahoo.com/v7/finance/quote'

_OHLC = ['Open', 'High', 'Low', 'Close']


# ---------------------------------------------------------------------------
# Symbol resolution (.TW vs .TWO) with a persistent cache
# ---------------------------------------------------------------------------

def _suffix_cache_path(data_dir):
    return Path(data_dir) / 'ticker_suffix_cache.json'


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


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

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


def portfolio_ohlc(hist, portfolio, symbol_map, quotes=None):
    """Synthesize total-portfolio OHLC: per bar, Σ shares × per-stock O/H/L/C.

    Symbols with no bars at this interval (thin TPEX names, active ETFs) are
    held flat at their live/last price so the total never silently drops a
    holding. Returns (ohlc_df, n_flat) where n_flat is the count held flat."""
    frames, flat = [], []
    for code, pos in portfolio.items():
        shares = pos.get('shares', 0)
        if not shares:
            continue
        sub = hist.get(symbol_map.get(code))
        if sub is not None and not sub.empty and set(_OHLC) <= set(sub.columns):
            frames.append((code, sub[_OHLC] * shares))
        else:
            flat.append((code, shares))
    if not frames:
        return pd.DataFrame(), len(flat)

    idx = frames[0][1].index
    for _, f in frames[1:]:
        idx = idx.union(f.index)

    total = pd.DataFrame(0.0, index=idx, columns=_OHLC)
    for _, f in frames:
        total += f.reindex(idx).ffill().bfill()

    for code, shares in flat:
        q = (quotes or {}).get(code)
        price = q['price'] if q else None
        if price:
            total += price * shares       # constant contribution across all bars

    # Drop bars where most symbols had no data (pre-ffill) — common intraday gaps
    closes = pd.concat([f['Close'].reindex(idx) for _, f in frames], axis=1)
    keep = closes.notna().sum(axis=1) >= max(1, len(frames) // 2)
    return total[keep], len(flat)


def taiwan_market_open(now=None):
    if os.getenv('LIVE_PORTFOLIO_FORCE_OPEN') == '1':
        return True
    now = now or datetime.datetime.now(ZoneInfo('Asia/Taipei'))
    if now.weekday() >= 5:
        return False
    t = now.time()
    return datetime.time(9, 0) <= t <= datetime.time(13, 30)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def candle_renderable(ohlc_df, title, width, height, hline=None, y_mode='linear'):
    """plotext chart → ANSI → rich Text (embeddable in Live/Group).

    y_mode: 'linear' → $ candlestick · 'log' → log-$ candlestick (equal % moves =
    equal height, so fluctuation stays visible on long ranges where the portfolio
    has grown a lot) · 'pct' → % change from the window's first bar (line)."""
    if ohlc_df is None or ohlc_df.empty:
        return Text('（無資料 / no data at this interval）', style='dim')
    if height < 8 or width < 40:
        return Text('Terminal too small for chart — resize to view.', style='yellow')

    idx = ohlc_df.index
    intraday = len(idx) > 1 and (idx[1] - idx[0]) < pd.Timedelta(days=1)
    span = (idx[-1] - idx[0]) if len(idx) > 1 else pd.Timedelta(0)
    # Pick the shortest label that stays unambiguous, so the x-axis stays legible:
    #   single intraday day → time only · multi-day intraday → d/m H:M
    #   ≤ ~1 year of daily bars → d/m (the year is constant — repeating it is clutter)
    #   multi-year → m/Y (individual days aren't meaningful at that zoom)
    if intraday:
        if span < pd.Timedelta(days=1):
            form, fmt = ('H:M', '%H:%M')
        else:
            form, fmt = ('d/m H:M', '%d/%m %H:%M')
    elif span > pd.Timedelta(days=400):
        form, fmt = ('m/Y', '%m/%Y')
    else:
        form, fmt = ('d/m', '%d/%m')

    plt.clear_figure()
    plt.theme('clear')
    plt.date_form(form)
    plt.plotsize(width, height)
    # plotext re-displays parsed times in the MACHINE's local tz (treating the
    # naive string as UTC), which shifts intraday labels by the local offset —
    # e.g. an 09:00 Taipei bar renders as 17:00 on a UTC+8 host. Pre-subtract the
    # machine offset for time-bearing labels so plotext lands back on the real
    # session time. Cross-env safe: the offset is 0 in the UTC container.
    # Skip for date-only (daily/weekly) labels so a bar can't slip across midnight.
    if intraday:
        off = datetime.datetime.now().astimezone().utcoffset() or datetime.timedelta(0)
        dates = [(ts - off).strftime(fmt) for ts in idx]
    else:
        dates = [ts.strftime(fmt) for ts in idx]
    if y_mode == 'pct':
        base = float(ohlc_df['Close'].iloc[0]) or 1.0
        plt.plot(dates, [(v / base - 1.0) * 100 for v in ohlc_df['Close'].tolist()])
        plt.hline(0, 'gray')
        plt.title(f'{title}  (% from start)')
    else:
        plt.candlestick(dates, {c: ohlc_df[c].tolist() for c in _OHLC})
        # Zoom Y to the actual price band (+ margin) so real fluctuation is
        # visible. Otherwise a break-even line far below the data (or just a
        # large absolute base) stretches the axis and flattens the candles into
        # a sliver at the top. The per-stock charts pass no hline and so already
        # auto-fit this way — this brings the total chart in line with them.
        lo = float(ohlc_df['Low'].min())
        hi = float(ohlc_df['High'].max())
        pad = (hi - lo) * 0.12 if hi > lo else (abs(hi) * 0.01 or 1.0)
        ylo, yhi = lo - pad, hi + pad
        note = ''
        if hline:
            if ylo <= hline <= yhi:
                plt.hline(hline, 'gray')           # break-even in view — show it
            else:
                # Off-screen once zoomed: keep the break-even context as a title
                # note instead of letting the line drag the axis back out.
                last = float(ohlc_df['Close'].iloc[-1])
                note = f'  ({(last / hline - 1.0) * 100:+.0f}% vs cost)'
        if y_mode == 'log':
            try:
                plt.yscale('log')
            except Exception:
                pass
            # Don't call plt.ylim() under log scale — this plotext version
            # power10's the raw bounds and overflows. With the off-range hline
            # gated out above, plotext already auto-fits log to the data band.
            plt.title(f'{title}  (log $){note}')
        else:
            plt.ylim(ylo, yhi)
            plt.title(f'{title}{note}')
    return Text.from_ansi(plt.build())


def _fmt_signed(v, pct=False):
    s = f'{v:+,.2f}%' if pct else f'{v:+,.0f}'
    # TW convention: red = up, green = down
    color = 'red' if v > 0 else ('green' if v < 0 else 'white')
    return f'[{color}]{s}[/{color}]'


def build_live_table(portfolio, quotes, market_open=None):
    """Returns (table, n_unpriced, frozen_codes).

    n_unpriced / frozen_codes are the two ways the board can lie about being
    live, handed back so the caller can annotate them: holdings with no quote at
    all (excluded from the totals entirely) and holdings priced off the daily
    close because no 1-minute bar exists (shown, but unable to tick)."""
    if market_open is None:
        market_open = taiwan_market_open()
    t = Table(title='Portfolio Holdings — Live', box=box.ROUNDED)
    for col, kw in [('#', dict(style='dim', width=3, justify='right')),
                    ('Code', dict(style='bold cyan', width=8)),
                    ('Name', dict(width=14)),
                    ('Shares', dict(justify='right')),
                    ('Price', dict(justify='right')),
                    ('Chg%', dict(justify='right')),
                    ('Mkt Value', dict(justify='right')),
                    ('P/L', dict(justify='right')),
                    ('P/L%', dict(justify='right')),
                    ('Daily P/L', dict(justify='right'))]:
        t.add_column(col, **kw)

    tot_val = tot_cost = tot_daily_pnl = 0.0
    n_unpriced, frozen = 0, []
    for i, (code, pos) in enumerate(portfolio.items(), 1):
        sh, cost = pos.get('shares', 0), pos.get('cost_basis', 0)
        q = quotes.get(code)
        if q:
            price = q['price']
            chg_pct = (price - q['prev_close']) / q['prev_close'] * 100 if q['prev_close'] else 0
            daily_pnl = (price - q['prev_close']) * sh if q['prev_close'] else 0
            val = price * sh
            pnl = val - cost
            pnl_pct = pnl / cost * 100 if cost else 0
            tot_val += val
            # Cost joins the total only alongside its own market value. Adding it
            # unconditionally booked an unpriced holding's entire position as a
            # loss, so one failed quote could sink the whole portfolio's P/L.
            tot_cost += cost
            tot_daily_pnl += daily_pnl
            px = f'{price:,.2f}'
            if market_open and not q.get('intraday', True):
                px = f'[dim]{px}[/dim]'      # can't tick — see the note under the table
                frozen.append(code)
            t.add_row(str(i), code, pos.get('name', ''), f'{sh:,}',
                      px, _fmt_signed(chg_pct, pct=True),
                      f'{val:,.0f}', _fmt_signed(pnl),
                      _fmt_signed(pnl_pct, pct=True), _fmt_signed(daily_pnl))
        else:
            n_unpriced += 1
            t.add_row(str(i), code, pos.get('name', ''), f'{sh:,}',
                      '[dim]…[/dim]', '', '', '', '', '')

    if tot_val:
        pnl = tot_val - tot_cost
        # Say so when the Total covers only part of the book, so a shrunken
        # figure is never mistaken for the market moving.
        label = ('Total' if not n_unpriced
                 else f'Total ({len(portfolio) - n_unpriced}/{len(portfolio)})')
        t.add_section()
        t.add_row('', '', f'[bold]{label}[/bold]', '', '',
                  '', f'[bold]{tot_val:,.0f}[/bold]', _fmt_signed(pnl),
                  _fmt_signed(pnl / tot_cost * 100 if tot_cost else 0, pct=True),
                  _fmt_signed(tot_daily_pnl))
    return t, n_unpriced, frozen


# ---------------------------------------------------------------------------
# Non-blocking key input (termios; falls back to Ctrl+C-only on non-tty)
# ---------------------------------------------------------------------------

class _KeyReader:
    def __enter__(self):
        self.ok = False
        try:
            import termios, tty
            self.fd = sys.stdin.fileno()
            self.old = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
            self.ok = True
        except Exception:
            pass
        return self

    def read(self):
        """Return one keypress token ('' if none). Specials: ENTER / BS / ESC;
        arrow keys map to n/p. Other keys come back as their raw character
        (case preserved, so search input can type US tickers)."""
        if not self.ok:
            return ''
        if not select.select([sys.stdin], [], [], 0)[0]:
            return ''
        data = os.read(self.fd, 8).decode(errors='ignore')
        if data.startswith('\x1b['):
            return {'\x1b[C': 'n', '\x1b[D': 'p'}.get(data[:3], '')
        if data == '\x1b':
            return 'ESC'
        ch = data[0]
        if ch in ('\r', '\n'):
            return 'ENTER'
        if ch in ('\x7f', '\x08'):
            return 'BS'
        return ch

    def __exit__(self, *a):
        if self.ok:
            import termios
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)


# ---------------------------------------------------------------------------
# Worker thread — owns ALL network I/O
# ---------------------------------------------------------------------------

class _Worker(threading.Thread):
    def __init__(self, portfolio, symbol_map, data_dir):
        super().__init__(daemon=True)
        self.portfolio = portfolio
        self.symbol_map = symbol_map
        self.data_dir = data_dir
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.range_key = '1'                 # currently displayed range
        self.state = {'quotes': {}, 'quotes_at': None, 'quotes_try_at': None,
                      'hist': {}, 'compare': None, 'error': '', 'backoff': 1}

    def set_range(self, key):
        with self.lock:
            self.range_key = key

    def set_compare(self, text):
        with self.lock:
            self.state['compare'] = {'input': text, 'symbol': None, 'name': '',
                                     'quote': None, 'hist': {}, 'error': ''}

    def clear_compare(self):
        with self.lock:
            self.state['compare'] = None

    def snapshot(self):
        with self.lock:
            return dict(self.state), self.range_key

    def run(self):
        while not self.stop_event.is_set():
            try:
                self._cycle()
                with self.lock:
                    self.state['backoff'] = 1
            except Exception as e:
                with self.lock:
                    self.state['error'] = f'{type(e).__name__}: {e}'
                    self.state['backoff'] = min(self.state['backoff'] * 2, 16)
            self.stop_event.wait(2)

    def _stale(self, ts, ttl):
        return ts is None or (datetime.datetime.now() - ts).total_seconds() > ttl

    def _cycle(self):
        with self.lock:
            st, rkey = dict(self.state), self.range_key
        backoff = st['backoff']
        quote_ttl = (QUOTE_TTL_OPEN if taiwan_market_open() else QUOTE_TTL_CLOSED) * backoff

        # Pace off the last ATTEMPT, not the last success. Yahoo returns empty
        # rather than raising when it throttles, so gating on quotes_at meant a
        # failed fetch stayed stale and the 2s worker loop retried immediately —
        # a hot retry storm that guaranteed more throttling while the board sat
        # frozen with nothing on screen saying so.
        if self._stale(st['quotes_try_at'], quote_ttl):
            with self.lock:
                self.state['quotes_try_at'] = datetime.datetime.now()
            quotes = fetch_live_quotes(list(self.portfolio), self.symbol_map)
            with self.lock:
                if quotes:
                    self.state['quotes'] = quotes
                    self.state['quotes_at'] = datetime.datetime.now()
                    self.state['error'] = ''
                else:
                    self.state['error'] = 'quote fetch returned nothing (throttled?)'

        _, period, interval, ttl = RANGES[rkey]
        entry = st['hist'].get(rkey)
        # Non-Day ranges are static intraday; Day refreshes with the market
        if entry is None or self._stale(entry['at'], ttl * backoff):
            hist = fetch_history(self.symbol_map.values(), period, interval)
            with self.lock:
                quotes = self.state['quotes']
            total, n_flat = portfolio_ohlc(hist, self.portfolio, self.symbol_map, quotes)
            with self.lock:
                self.state['hist'][rkey] = {
                    'at': datetime.datetime.now(), 'total': total,
                    'stocks': hist, 'n_flat': n_flat,
                }
                self.state['error'] = ''

        self._cycle_compare(rkey, backoff)

    def _cycle_compare(self, rkey, backoff):
        """Resolve + fetch the user's compare ticker for the current range."""
        with self.lock:
            cmp_ = self.state['compare']
        if not cmp_:
            return
        text, sym = cmp_['input'], cmp_.get('symbol')

        if not sym:
            sym = resolve_any(text, self.data_dir)
            quote = get_yfinance_data(sym) if sym else None
            with self.lock:
                c = self.state['compare']
                if not c or c['input'] != text:   # user changed/cleared mid-fetch
                    return
                c['symbol'] = sym
                c['quote'] = quote
                c['name'] = (quote or {}).get('name', '')
                if quote is None:
                    c['error'] = f'{text}: not found'
                    return
                c['error'] = ''

        _, period, interval, ttl = RANGES[rkey]
        entry = cmp_['hist'].get(rkey)
        if entry is None or self._stale(entry['at'], ttl * backoff):
            df = fetch_history([sym], period, interval).get(sym)
            with self.lock:
                c = self.state['compare']
                if not c or c['input'] != text:
                    return
                c['hist'][rkey] = {'at': datetime.datetime.now(), 'df': df}
                c['error'] = ('' if df is not None and not df.empty
                              else f'{text}: no data at this interval')


# ---------------------------------------------------------------------------
# Main view — page 0: live table + total candle; pages 1..N: one per holding
# (top half) + compare-ticker search bar (middle) + compared graph (bottom half)
# ---------------------------------------------------------------------------

def _hero_line(st, label, page, n_pages):
    """Status line shown at the very top of every page."""
    mkt = '[red]OPEN[/red]' if taiwan_market_open() else '[dim]CLOSED — showing last session[/dim]'
    if st['quotes_at']:
        # Age, not just a timestamp: the screen repaints 4×/s off cached quotes,
        # so this is the only thing that says whether the numbers are live.
        age = int((datetime.datetime.now() - st['quotes_at']).total_seconds())
        at = st['quotes_at'].strftime('%H:%M:%S') + f' [dim]({age}s ago)[/dim]'
    else:
        at = '—'
    # When we polled ≠ when the exchange last printed. The TW feed is delayed, so
    # show Yahoo's own quote stamp too — otherwise a fresh poll of a stale price
    # looks like a live market that isn't moving.
    stamps = {q['quote_at'] for q in st.get('quotes', {}).values() if q.get('quote_at')}
    quoted = f' · quote {max(stamps).strftime("%H:%M:%S")}' if stamps else ''
    s = (f'market {mkt} · updated {at}{quoted} · range [bold]{label}[/bold] · '
         f'page {page+1}/{n_pages}')
    if st['error']:
        s += f' · [yellow]stale — {st["error"]}[/yellow]'
    return Text.from_markup(s)


def _search_bar(st, search_mode, search_buf, width):
    """The compare-ticker bar rendered between the two graphs — drawn as a
    full-width grey input field so the search feature is visible without
    pressing '/'. Brighter grey while focused (search mode)."""
    cmp_ = st['compare']
    if search_mode:
        markup = (f' 🔍 Compare ▸ [bold white]{search_buf}[/bold white]▌  '
                  f'[dim](Enter = go · Esc = cancel)[/dim] ')
        bg = 'grey35'
    elif cmp_ is None:
        markup = (' 🔍 Compare ▸ [dim]press / and type any ticker — '
                  '2330, 0050, AAPL, NVDA…[/dim] ')
        bg = 'grey23'
    elif cmp_['error']:
        markup = (f' 🔍 Compare ▸ [yellow]{cmp_["error"]}[/yellow]  '
                  f'[dim](/ retry · c clear)[/dim] ')
        bg = 'grey23'
    else:
        who = cmp_['input'] + (f' {cmp_["name"]}' if cmp_.get('name') else '')
        last = f' · last {cmp_["quote"]["price"]:,.2f}' if cmp_.get('quote') else ''
        markup = (f' 🔍 Compare ▸ [bold cyan]{who}[/bold cyan]{last}  '
                  f'[dim](/ change · c clear)[/dim] ')
        bg = 'grey23'
    bar = Text.from_markup(markup, style=f'on {bg}')
    if bar.cell_len < width:
        bar.pad_right(width - bar.cell_len)
    return bar


_YMODE_TAG = {'linear': '$', 'log': 'log$', 'pct': '%ret'}


def _render_view(console, portfolio, codes, symbol_map, st, range_key, page,
                 n_pages, total_cost, search_mode, search_buf, y_mode='linear'):
    entry = st['hist'].get(range_key)
    label = RANGES[range_key][0]
    w = console.size.width - 4
    H = console.size.height
    parts = [_hero_line(st, label, page, n_pages)]
    ykeys = f'\\[l] log \\[%] %ret [dim]({_YMODE_TAG[y_mode]})[/dim]'

    if page == 0:
        table, n_unpriced, frozen = build_live_table(portfolio, st['quotes'])
        notes = []
        # Only after a fetch has actually landed — before that every holding is
        # "unpriced" simply because the first cycle hasn't returned yet, and the
        # warning would fire on every startup.
        if n_unpriced and st['quotes_at']:
            notes.append(f'⚠ {n_unpriced} holding(s) have no quote — left out of Total '
                         f'(both value and cost)')
        if frozen:
            notes.append(f'⚠ no intraday feed for {", ".join(frozen)} — priced at last '
                         f'close, will not move until the next session')
        if entry and entry['n_flat']:
            notes.append(f"⚠ {entry['n_flat']} holding(s) shown at last close "
                         f"(no bars at this interval)")
        # Reserve a row per note so the chart shrinks instead of scrolling them off.
        chart_h = max(H - len(portfolio) - 16 - len(notes), 8)
        parts.append(candle_renderable(entry['total'], f'Total Portfolio — {label}',
                                       w, chart_h, hline=total_cost, y_mode=y_mode)
                     if entry is not None else Text('fetching…', style='dim'))
        parts.insert(1, table)
        for note in notes:
            parts.append(Text(note, style='yellow'))
        keybar = f'[dim]\\[1-7] range  \\[n/p ←/→] page  {ykeys}  \\[q] back[/dim]'
    else:
        code = codes[page - 1]
        pos = portfolio[code]
        q = st['quotes'].get(code)
        head = f"{code} {pos.get('name','')} — {pos.get('shares',0):,} shares"
        if q:
            head += f" · last {q['price']:,.2f}"
            if taiwan_market_open() and not q.get('intraday', True):
                head += ' (last close — no intraday feed)'
        parts.append(Text(head, style='bold cyan'))

        # Top half: the holding's graph; bottom half: the compared ticker's.
        h_each = max((H - 7) // 2, 8)
        sub = entry['stocks'].get(symbol_map[code]) if entry else None
        parts.append(candle_renderable(
            sub[_OHLC] if sub is not None and not sub.empty else None,
            f'{code} — {label}', w, h_each, y_mode=y_mode)
            if entry is not None else Text('fetching…', style='dim'))

        parts.append(_search_bar(st, search_mode, search_buf, w))

        cmp_ = st['compare']
        if cmp_ and not cmp_['error']:
            centry = cmp_['hist'].get(range_key)
            if centry and centry['df'] is not None and not centry['df'].empty:
                title = cmp_['input'] + (f' {cmp_["name"]}' if cmp_.get('name') else '')
                parts.append(candle_renderable(centry['df'][_OHLC],
                                               f'{title} — {label}', w, h_each, y_mode=y_mode))
            else:
                parts.append(Text('fetching…', style='dim'))
        keybar = ('[dim]\\[1-7] range  \\[n/p] page  \\[/] compare  '
                  f'{ykeys}  \\[c] clear  \\[q] back[/dim]')

    parts.append(Text.from_markup(keybar))
    return Group(*parts)


def graphs_view(console, portfolio, data_dir):
    from rich.live import Live

    codes = [c for c, p in portfolio.items() if p.get('shares')]
    if not codes:
        console.print('[yellow]Portfolio is empty — nothing to graph.[/yellow]')
        return

    console.print('[dim]Resolving tickers (.TW/.TWO, cached after first run)...[/dim]')
    symbol_map = resolve_symbols(codes, data_dir)
    total_cost = sum(p.get('cost_basis', 0) for p in portfolio.values())

    worker = _Worker(portfolio, symbol_map, data_dir)
    worker.start()
    page, range_key = 0, '1'
    n_pages = 1 + len(codes)
    search_mode, search_buf = False, ''
    y_mode = 'linear'   # 'linear' $ · 'log' log-$ · 'pct' % from start

    def render():
        st, _ = worker.snapshot()
        return _render_view(console, portfolio, codes, symbol_map, st,
                            range_key, page, n_pages, total_cost,
                            search_mode, search_buf, y_mode)

    try:
        with _KeyReader() as keys, Live(render(), console=console,
                                        screen=True, refresh_per_second=4) as live:
            while True:
                k = keys.read()
                if search_mode:
                    if k == 'ENTER':
                        if search_buf.strip():
                            worker.set_compare(search_buf.strip().upper())
                        search_mode, search_buf = False, ''
                    elif k == 'ESC':
                        search_mode, search_buf = False, ''
                    elif k == 'BS':
                        search_buf = search_buf[:-1]
                    elif len(k) == 1 and (k.isalnum() or k in '.^-='):
                        search_buf += k.upper()
                elif k:
                    kl = k.lower()
                    if kl == 'q':
                        break
                    elif k in RANGES:
                        range_key = k
                        worker.set_range(k)
                    elif kl == 'n':
                        page = (page + 1) % n_pages
                    elif kl == 'p':
                        page = (page - 1) % n_pages
                    elif kl == '/' and page > 0:
                        search_mode, search_buf = True, ''
                    elif kl == 'c':
                        worker.clear_compare()
                    elif kl == 'l':          # toggle linear $ ↔ log $
                        y_mode = 'linear' if y_mode == 'log' else 'log'
                    elif k == '%':           # toggle % return ↔ $
                        y_mode = 'linear' if y_mode == 'pct' else 'pct'
                live.update(render())
                threading.Event().wait(0.15)
    except KeyboardInterrupt:
        pass
    finally:
        worker.stop_event.set()
        worker.join(timeout=3)
