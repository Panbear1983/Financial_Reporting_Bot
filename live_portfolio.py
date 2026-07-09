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

QUOTE_TTL_OPEN   = 150   # table refresh cadence during market hours
QUOTE_TTL_CLOSED = 600

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


def fetch_live_quotes(codes, symbol_map):
    """{code: {price, prev_close, ...}} for the live board.

    Rebuilt 2026-07-08: derives quotes from BATCHED yf.download instead of firing
    one raw urllib get_yfinance_data() per stock across 8 threads — that burst of
    parallel Yahoo v8 chart calls hard-429s (and SSL-failed on the host CA store),
    which left the live board empty. yf.download uses a browser-impersonating
    session that isn't throttled (same path the candlestick graphs already use).

    prev_close = prior day's close; price = freshest intraday 1-minute close when
    available, else the latest daily close. Falls back to the raw per-stock path
    only for symbols the batch download couldn't resolve.
    """
    syms = [symbol_map[c] for c in codes]
    daily = fetch_history(syms, period='5d', interval='1d')
    try:
        intra = fetch_history(syms, period='1d', interval='1m')
    except Exception:
        intra = {}

    quotes = {}
    missing = []
    for code in codes:
        sym = symbol_map[code]
        d = daily.get(sym)
        closes = d['Close'].dropna() if d is not None and 'Close' in getattr(d, 'columns', []) else None
        if closes is None or closes.empty:
            missing.append(code)
            continue
        prev_close = float(closes.iloc[-2]) if len(closes) >= 2 else float(closes.iloc[-1])
        price = float(closes.iloc[-1])
        iv = intra.get(sym)
        if iv is not None and 'Close' in getattr(iv, 'columns', []):
            ic = iv['Close'].dropna()
            if not ic.empty:
                price = float(ic.iloc[-1])   # freshest intraday price during market hours
        quotes[code] = {
            'name': code, 'price': price, 'prev_close': prev_close,
            'today_open': price, 'change': price - prev_close, 'intraday_change': 0.0,
        }

    # Last-ditch per-stock fallback (hardened get_yfinance_data) for anything the
    # batch missed — thin/newly-listed symbols yf.download occasionally drops.
    if missing:
        def one(code):
            return code, get_yfinance_data(symbol_map[code])
        with ThreadPoolExecutor(max_workers=4) as ex:
            for code, q in ex.map(one, missing):
                if q:
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

def candle_renderable(ohlc_df, title, width, height, hline=None):
    """plotext candlestick → ANSI → rich Text (embeddable in Live/Group)."""
    if ohlc_df is None or ohlc_df.empty:
        return Text('（無資料 / no data at this interval）', style='dim')
    if height < 8 or width < 40:
        return Text('Terminal too small for chart — resize to view.', style='yellow')

    intraday = len(ohlc_df) > 1 and (ohlc_df.index[1] - ohlc_df.index[0]) < pd.Timedelta(days=1)
    form, fmt = ('d/m H:M', '%d/%m %H:%M') if intraday else ('d/m/Y', '%d/%m/%Y')

    plt.clear_figure()
    plt.theme('clear')
    plt.date_form(form)
    plt.plotsize(width, height)
    dates = [ts.strftime(fmt) for ts in ohlc_df.index]
    plt.candlestick(dates, {c: ohlc_df[c].tolist() for c in _OHLC})
    if hline:
        plt.hline(hline, 'gray')
    plt.title(title)
    return Text.from_ansi(plt.build())


def _fmt_signed(v, pct=False):
    s = f'{v:+,.2f}%' if pct else f'{v:+,.0f}'
    # TW convention: red = up, green = down
    color = 'red' if v > 0 else ('green' if v < 0 else 'white')
    return f'[{color}]{s}[/{color}]'


def build_live_table(portfolio, quotes):
    t = Table(title='Portfolio Holdings — Live', box=box.ROUNDED)
    for col, kw in [('#', dict(style='dim', width=3, justify='right')),
                    ('Code', dict(style='bold cyan', width=8)),
                    ('Name', dict(width=14)),
                    ('Shares', dict(justify='right')),
                    ('Price', dict(justify='right')),
                    ('Chg%', dict(justify='right')),
                    ('Mkt Value', dict(justify='right')),
                    ('P/L', dict(justify='right')),
                    ('P/L%', dict(justify='right'))]:
        t.add_column(col, **kw)

    tot_val = tot_cost = 0.0
    for i, (code, pos) in enumerate(portfolio.items(), 1):
        sh, cost = pos.get('shares', 0), pos.get('cost_basis', 0)
        q = quotes.get(code)
        tot_cost += cost
        if q:
            price = q['price']
            chg_pct = (price - q['prev_close']) / q['prev_close'] * 100 if q['prev_close'] else 0
            val = price * sh
            pnl = val - cost
            pnl_pct = pnl / cost * 100 if cost else 0
            tot_val += val
            t.add_row(str(i), code, pos.get('name', ''), f'{sh:,}',
                      f'{price:,.2f}', _fmt_signed(chg_pct, pct=True),
                      f'{val:,.0f}', _fmt_signed(pnl), _fmt_signed(pnl_pct, pct=True))
        else:
            t.add_row(str(i), code, pos.get('name', ''), f'{sh:,}',
                      '[dim]…[/dim]', '', '', '', '')

    if tot_val:
        pnl = tot_val - tot_cost
        t.add_section()
        t.add_row('', '', '[bold]Total[/bold]', '', '',
                  '', f'[bold]{tot_val:,.0f}[/bold]', _fmt_signed(pnl),
                  _fmt_signed(pnl / tot_cost * 100 if tot_cost else 0, pct=True))
    return t


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
        self.state = {'quotes': {}, 'quotes_at': None,
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

        if self._stale(st['quotes_at'], quote_ttl):
            quotes = fetch_live_quotes(list(self.portfolio), self.symbol_map)
            with self.lock:
                if quotes:
                    self.state['quotes'] = quotes
                    self.state['quotes_at'] = datetime.datetime.now()
                    self.state['error'] = ''

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
    at = st['quotes_at'].strftime('%H:%M:%S') if st['quotes_at'] else '—'
    s = f'market {mkt} · updated {at} · range [bold]{label}[/bold] · page {page+1}/{n_pages}'
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


def _render_view(console, portfolio, codes, symbol_map, st, range_key, page,
                 n_pages, total_cost, search_mode, search_buf):
    entry = st['hist'].get(range_key)
    label = RANGES[range_key][0]
    w = console.size.width - 4
    H = console.size.height
    parts = [_hero_line(st, label, page, n_pages)]

    if page == 0:
        table = build_live_table(portfolio, st['quotes'])
        chart_h = max(H - len(portfolio) - 16, 8)
        parts.append(candle_renderable(entry['total'], f'Total Portfolio — {label}',
                                       w, chart_h, hline=total_cost)
                     if entry is not None else Text('fetching…', style='dim'))
        parts.insert(1, table)
        if entry and entry['n_flat']:
            parts.append(Text(f"⚠ {entry['n_flat']} holding(s) shown at last close "
                              f"(no bars at this interval)", style='yellow'))
        keybar = '[dim]\\[1-7] range  \\[n/p ←/→] page  \\[q] back[/dim]'
    else:
        code = codes[page - 1]
        pos = portfolio[code]
        q = st['quotes'].get(code)
        head = f"{code} {pos.get('name','')} — {pos.get('shares',0):,} shares"
        if q:
            head += f" · last {q['price']:,.2f}"
        parts.append(Text(head, style='bold cyan'))

        # Top half: the holding's graph; bottom half: the compared ticker's.
        h_each = max((H - 7) // 2, 8)
        sub = entry['stocks'].get(symbol_map[code]) if entry else None
        parts.append(candle_renderable(
            sub[_OHLC] if sub is not None and not sub.empty else None,
            f'{code} — {label}', w, h_each)
            if entry is not None else Text('fetching…', style='dim'))

        parts.append(_search_bar(st, search_mode, search_buf, w))

        cmp_ = st['compare']
        if cmp_ and not cmp_['error']:
            centry = cmp_['hist'].get(range_key)
            if centry and centry['df'] is not None and not centry['df'].empty:
                title = cmp_['input'] + (f' {cmp_["name"]}' if cmp_.get('name') else '')
                parts.append(candle_renderable(centry['df'][_OHLC],
                                               f'{title} — {label}', w, h_each))
            else:
                parts.append(Text('fetching…', style='dim'))
        keybar = ('[dim]\\[1-7] range  \\[n/p ←/→] page  \\[/] compare  '
                  '\\[c] clear  \\[q] back[/dim]')

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

    def render():
        st, _ = worker.snapshot()
        return _render_view(console, portfolio, codes, symbol_map, st,
                            range_key, page, n_pages, total_cost,
                            search_mode, search_buf)

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
                live.update(render())
                threading.Event().wait(0.15)
    except KeyboardInterrupt:
        pass
    finally:
        worker.stop_event.set()
        worker.join(timeout=3)
