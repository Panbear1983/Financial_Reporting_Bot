"""
TWSE Daily Report — two modes:
  morning  (01:30 UTC / 09:30 Taiwan): yfinance live prices, no full TWSE scan
  closing  (08:00 UTC / 16:00 Taiwan): TWSE official data once published

Data source rules — ZERO figure hallucination:
  All numbers come from scrapers only.
  AI (OpenRouter) is called only for explanatory text (原因, 展望, 研究, 推薦).
"""

import urllib.request
import json
import csv
import os
import re
import sys
import time
import datetime
import xml.etree.ElementTree as ET
import requests
import yfinance as yf
import numpy as np
import pandas as pd
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from market_data_fetcher import TWSDDataSource
from custom_stock_lookup import get_yfinance_data

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR   = os.getenv('OPENCLAW_DATA_DIR', os.path.join(SCRIPT_DIR, 'data'))

def _config_path(filename):
    """Prefer data-dir (persistent volume) over script-dir (image layer)."""
    data_path = os.path.join(DATA_DIR, filename)
    if os.path.exists(data_path):
        return data_path
    return os.path.join(SCRIPT_DIR, filename)


def load_tracked_stocks():
    path = _config_path('tracked_stocks.json')
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


def _resolve_section_order(cfg_order, default_order):
    """Return a safe emit order: configured keys (known, deduped) first, then any
    missing defaults appended. A bad/partial order can never drop or duplicate a
    section — the report stays complete even if bot_config is hand-edited."""
    final = []
    for k in (cfg_order or []):
        if k in default_order and k not in final:
            final.append(k)
    for k in default_order:
        if k not in final:
            final.append(k)
    return final


def _stock_note(cfg, code):
    """Optional per-stock annotation (bot_config['notes'][code]), shown under a holding."""
    note = (cfg.get('notes') or {}).get(str(code))
    return f"\n  📝 {note.strip()}" if note and str(note).strip() else ""


def _style_text(cfg, key):
    """Optional custom header/footer text (bot_config['report_style'][key])."""
    return ((cfg.get('report_style') or {}).get(key, '') or '').strip()


def format_portfolio_line(shares, cost_basis, scraped_close):
    # 現值 = 股數 × TWSE/TPEX 收盤價 (scraped)
    # 預估損益 = 現值 - 付出成本 (formula)
    current_value = shares * scraped_close
    total_gain    = current_value - cost_basis
    total_pct     = total_gain / cost_basis * 100 if cost_basis else 0.0
    t_sign = '+' if total_gain >= 0 else ''
    return (
        f"    💰 股倉 {shares:,}股, 付出成本 {int(cost_basis):,}元"
        f" → 現值：{int(current_value):,}元 ({shares:,} × {scraped_close:.2f}元)"
        f" → 預估損益：{t_sign}{int(total_gain):,}元 ({total_pct:+.1f}%)"
    )


# ---------------------------------------------------------------------------
# Sandbox helpers
# ---------------------------------------------------------------------------

def _line_safe_chunks(text, limit=4000):
    """Split text into chunks at line boundaries near limit."""
    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        cut = text.rfind('\n', 0, limit)
        if cut == -1:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip('\n')
    return chunks


def _apply_markdown_ansi(text):
    """Convert Telegram legacy Markdown to ANSI escape codes."""
    text = re.sub(r'\*\*(.+?)\*\*', '\033[1m\\1\033[0m', text)
    text = re.sub(r'_(.+?)_',       '\033[3m\\1\033[0m', text)
    text = re.sub(r'`(.+?)`',       '\033[7m\\1\033[0m', text)
    return text


def _render_sandbox(text):
    """Render report to terminal + file, simulating Telegram delivery."""
    chunks     = _line_safe_chunks(text)
    total      = len(chunks)
    timestamp  = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    data_dir   = os.getenv('OPENCLAW_DATA_DIR', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data'))
    os.makedirs(data_dir, exist_ok=True)
    fname      = f"sandbox_preview_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    fpath      = os.path.join(data_dir, fname)

    file_lines = []
    bar        = '━' * 25
    is_tty     = sys.stdout.isatty()

    for i, chunk in enumerate(chunks, 1):
        header = f"{bar}  SANDBOX  Message {i}/{total}  {bar}"
        footer = f"{bar}  END {i}/{total}  — {timestamp}  {bar}"

        if is_tty:
            # Interactive terminal: print with ANSI formatting
            print(f"\n{header}", flush=True)
            print(_apply_markdown_ansi(chunk), flush=True)
            print(f"{footer}\n", flush=True)

        # Always write plain text to file (no ANSI codes)
        file_lines.append(header)
        file_lines.append(chunk)
        file_lines.append(footer)
        file_lines.append('')

    with open(fpath, 'w', encoding='utf-8') as f:
        f.write('\n'.join(file_lines))

    # Always show file path prominently regardless of TTY
    sep = '━' * 55
    print(f"\n{sep}", flush=True)
    print(f"  [SANDBOX] Full report saved to:", flush=True)
    print(f"  {fpath}", flush=True)
    print(f"  To view: cat \"{fpath}\"", flush=True)
    print(f"{sep}\n", flush=True)


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def send_telegram_report(text):
    if os.getenv('SANDBOX_MODE', '').lower() == 'true':
        _render_sandbox(text)
        return
    token   = os.getenv('TELEGRAM_BOT_TOKEN')
    chat_id = os.getenv('TELEGRAM_CHAT_ID')
    if not token or not chat_id:
        print(f"[{_now()}] Telegram not configured, skipping.")
        return
    url    = f"https://api.telegram.org/bot{token}/sendMessage"
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
    for chunk in chunks:
        try:
            resp = requests.post(
                url,
                json={"chat_id": chat_id, "text": chunk, "parse_mode": "Markdown"},
                timeout=15
            )
            if resp.ok:
                print(f"[{_now()}] Telegram notification sent.")
            else:
                print(f"[{_now()}] Telegram send failed: {resp.text}")
        except Exception as e:
            print(f"[{_now()}] Telegram error: {e}")


def _send_telegram(token, chat_id, text):
    """Send one report to one Telegram destination (chunked at line boundaries,
    ≤4000 chars). Returns bool."""
    if not token or not chat_id:
        print(f"[{_now()}] Telegram destination missing token/chat_id, skipping.")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    ok_all = True
    # Split on line boundaries (not raw 4000-char slices) so a Markdown entity
    # like **bold** is never cut in half across a chunk boundary — that split was
    # what produced Telegram's "can't parse entities" 400 and dropped a chunk.
    for chunk in _line_safe_chunks(text, limit=4000):
        # Two payloads: formatted first, then a plain-text fallback. If Markdown
        # parsing still fails on a chunk, resend it without parse_mode so the
        # content always reaches the channel instead of being silently dropped.
        payloads = (
            {"chat_id": chat_id, "text": chunk, "parse_mode": "Markdown"},
            {"chat_id": chat_id, "text": chunk},
        )
        payload_i = 0
        # Retry transient failures (e.g. brief "Network is unreachable" blips) so a
        # single dropped chunk doesn't silently deliver half a report.
        sent = False
        for attempt, delay in ((1, 2), (2, 5), (3, 0)):
            try:
                resp = requests.post(url, json=payloads[payload_i], timeout=15)
                if resp.ok:
                    print(f"[{_now()}] Telegram sent → {chat_id}.")
                    sent = True
                    break
                print(f"[{_now()}] Telegram failed ({chat_id}, attempt {attempt}): {resp.text}")
                if (resp.status_code == 400 and payload_i == 0
                        and "parse entities" in resp.text):
                    payload_i = 1   # drop Markdown, resend same chunk as plain text
                    continue        # immediate retry, no backoff
                if resp.status_code < 500:
                    break  # other 4xx won't heal on retry (bad chat, rate-limit body)
            except Exception as e:
                print(f"[{_now()}] Telegram error ({chat_id}, attempt {attempt}): {e}")
            if delay:
                time.sleep(delay)
        ok_all = ok_all and sent
    return ok_all


def deliver_report(text, cfg=None):
    """Deliver the report to every enabled channel in cfg['delivery']['channels'].

    Backward compatible: when no channels are configured, falls back to the single
    env-based Telegram destination (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID). Telegram
    channels can each name their own token env var + chat_id, so multiple bots / group
    chats are supported. LINE / WhatsApp channels are config placeholders meant to be
    routed through OpenClaw — OpenClaw has no messaging gateway yet, so they are logged
    and skipped (never silently faked as delivered)."""
    if os.getenv('SANDBOX_MODE', '').lower() == 'true':
        _render_sandbox(text)
        return
    if cfg is None:
        cfg = load_bot_config()
    channels = [c for c in (cfg.get('delivery', {}) or {}).get('channels', []) if c.get('enabled', True)]
    if not channels:
        _send_telegram(os.getenv('TELEGRAM_BOT_TOKEN'), os.getenv('TELEGRAM_CHAT_ID'), text)
        return
    for ch in channels:
        ctype = (ch.get('type') or 'telegram').lower()
        name  = ch.get('name', ctype)
        if ctype == 'telegram':
            token   = os.getenv(ch.get('token_env', 'TELEGRAM_BOT_TOKEN')) or os.getenv('TELEGRAM_BOT_TOKEN')
            chat_id = ch.get('chat_id') or os.getenv('TELEGRAM_CHAT_ID')
            _send_telegram(token, chat_id, text)
        else:
            print(f"[{_now()}] Channel '{name}' (type={ctype}) configured but OpenClaw has no "
                  f"{ctype} gateway yet — skipped, not sent.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now():
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def format_zhang(volume):
    """Convert raw share volume (int or comma-string) to 張 string."""
    try:
        vol = int(str(volume).replace(',', ''))
        return f"{vol // 1000:,}"
    except:
        return str(volume)


def get_market_sentiment(pct):
    if pct > 1:
        return '🟢 樂觀'
    elif pct < -1:
        return '🔴 謹慎'
    return '🟡 觀望'


# ---------------------------------------------------------------------------
# Scrapers — numbers only, no AI
# ---------------------------------------------------------------------------

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


def fetch_global_indices(indices=None):
    """Returns list of formatted strings — scraped from Yahoo Finance."""
    if not indices:
        indices = [
            ('S&P 500',    '^GSPC'),
            ('Nasdaq',     '^IXIC'),
            ('費城半導體', '^SOX'),
            ('日經225',    '^N225'),
        ]
    lines = []
    for name, sym in indices:
        d = get_yfinance_data(sym)
        if d:
            pct = d['change'] / d['prev_close'] * 100
            lines.append(f"• {name}：{d['price']:,.2f} ({pct:+.2f}%)")
        else:
            lines.append(f"• {name}：資料暫時無法取得")
    return lines


def _ticker_history(code, period='20d'):
    """Fetch yfinance history, trying .TW (TWSE) then .TWO (TPEX/上櫃)."""
    for suffix in ('.TW', '.TWO'):
        hist = yf.Ticker(f"{code}{suffix}").history(period=period)
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
    """Live price data for a single Taiwan stock. Tries .TW then .TWO."""
    for suffix in ('.TW', '.TWO'):
        d = get_yfinance_data(f"{code}{suffix}")
        if d:
            return d
    return None


def fetch_twse_all():
    """Full TWSE scan. Returns list of stock dicts or None."""
    try:
        result = TWSDDataSource().fetch_data()
        data   = result['data']
        date   = data[0].get('Date', '') if data else ''
        print(f"[{_now()}] TWSE data date: {date} ({len(data)} stocks)")
        return data
    except Exception as e:
        print(f"[{_now()}] TWSE fetch failed: {e}")
        return None


def fetch_tpex_all():
    """TPEX (上櫃) daily data, normalized to match TWSE field names."""
    try:
        resp = requests.get(
            'https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes',
            headers={'User-Agent': 'Mozilla/5.0'},
            timeout=15,
        )
        raw = resp.json()
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


RETURN_FLAG_PCT = 500.0  # |return| beyond this gets a ⚠ verify-flag


def _returns_line(code, rets=None):
    """Render the watchlist 績效 strip (1月/3月/1年). Empty string when no data.
    Returns beyond ±RETURN_FLAG_PCT are marked ⚠ — usually a real low-base 興櫃 move,
    but flagged so a sparse/erroneous early bar isn't taken at face value.
    `rets` may be a precomputed fetch_period_returns() dict to avoid a duplicate call."""
    rets = rets if rets is not None else fetch_period_returns(code)
    if not rets:
        return ""
    parts = []
    for label in ('1月', '3月', '1年'):
        v = rets.get(label)
        if v is None:
            parts.append(f"{label} N/A")
        elif abs(v) > RETURN_FLAG_PCT:
            parts.append(f"{label} {v:+.0f}%⚠")
        else:
            parts.append(f"{label} {v:+.1f}%")
    return "\n  績效：" + " · ".join(parts)


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


def _src_tag(row):
    """Inline marker for closing lines sourced via the yfinance fallback rather than
    TWSE official data — signals the reader the volume figure is not an official 量."""
    return "（yfinance 報價）" if row.get('_fallback') else ""


# ---------------------------------------------------------------------------
# OpenRouter — text only, numbers always provided via prompt context
# ---------------------------------------------------------------------------

def call_openrouter(prompt, max_tokens=200, model='anthropic/claude-haiku-3-5', api_key=None,
                    system=None, temperature=None):
    api_key = api_key or os.getenv('OPENROUTER_API_KEY')
    if not api_key:
        return ''
    messages = []
    if system:
        messages.append({'role': 'system', 'content': system})
    messages.append({'role': 'user', 'content': prompt})
    payload = {'model': model, 'messages': messages, 'max_tokens': max_tokens}
    if temperature is not None:
        payload['temperature'] = temperature
    try:
        resp = requests.post(
            'https://openrouter.ai/api/v1/chat/completions',
            headers={
                'Authorization': f'Bearer {api_key}',
                'Content-Type': 'application/json',
            },
            json=payload,
            timeout=20,
        )
        if resp.ok:
            return resp.json()['choices'][0]['message']['content'].strip()
    except Exception as e:
        print(f"[{_now()}] OpenRouter error: {e}")
    return ''


def ai_report_summary(report_text, cfg, digest='', history=''):
    """One AI pass over the compiled report → a 報告總結 analyst conclusion.

    `report_text` is expected to be NOTE-SCRUBBED by the caller (no 📝 lines): the
    static hand-written notes are a fixed thesis and must not contaminate analysis of
    a fluid market. `digest` is an optional NOTE-FREE structured block (today's
    objective signals); `history` is an optional NOTE-FREE recent-trend block (from the
    data pool) so the model can reason over trajectories/streaks, not just a snapshot.

    Pluggable engine: ai.summary_model (falls back to ai.model), ai.summary_key_env
    (env var holding the OpenRouter key), ai.summary_temperature (default 0.3).
    Returns '' on any failure so a bad/empty AI call can never blank the report."""
    ai_cfg      = cfg.get('ai', {})
    model       = ai_cfg.get('summary_model') or ai_cfg.get('model', 'anthropic/claude-haiku-3-5')
    max_tokens  = int(ai_cfg.get('max_tokens_summary', 400) or 400)
    key_env     = ai_cfg.get('summary_key_env', 'OPENROUTER_API_KEY')
    api_key     = os.getenv(key_env) or os.getenv('OPENROUTER_API_KEY')
    temperature = ai_cfg.get('summary_temperature', 0.3)
    system = (
        "你是一位嚴謹的台股分析師。只根據所提供的『當日客觀數據』、『近期走勢』與報告內容進行分析，"
        "不得臆測使用者的選股理由或動機（報告不含使用者備註）。對觀察清單，"
        "請依當日價格動作、技術指標（RSI／量比／乖離）與 1月／3月／1年 報酬給出判斷，"
        "並可參考近期走勢指出趨勢與連續性（如連續數日漲跌、與昨日比較），但結論仍以當日數據為主。"
    )
    parts = []
    if digest:
        parts.append(f"=== 當日客觀數據 ===\n{digest}\n=== 數據結束 ===")
    if history:
        parts.append(f"=== 近期走勢（供趨勢／連續性判斷） ===\n{history}\n=== 走勢結束 ===")
    parts.append(
        "以下為今日完整台股報告（供全球市場與新聞背景參考）：\n"
        f"=== 報告內容 ===\n{report_text}\n=== 報告結束 ==="
    )
    parts.append(
        "請撰寫「報告總結」（繁體中文，精簡條列）：\n"
        "1. 今日重點與持倉整體狀況（可對比近期走勢指出趨勢）；\n"
        "2. 【觀察清單】逐檔給出 值得關注／觀望／回避 的判斷，並以當日數據佐證（可引用關鍵數字、點出連續性）；\n"
        "3. 需留意的風險。"
    )
    prompt = "\n\n".join(parts)
    try:
        return call_openrouter(prompt, max_tokens=max_tokens, model=model, api_key=api_key,
                               system=system, temperature=temperature)
    except Exception as e:
        print(f"[{_now()}] Report-summary error: {e}")
        return ''


def fetch_brave_news(query='台股 今日 財經', count=5):
    """Fetch latest financial news headlines via Brave Search API."""
    api_key = os.getenv('BRAVE_API_KEY')
    if not api_key:
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


def ai_stock_reason(name, code, open_p, close_p, change, pct, rsi, vol_ratio, zhang, taiex_pct,
                     divergence_threshold=1.5, rsi_overbought=80, rsi_oversold=30,
                     max_tokens=100, model='anthropic/claude-haiku-3-5'):
    """One-sentence explanation. Numbers injected from scrapers."""
    direction = '漲' if change >= 0 else '跌'
    diverge = abs(pct - taiex_pct) > divergence_threshold
    diverge_hint = (
        f"（注意：個股走勢與大盤{'明顯背離' if diverge else '相符'}，請從個股角度分析）"
        if diverge else ""
    )
    rsi_hint = ''
    try:
        rsi_val = float(rsi)
        if rsi_val >= rsi_overbought:
            rsi_hint = '（RSI超買區）'
        elif rsi_val <= rsi_oversold:
            rsi_hint = '（RSI超賣區）'
    except (ValueError, TypeError):
        pass
    prompt = (
        f"以下是今日台股個股數據（數字均來自官方數據，請勿更改）：\n"
        f"股票：{name}（{code}）\n"
        f"開盤：{open_p}元，收盤：{close_p}元，{direction}{abs(change):.1f}元（{pct:+.2f}%）\n"
        f"RSI：{rsi}{rsi_hint}，量比：{vol_ratio}，成交量：{zhang}張\n"
        f"今日加權指數：{taiex_pct:+.2f}%{diverge_hint}\n"
        f"請用繁體中文寫一句話（30字以內）解釋今日股價可能的原因。"
        f"避免使用'市場情緒回暖'、'投資者信心增強'等通用詞，聚焦該股的具體因素。"
        f"只輸出原因句，不要加數字、不要加股票名稱。"
    )
    return call_openrouter(prompt, max_tokens=max_tokens, model=model)


def ai_stock_reasons_batch(contexts, taiex_pct, cfg):
    """ONE batched AI call for every stock's one-line 原因 → {code: reason}.

    Replaces ~N separate ai_stock_reason() calls with a single cross-stock-aware
    request (cheaper, faster, and it can compare names). `contexts` is a list of
    per-stock dicts (code/name/change/pct/rsi/vol_ratio/zhang and optional 'val'
    signals). Returns {} on any failure, so callers simply omit 原因 — never blanks."""
    if not contexts:
        return {}
    ai_cfg = cfg.get('ai', {})
    model  = ai_cfg.get('model', 'anthropic/claude-haiku-3-5')
    max_tokens = min(45 * len(contexts) + 120, 2000)
    rows = []
    for c in contexts:
        direction = '漲' if c.get('change', 0) >= 0 else '跌'
        val = c.get('val') or {}
        extra = []
        if val.get('pe') is not None:
            extra.append(f"PE{val['pe']}")
        if val.get('yield') is not None:
            extra.append(f"殖利率{val['yield']}%")
        if val.get('margin_chg') is not None:
            extra.append(f"融資變化{val['margin_chg']:+}張")
        rows.append(
            f"{c['code']} {c['name']}：{direction}{abs(c.get('change', 0)):.1f}元"
            f"({c.get('pct', 0):+.2f}%) RSI{c.get('rsi')} 量比{c.get('vol_ratio')} "
            f"量{c.get('zhang', 'N/A')}張" + ("　" + " ".join(extra) if extra else "")
        )
    prompt = (
        f"今日加權指數 {taiex_pct:+.2f}%。以下為多檔台股今日數據，請為每一檔用繁體中文寫一句"
        f"（30字內）解釋今日漲跌的具體原因，聚焦個股因素，避免『市場情緒』等通用詞。\n"
        f"嚴格逐行輸出，每行格式「代號: 原因」，僅此格式，不要標題或多餘文字。\n\n"
        + "\n".join(rows)
    )
    text = call_openrouter(prompt, max_tokens=max_tokens, model=model,
                           temperature=ai_cfg.get('reason_temperature'))
    out = {}
    for ln in (text or '').splitlines():
        ln = ln.strip().lstrip('-*•　 ').strip()
        sep = '：' if '：' in ln else (':' if ':' in ln else '')
        if not sep:
            continue
        left, _, reason = ln.partition(sep)
        m = re.search(r'(\d{4,6}[A-Z]?)', left)      # pull the ticker from '代號' or '名稱(代號)'
        reason = reason.strip()
        if m and reason:
            out[m.group(1)] = reason
    return out


def ai_morning_outlook(taiex_pct, global_lines, stock_lines,
                        max_tokens=200, model='anthropic/claude-haiku-3-5'):
    """Short morning market outlook. No figures generated by AI."""
    prompt = (
        f"今日台股加權指數昨收漲跌：{taiex_pct:+.2f}%\n"
        f"全球市場：\n" + "\n".join(global_lines) + "\n\n"
        f"追蹤個股現況：\n" + "\n".join(stock_lines) + "\n\n"
        f"請用繁體中文寫3句話的開盤展望分析，不要捏造數字，只描述市場方向與注意事項。"
    )
    return call_openrouter(prompt, max_tokens=max_tokens, model=model)


def ai_closing_commentary(taiex_pct, global_lines, top_vol, top_losers,
                           max_tokens=350, model='anthropic/claude-haiku-3-5'):
    """Market commentary for closing report. Returns (研究段, 推薦段)."""
    vol_str  = '、'.join(f"{s['Name']}({s['Code']})" for s in top_vol[:3])
    loss_str = '、'.join(f"{s['Name']}({s['Code']})" for s in top_losers[:3])
    prompt = (
        f"今日台股加權指數漲跌：{taiex_pct:+.2f}%\n"
        f"全球市場：\n" + "\n".join(global_lines) + "\n"
        f"成交量最大：{vol_str}\n"
        f"跌幅最深：{loss_str}\n\n"
        f"請用繁體中文輸出兩段，段落之間空一行：\n"
        f"第一段以「💡 相關產業研究：」開頭，3-4句分析今日市場趨勢與產業輪動，不要捏造數字。\n"
        f"第二段以「🚨 其他熱門產業推薦：」開頭，2-3句推薦值得關注的產業方向，不要捏造數字。"
    )
    text = call_openrouter(prompt, max_tokens=max_tokens, model=model)
    parts = text.split('\n\n', 1) if text else ['', '']
    return parts[0], parts[1] if len(parts) > 1 else ''


# ---------------------------------------------------------------------------
# Morning report (Mode A) — yfinance only
# ---------------------------------------------------------------------------

def generate_morning_report():
    tracked       = load_tracked_stocks()   # {"2330": "台積電", ...}
    tracked_notes = load_tracked_notes()
    portfolio = load_portfolio()        # {"2330": {"shares": N, "avg_cost": X}, ...}
    date_str  = datetime.datetime.now().strftime('%Y-%m-%d')
    time_str  = datetime.datetime.now().strftime('%H:%M')
    output_dir = os.getenv('OPENCLAW_DATA_DIR', '/app/data')

    cfg            = load_bot_config()
    technicals_cfg = cfg.get('technicals', {})
    ai_cfg         = cfg.get('ai', {})
    sections       = cfg.get('sections', {}).get('morning', {})
    period_days    = technicals_cfg.get('period_days', 20)

    print(f"[{_now()}] [MORNING] Fetching TAIEX...")
    taiex = fetch_taiex()
    taiex_pct = taiex['pct'] if taiex else 0.0

    print(f"[{_now()}] [MORNING] Fetching global indices...")
    global_lines = fetch_global_indices(cfg.get('global_indices'))

    print(f"[{_now()}] [MORNING] Fetching holdings (yfinance live)...")
    holding_sections = []
    holdings_data    = []
    stock_summary    = []
    pf_daily_total   = 0.0
    pf_gain_total    = 0.0
    pf_cost_total    = 0.0

    _valuation = fetch_valuation()
    _margin    = fetch_margin()

    # Pass 1 — gather holdings (data only; 展望 comes from one batched call below)
    hold_ctx = []
    for code, pos in portfolio.items():
        name = pos.get('name', code)
        d = fetch_yfinance_stock(code)
        rsi, vol_ratio = fetch_stock_technicals(code, opening_mode=True, period_days=period_days)
        if d:
            price, prev_cls, change = d['price'], d['prev_close'], d['change']
            pct = change / prev_cls * 100 if prev_cls else 0.0
            hold_ctx.append({
                'code': code, 'name': name, 'pos': pos,
                'price': price, 'prev_cls': prev_cls, 'change': change, 'pct': pct,
                'rsi': rsi, 'vol_ratio': vol_ratio, 'zhang': 'N/A',
            })
        else:
            holding_sections.append(f"• **{name} ({code})**：資料暫時無法取得")
            holding_sections.append("")

    print(f"[{_now()}] [MORNING] Fetching watchlist (yfinance live)...")
    watch_sections = []
    watch_data     = []
    watch_ctx = []
    for code, name in tracked.items():
        if code in portfolio:
            continue
        d = fetch_yfinance_stock(code)
        rsi, vol_ratio = fetch_stock_technicals(code, opening_mode=True, period_days=period_days)
        if d:
            price, prev_cls, change = d['price'], d['prev_close'], d['change']
            pct = change / prev_cls * 100 if prev_cls else 0.0
            watch_ctx.append({
                'code': code, 'name': name,
                'price': price, 'prev_cls': prev_cls, 'change': change, 'pct': pct,
                'rsi': rsi, 'vol_ratio': vol_ratio, 'zhang': 'N/A',
                'rets': fetch_period_returns(code), 'note': tracked_notes.get(code, ''),
            })
        else:
            watch_sections.append(f"• **{name} ({code})**：資料暫時無法取得")
            watch_sections.append("")

    # One batched AI call for every 展望 (holdings + watchlist) — cross-stock aware
    _attach_signals(hold_ctx + watch_ctx, _valuation, _margin)
    print(f"[{_now()}] [MORNING] AI 展望 (batched, {len(hold_ctx) + len(watch_ctx)} stocks)...")
    _reasons = ai_stock_reasons_batch(hold_ctx + watch_ctx, taiex_pct, cfg)

    # Pass 2 — render holdings
    for c in hold_ctx:
        code, name = c['code'], c['name']
        reason = _reasons.get(code, '')
        price, prev_cls, change, pct = c['price'], c['prev_cls'], c['change'], c['pct']
        direction = '漲' if change >= 0 else '跌'
        line = (
            f"• **{name} ({code})**：目前 {price:,.1f}元"
            f" [昨收 {prev_cls:,.1f} | {direction}{abs(change):.1f}元 ({pct:+.2f}%)]"
        )
        try:
            pf_line = format_portfolio_line(c['pos']['shares'], c['pos']['cost_basis'], price)
            line += '\n' + pf_line
            pf_daily_total += c['pos']['shares'] * change
            pf_gain_total  += c['pos']['shares'] * price - c['pos']['cost_basis']
            pf_cost_total  += c['pos']['cost_basis']
        except (ValueError, TypeError, KeyError):
            pass
        if reason:
            line += f"\n  展望：{reason}"
        line += _stock_note(cfg, code)
        holding_sections.append(line)
        holding_sections.append("")
        holdings_data.append(_stock_row(
            code, name, price, change, pct, c['rsi'], c['vol_ratio'], reason,
            (cfg.get('notes') or {}).get(str(code), ''), valuation=c.get('val')))
        stock_summary.append(f"{name}({code}) {direction}{abs(pct):.1f}%")

    # Pass 2 — render watchlist
    for c in watch_ctx:
        code, name = c['code'], c['name']
        reason = _reasons.get(code, '')
        price, prev_cls, change, pct = c['price'], c['prev_cls'], c['change'], c['pct']
        direction = '漲' if change >= 0 else '跌'
        line = (
            f"• **{name} ({code})**：目前 {price:,.1f}元"
            f" [昨收 {prev_cls:,.1f} | {direction}{abs(change):.1f}元 ({pct:+.2f}%)]"
        )
        line += _returns_line(code, c['rets'])
        if reason:
            line += f"\n  展望：{reason}"
        if c['note']:
            line += f"\n  📝 備註：{c['note']}"
        watch_sections.append(line)
        watch_sections.append("")
        watch_data.append(_stock_row(
            code, name, price, change, pct, c['rsi'], c['vol_ratio'], reason,
            c['note'], returns=c['rets'], valuation=c.get('val')))

    print(f"[{_now()}] [MORNING] Generating AI outlook...")
    outlook = ai_morning_outlook(
        taiex_pct, global_lines, stock_summary,
        max_tokens=ai_cfg.get('max_tokens_outlook', 200),
        model=ai_cfg.get('model', 'anthropic/claude-haiku-3-5'),
    )

    # Portfolio summary line (only shown if user has real positions)
    pf_summary = ''
    if pf_cost_total > 0:
        d_sign = '+' if pf_daily_total >= 0 else ''
        t_sign = '+' if pf_gain_total  >= 0 else ''
        pf_total_pct = pf_gain_total / pf_cost_total * 100
        pf_summary = (
            f"💰 今日持倉：今日損益 {d_sign}{pf_daily_total:,.0f}元"
            f" | 持倉總損益 {t_sign}{pf_gain_total:,.0f}元 ({pf_total_pct:+.1f}%)"
        )

    # Assemble — build each section block, then emit in the configured order
    blocks = {}

    mov = ["市場總覽："]
    if taiex:
        mov.append(f"• 加權指數：{taiex['open']:,.0f} 點（開盤參考）")
        mov.append(f"• 昨收漲跌：{taiex_pct:+.2f}%")
        mov.append(f"• 市場情緒：{get_market_sentiment(taiex_pct)}")
    else:
        mov.append("• 加權指數：資料暫時無法取得")
    mov.append("")
    blocks['market_overview'] = mov

    gm = ["🌐 全球市場（上一交易日收盤）："]
    gm.extend(global_lines)
    gm.append("")
    blocks['global_markets'] = gm

    hold = ["🎯 **持倉：**"]
    if pf_summary and sections.get('cost_line', True):
        hold.append(pf_summary)
        hold.append("")
    hold.extend(holding_sections)
    hold.append("")
    blocks['holdings'] = hold

    if watch_sections:
        wl = ["👁 **觀察清單：**", ""]
        wl.extend(watch_sections)
        wl.append("")
        blocks['watchlist'] = wl

    if outlook:
        blocks['ai_outlook'] = [f"💡 開盤展望：\n{outlook}"]

    lines = []
    style_header = _style_text(cfg, 'header')
    if style_header:
        lines.append(style_header)
        lines.append("")
    lines.append(f"📊 {date_str} 台股開盤快報（{time_str} 數據）")
    lines.append(f"⚠️ 數據來源：Yahoo Finance（TWSE官方數據於收盤後發布）")
    lines.append("")
    morning_order = cfg.get('sections', {}).get('morning_order')
    for key in _resolve_section_order(
            morning_order,
            ['market_overview', 'global_markets', 'holdings', 'watchlist', 'ai_outlook']):
        if sections.get(key, True) and key in blocks:
            lines.extend(blocks[key])

    _summary = ''
    if sections.get('report_summary', True):
        # Scrub manual 📝 notes (watchlist 備註 + holdings note) — a static thesis must
        # not contaminate analysis of a fluid market. Notes still print in the report.
        _clean = "\n".join(l for l in "\n".join(lines).split("\n")
                           if not l.lstrip().startswith("📝"))
        _digest = _summary_digest(watch_data, holdings_data, {
            'taiex_pct': taiex_pct, 'pf_cost_total': pf_cost_total,
            'pf_value_total': pf_cost_total + pf_gain_total, 'pf_gain_total': pf_gain_total,
            'pf_total_pct': (pf_gain_total / pf_cost_total * 100) if pf_cost_total else 0.0,
            'pf_day_pnl': pf_daily_total,
        }, technicals_cfg)
        _hist = _history_digest(
            _load_pool_history(output_dir, 'morning', ai_cfg.get('summary_history_days', 5)),
            {'date': date_str, 'taiex_pct': taiex_pct,
             'pf_total_pct': (pf_gain_total / pf_cost_total * 100) if pf_cost_total else 0.0},
            watch_data)
        _summary = ai_report_summary(_clean, cfg, digest=_digest, history=_hist)
        if _summary:
            lines.append("")
            lines.append("📋 **報告總結（AI 分析師）：**")
            lines.append(_summary)

    style_footer = _style_text(cfg, 'footer')
    if style_footer:
        lines.append("")
        lines.append(style_footer)

    report = "\n".join(lines)
    record = _build_pool_record(
        'morning', date_str, taiex_pct,
        pf_daily_total, pf_gain_total, pf_cost_total,
        holdings_data, watch_data,
        ai_summary=_summary, ai_commentary=(outlook or ''), news=[])
    _save_and_print(report, output_dir, 'twse_daily_report.md', mode='morning', record=record)
    return report


# ---------------------------------------------------------------------------
# Closing report (Mode B) — TWSE official + yfinance technicals
# ---------------------------------------------------------------------------

def generate_closing_report():
    tracked       = load_tracked_stocks()
    tracked_notes = load_tracked_notes()
    portfolio  = load_portfolio()
    date_str   = datetime.datetime.now().strftime('%Y-%m-%d')
    time_str   = datetime.datetime.now().strftime('%H:%M')
    output_dir = os.getenv('OPENCLAW_DATA_DIR', '/app/data')

    cfg            = load_bot_config()
    technicals_cfg = cfg.get('technicals', {})
    ai_cfg         = cfg.get('ai', {})
    news_cfg       = cfg.get('news', {})
    sections       = cfg.get('sections', {}).get('closing', {})
    period_days    = technicals_cfg.get('period_days', 20)

    print(f"[{_now()}] [CLOSING] Fetching TAIEX...")
    taiex = fetch_taiex()
    taiex_pct = taiex['pct'] if taiex else 0.0

    print(f"[{_now()}] [CLOSING] Fetching global indices...")
    global_lines = fetch_global_indices(cfg.get('global_indices'))

    print(f"[{_now()}] [CLOSING] Fetching TWSE official data...")
    all_stocks = fetch_twse_all()
    if not all_stocks:
        print(f"[{_now()}] TWSE data unavailable — aborting closing report.")
        return None

    # Check if TWSE data is today's — ROC year = Gregorian - 1911
    twse_date_raw = all_stocks[0].get('Date', '') if all_stocks else ''
    today_roc = datetime.datetime.now().strftime(f"{datetime.datetime.now().year - 1911}%m%d")
    data_is_stale = twse_date_raw != today_roc

    valid = parse_twse_valid(all_stocks)
    twse_by_code = {s['Code']: s for s in valid}

    top_volume = sorted(valid, key=lambda x: x['_vol'], reverse=True)[:5]
    top_losers = sorted(valid, key=lambda x: x['_pct'])[:5]

    # Merge TPEX (上櫃) data for portfolio stocks not on TWSE main board
    print(f"[{_now()}] [CLOSING] Fetching TPEX data...")
    tpex_stocks = fetch_tpex_all()
    if tpex_stocks:
        for s in parse_twse_valid(tpex_stocks):
            if s['Code'] not in twse_by_code:
                twse_by_code[s['Code']] = s

    print(f"[{_now()}] [CLOSING] Fetching fundamentals (valuation, margin)...")
    _valuation = fetch_valuation()
    _margin    = fetch_margin()

    print(f"[{_now()}] [CLOSING] Gathering holdings + watchlist...")
    holding_sections = []
    holdings_data    = []
    pf_daily_total   = 0.0
    pf_gain_total    = 0.0
    pf_cost_total    = 0.0

    # Pass 1 — gather holdings (data only; 原因 comes from one batched call below)
    hold_ctx = []
    for code, pos in portfolio.items():
        name = pos.get('name', code)
        row = twse_by_code.get(code) or _twse_row_from_yfinance(code)
        if not row:
            holding_sections.append(f"• **{name} ({code})**：今日無交易數據")
            holding_sections.append("")
            continue
        rsi, vol_ratio = fetch_stock_technicals(code, period_days=period_days)
        hold_ctx.append({
            'code': code, 'name': name, 'pos': pos, 'row': row,
            'open_p': row.get('OpeningPrice', 'N/A'), 'close_p': row.get('ClosingPrice', 'N/A'),
            'change': row['_change'], 'pct': row['_pct'],
            'zhang': format_zhang(row.get('TradeVolume', '0')),
            'rsi': rsi, 'vol_ratio': vol_ratio,
        })

    # Pass 1 — gather watchlist
    watch_sections = []
    watch_data     = []
    watch_ctx = []
    for code, name in tracked.items():
        if code in portfolio:
            continue
        row = twse_by_code.get(code) or _twse_row_from_yfinance(code)
        if not row:
            watch_sections.append(f"• **{name} ({code})**：今日無交易數據")
            watch_sections.append("")
            continue
        rsi, vol_ratio = fetch_stock_technicals(code, period_days=period_days)
        watch_ctx.append({
            'code': code, 'name': name, 'row': row,
            'open_p': row.get('OpeningPrice', 'N/A'), 'close_p': row.get('ClosingPrice', 'N/A'),
            'change': row['_change'], 'pct': row['_pct'],
            'zhang': format_zhang(row.get('TradeVolume', '0')),
            'rsi': rsi, 'vol_ratio': vol_ratio, 'rets': fetch_period_returns(code),
            'note': tracked_notes.get(code, ''),
        })

    # One batched AI call for every 原因 (holdings + watchlist) — cross-stock aware
    _attach_signals(hold_ctx + watch_ctx, _valuation, _margin)
    print(f"[{_now()}] [CLOSING] AI 原因 (batched, {len(hold_ctx) + len(watch_ctx)} stocks)...")
    _reasons = ai_stock_reasons_batch(hold_ctx + watch_ctx, taiex_pct, cfg)

    # Pass 2 — render holdings
    for c in hold_ctx:
        code, name, row = c['code'], c['name'], c['row']
        reason = _reasons.get(code, '')
        line = (
            f"• **{name} ({code})**：[開盤] {c['open_p']}元 → [收盤] {c['close_p']}元"
            f" ({'漲' if c['change'] >= 0 else '跌'} {abs(c['change']):.1f}元 / {c['pct']:+.2f}%)"
            f" [成交量: {c['zhang']}張]{_src_tag(row)}"
        )
        try:
            pf_line = format_portfolio_line(c['pos']['shares'], c['pos']['cost_basis'], row['_close'])
            line += '\n' + pf_line
            pf_daily_total += c['pos']['shares'] * c['change']
            pf_gain_total  += c['pos']['shares'] * row['_close'] - c['pos']['cost_basis']
            pf_cost_total  += c['pos']['cost_basis']
        except (ValueError, TypeError, KeyError):
            pass
        if reason:
            line += f"\n    原因：{reason}"
        line += _stock_note(cfg, code)
        holding_sections.append(line)
        holding_sections.append("")
        holdings_data.append(_stock_row(
            code, name, row.get('_close'), c['change'], c['pct'], c['rsi'], c['vol_ratio'], reason,
            (cfg.get('notes') or {}).get(str(code), ''), valuation=c.get('val')))

    # Pass 2 — render watchlist
    for c in watch_ctx:
        code, name, row = c['code'], c['name'], c['row']
        reason = _reasons.get(code, '')
        line = (
            f"• **{name} ({code})**：[開盤] {c['open_p']}元 → [收盤] {c['close_p']}元"
            f" ({'漲' if c['change'] >= 0 else '跌'} {abs(c['change']):.1f}元 / {c['pct']:+.2f}%)"
            f" [成交量: {c['zhang']}張]{_src_tag(row)}"
        )
        line += _returns_line(code, c['rets'])
        if reason:
            line += f"\n    原因：{reason}"
        if c['note']:
            line += f"\n    📝 備註：{c['note']}"
        watch_sections.append(line)
        watch_sections.append("")
        watch_data.append(_stock_row(
            code, name, row.get('_close'), c['change'], c['pct'], c['rsi'], c['vol_ratio'], reason,
            c['note'], returns=c['rets'], valuation=c.get('val')))

    print(f"[{_now()}] [CLOSING] Fetching Brave news headlines...")
    brave_headlines = fetch_brave_news(
        query=news_cfg.get('query_closing', '台股 今日 財經 股市'),
        count=news_cfg.get('count', 5),
    )

    print(f"[{_now()}] [CLOSING] Generating AI market commentary...")
    research, recommend = ai_closing_commentary(
        taiex_pct, global_lines, top_volume, top_losers,
        max_tokens=ai_cfg.get('max_tokens_research', 350),
        model=ai_cfg.get('model', 'anthropic/claude-haiku-3-5'),
    )

    # Portfolio summary line (only shown if user has real positions)
    pf_summary = ''
    if pf_cost_total > 0:
        d_sign = '+' if pf_daily_total >= 0 else ''
        t_sign = '+' if pf_gain_total  >= 0 else ''
        pf_total_pct = pf_gain_total / pf_cost_total * 100
        pf_summary = (
            f"💰 今日持倉：今日損益 {d_sign}{pf_daily_total:,.0f}元"
            f" | 持倉總損益 {t_sign}{pf_gain_total:,.0f}元 ({pf_total_pct:+.1f}%)"
        )

    # Assemble — build each section block, then emit in the configured order
    blocks = {}

    mov = ["市場總覽："]
    if taiex:
        mov.append(f"• 加權指數：{taiex['close']:,.0f} 點")
        mov.append(f"• 漲跌幅：{taiex_pct:+.2f}%")
        mov.append(f"• 市場情緒評估：{get_market_sentiment(taiex_pct)}")
    else:
        mov.append("• 加權指數：資料暫時無法取得")
    mov.append("")
    blocks['market_overview'] = mov

    gm = ["🌐 全球市場："]
    gm.extend(global_lines)
    gm.append("")
    blocks['global_markets'] = gm

    hot = ["🔥 **今日市場熱點掃描：**", "*成交量前五：*"]
    for s in top_volume:
        hot.append(f"  • {s['Name']} ({s['Code']}): {format_zhang(s['_vol'])}張 ({s['_pct']:+.2f}%)")
    hot.append("")
    hot.append("*跌幅前五：*")
    for s in top_losers:
        hot.append(f"  • {s['Name']} ({s['Code']}): {s['_pct']:+.2f}%")
    hot.append("")
    blocks['hotlist'] = hot

    hold = ["**持倉：**"]
    if pf_summary and sections.get('cost_line', True):
        hold.append(pf_summary)
        hold.append("")
    hold.extend(holding_sections)
    hold.append("")
    blocks['holdings'] = hold

    if watch_sections:
        wl = ["👁 **觀察清單：**", ""]
        wl.extend(watch_sections)
        wl.append("")
        blocks['watchlist'] = wl

    if brave_headlines:
        nb = ["📰 **今日財經新聞：**"]
        for h in brave_headlines:
            nb.append(f"  • {h}")
        nb.append("")
        blocks['news'] = nb

    ai_block = []
    if research:
        ai_block.append(research)
    if recommend:
        if ai_block:
            ai_block.append("")
        ai_block.append(recommend)
    if ai_block:
        blocks['ai_research'] = ai_block

    lines = []
    style_header = _style_text(cfg, 'header')
    if style_header:
        lines.append(style_header)
        lines.append("")
    lines.append(f"📊 {date_str} 台股收盤報告（{time_str} 數據）")
    if data_is_stale:
        lines.append(f"⚠️ 注意：TWSE尚未發布今日數據，以下為最近交易日（{twse_date_raw}）數據")
    lines.append(f"數據來源：TWSE官方 / 技術指標：Yahoo Finance")
    lines.append("")
    closing_order = cfg.get('sections', {}).get('closing_order')
    for key in _resolve_section_order(
            closing_order,
            ['market_overview', 'global_markets', 'hotlist', 'holdings', 'watchlist', 'news', 'ai_research']):
        if sections.get(key, True) and key in blocks:
            lines.extend(blocks[key])

    _summary = ''
    if sections.get('report_summary', True):
        # Scrub manual 📝 notes (watchlist 備註 + holdings note) — a static thesis must
        # not contaminate analysis of a fluid market. Notes still print in the report.
        _clean = "\n".join(l for l in "\n".join(lines).split("\n")
                           if not l.lstrip().startswith("📝"))
        _digest = _summary_digest(watch_data, holdings_data, {
            'taiex_pct': taiex_pct, 'pf_cost_total': pf_cost_total,
            'pf_value_total': pf_cost_total + pf_gain_total, 'pf_gain_total': pf_gain_total,
            'pf_total_pct': (pf_gain_total / pf_cost_total * 100) if pf_cost_total else 0.0,
            'pf_day_pnl': pf_daily_total,
        }, technicals_cfg)
        _hist = _history_digest(
            _load_pool_history(output_dir, 'closing', ai_cfg.get('summary_history_days', 5)),
            {'date': date_str, 'taiex_pct': taiex_pct,
             'pf_total_pct': (pf_gain_total / pf_cost_total * 100) if pf_cost_total else 0.0},
            watch_data)
        _summary = ai_report_summary(_clean, cfg, digest=_digest, history=_hist)
        if _summary:
            lines.append("")
            lines.append("📋 **報告總結（AI 分析師）：**")
            lines.append(_summary)

    style_footer = _style_text(cfg, 'footer')
    if style_footer:
        lines.append("")
        lines.append(style_footer)

    report = "\n".join(lines)
    _commentary = "\n\n".join(p for p in (research, recommend) if p)
    record = _build_pool_record(
        'closing', date_str, taiex_pct,
        pf_daily_total, pf_gain_total, pf_cost_total,
        holdings_data, watch_data,
        ai_summary=_summary, ai_commentary=_commentary, news=brave_headlines or [])
    _save_and_print(report, output_dir, 'twse_daily_report.md', mode='closing', record=record)
    return report


# ---------------------------------------------------------------------------
# Shared save + print
# ---------------------------------------------------------------------------

def _stock_row(code, name, close, change, pct, rsi, vol_ratio, ai_reason, note,
               returns=None, valuation=None):
    """One structured per-stock entry for the data pool (holdings / watchlist).
    `returns` is the 1月/3月/1年 dict; `valuation` holds pe/yield/pb/margin_chg."""
    return {
        'code': code, 'name': name,
        'close': close, 'change': change, 'pct': pct,
        'rsi': rsi, 'vol_ratio': vol_ratio,
        'returns': returns or {},
        'valuation': valuation or {},
        'ai_reason': (ai_reason or '').strip(),
        'note': (note or '').strip(),
    }


def _rsi_state(rsi, tech_cfg):
    if rsi is None:
        return ''
    if rsi >= tech_cfg.get('rsi_overbought', 80):
        return '超買'
    if rsi <= tech_cfg.get('rsi_oversold', 30):
        return '超賣'
    return ''


def _summary_digest(watch_data, holdings_data, scalars, tech_cfg):
    """Compact, NOTE-FREE structured digest fed to the analyst summary so it reads
    the watchlist on objective signals (RSI/量比/乖離/報酬) instead of scraped prose.
    Deliberately omits the static 備註 note and the per-stock 原因 (avoids AI-of-AI echo)."""
    taiex_pct = scalars.get('taiex_pct', 0.0)
    div_thr   = tech_cfg.get('divergence_threshold', 1.5)
    lines = [f"加權指數 {taiex_pct:+.2f}%"]
    if scalars.get('pf_cost_total'):
        lines.append(
            f"持倉 現值{scalars.get('pf_value_total', 0):,.0f} · "
            f"總損益{scalars.get('pf_gain_total', 0):+,.0f}"
            f"({scalars.get('pf_total_pct', 0):+.1f}%) · 今日{scalars.get('pf_day_pnl', 0):+,.0f}"
        )

    def _fmt_rows(rows):
        out = []
        for r in rows:
            pct = r.get('pct')
            parts = [f"{r.get('name')}({r.get('code')})"]
            if r.get('close') is not None:
                parts.append(f"收{r['close']}")
            if pct is not None:
                parts.append(f"{pct:+.2f}%")
            rsi = r.get('rsi')
            if rsi is not None:
                st = _rsi_state(rsi, tech_cfg)
                parts.append(f"RSI{rsi}" + (f"/{st}" if st else ''))
            if r.get('vol_ratio') is not None:
                parts.append(f"量比{r['vol_ratio']}")
            if pct is not None and abs(pct - taiex_pct) > div_thr:
                parts.append("乖離大盤")
            rets = r.get('returns') or {}
            rstr = " ".join(f"{k}{rets[k]:+.0f}%" for k in ('1月', '3月', '1年')
                            if rets.get(k) is not None)
            if rstr:
                parts.append(rstr)
            val = r.get('valuation') or {}
            if val.get('pe') is not None:
                parts.append(f"PE{val['pe']:.1f}")
            if val.get('yield') is not None:
                parts.append(f"殖利率{val['yield']:.1f}%")
            if val.get('margin_chg') is not None:
                parts.append(f"融資{val['margin_chg']:+}張")
            out.append("- " + " ".join(parts))
        return out

    if watch_data:
        lines.append("觀察清單（當日客觀數據）:")
        lines.extend(_fmt_rows(watch_data))
    if holdings_data:
        lines.append("持倉個股（當日客觀數據）:")
        lines.extend(_fmt_rows(holdings_data))
    return "\n".join(lines)


def _load_pool_history(output_dir, mode, days):
    """Last `days` distinct-date pool records for `mode`, oldest→newest, from
    diary/pool.jsonl. Only numeric fields are ever read downstream (NO note /
    ai_reason), so history grounding stays note-free. Empty list on any failure."""
    if not days or days <= 0:
        return []
    path = os.path.join(output_dir, 'diary', 'pool.jsonl')
    by_date = {}
    try:
        with open(path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get('mode') == mode and rec.get('date'):
                    by_date[rec['date']] = rec        # latest run of a date wins
    except (FileNotFoundError, OSError):
        return []
    return [by_date[d] for d in sorted(by_date)][-days:]


def _streak(pcts):
    """Trailing consecutive same-direction run from the newest → '連N日漲/跌' or ''."""
    vals = [p for p in pcts if p is not None]
    if not vals or vals[-1] == 0:
        return ''
    up, run = vals[-1] > 0, 0
    for v in reversed(vals):
        if v == 0 or (v > 0) != up:
            break
        run += 1
    return f"連{run}日{'漲' if up else '跌'}" if run >= 2 else ''


def _history_digest(history, today_ctx, today_watch, max_names=15):
    """Compact NOTE-FREE recent-trend block: pool history + today's numbers. Reads
    only numeric fields (date / taiex_pct / pf_total_pct and per-stock code/pct) —
    never note or ai_reason — so it cannot leak the static thesis."""
    if not history:
        return ''
    md = lambda d: (d or '')[5:]                       # YYYY-MM-DD → MM-DD
    out = [f"（歷史為近 {len(history)} 個同時段交易日，數值為當日漲跌%/報酬%）"]
    out.append("加權指數%: " + " · ".join(
        f"{md(r.get('date'))} {r.get('taiex_pct', 0):+.2f}" for r in history)
        + f" · {md(today_ctx.get('date'))}(今){today_ctx.get('taiex_pct', 0):+.2f}")
    out.append("持倉報酬%: " + " · ".join(
        f"{md(r.get('date'))} {r.get('pf_total_pct', 0):+.1f}" for r in history)
        + f" · {md(today_ctx.get('date'))}(今){today_ctx.get('pf_total_pct', 0):+.1f}")

    hist_pct = {}                                       # code -> {date: pct}
    for r in history:
        for w in r.get('watchlist', []):
            hist_pct.setdefault(w.get('code'), {})[r.get('date')] = w.get('pct')
    rows = []
    for w in today_watch[:max_names]:
        code = w.get('code')
        cells, series = [], []
        for r in history:
            v = hist_pct.get(code, {}).get(r.get('date'))
            series.append(v)
            cells.append(f"{v:+.1f}" if v is not None else "–")
        series.append(w.get('pct'))
        cells.append(f"{w.get('pct'):+.1f}(今)" if w.get('pct') is not None else "–(今)")
        hint = _streak(series)
        rows.append(f"- {w.get('name')}({code}): " + " · ".join(cells)
                    + (f"  [{hint}]" if hint else ''))
    if rows:
        out.append("觀察清單近日漲跌%:")
        out.extend(rows)
    return "\n".join(out)


def _build_pool_record(mode, date_str, taiex_pct, pf_day_pnl, pf_gain_total,
                       pf_cost_total, holdings, watchlist,
                       ai_summary='', ai_commentary='', news=None):
    """Assemble one structured pool record: flat scalars + per-stock arrays + AI
    text. `report_md` is filled in by _save_and_print once the archive name exists."""
    pf_value_total = pf_cost_total + pf_gain_total
    pf_total_pct   = (pf_gain_total / pf_cost_total * 100) if pf_cost_total else 0.0
    return {
        'date': date_str,
        'mode': mode,
        'run_utc': datetime.datetime.utcnow().isoformat(timespec='seconds') + 'Z',
        'taiex_pct': round(taiex_pct, 4),
        'pf_cost_total': round(pf_cost_total, 2),
        'pf_value_total': round(pf_value_total, 2),
        'pf_day_pnl': round(pf_day_pnl, 2),
        'pf_gain_total': round(pf_gain_total, 2),
        'pf_total_pct': round(pf_total_pct, 4),
        'n_holdings': len(holdings),
        'n_watch': len(watchlist),
        'holdings': holdings,
        'watchlist': watchlist,
        'ai_summary': (ai_summary or '').strip(),
        'ai_commentary': (ai_commentary or '').strip(),
        'news': news or [],
        'report_md': '',
    }


# Flat scalar columns mirrored into metrics.csv (order defines the CSV header).
_POOL_CSV_COLS = ['date', 'mode', 'run_utc', 'taiex_pct', 'pf_cost_total',
                  'pf_value_total', 'pf_day_pnl', 'pf_gain_total', 'pf_total_pct',
                  'n_holdings', 'n_watch', 'report_md']


def append_to_pool(record, output_dir):
    """Append one report run to the local data pool for later inference/analysis:
    a full-fidelity JSON line in diary/pool.jsonl and a flat scalar row in
    diary/metrics.csv. Best-effort — never raises into the report path."""
    if not record:
        return
    try:
        pool_dir = os.path.join(output_dir, 'diary')
        os.makedirs(pool_dir, exist_ok=True)
        with open(os.path.join(pool_dir, 'pool.jsonl'), 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        csv_path = os.path.join(pool_dir, 'metrics.csv')
        is_new = not os.path.exists(csv_path)
        with open(csv_path, 'a', encoding='utf-8', newline='') as f:
            w = csv.writer(f)
            if is_new:
                w.writerow(_POOL_CSV_COLS)
            w.writerow([record.get(c, '') for c in _POOL_CSV_COLS])
        print(f"[{_now()}] Pooled → {os.path.join(pool_dir, 'pool.jsonl')}")
    except Exception as e:
        print(f"[{_now()}] Pool error: {e}")


def _prune_report_archive(arch_dir, keep=180):
    """Keep only the newest `keep` archived reports — caps disk growth.
    Filenames lead with the date (twse_YYYY-MM-DD_HHMM_*.md), so a lexical sort is
    chronological; the oldest beyond `keep` are removed."""
    try:
        files = sorted(f for f in os.listdir(arch_dir)
                       if f.startswith('twse_') and f.endswith('.md'))
        for old in files[:-keep] if keep > 0 else files:
            try:
                os.remove(os.path.join(arch_dir, old))
            except OSError:
                pass
    except OSError:
        pass


def _save_and_print(report, output_dir, filename, mode=None, record=None):
    path = os.path.join(output_dir, filename)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(report)
    print(f"[{_now()}] Report saved to {path}")
    # Dated local backup of every LIVE report (sandbox runs emit their own preview files).
    if os.getenv('SANDBOX_MODE', '').lower() != 'true':
        try:
            arch_dir = os.path.join(output_dir, 'reports')
            os.makedirs(arch_dir, exist_ok=True)
            stamp     = datetime.datetime.now().strftime('%Y-%m-%d_%H%M')
            label     = f"_{mode}" if mode else ""
            arch_path = os.path.join(arch_dir, f"twse_{stamp}{label}.md")
            with open(arch_path, 'w', encoding='utf-8') as f:
                f.write(report)
            print(f"[{_now()}] Archived → {arch_path}")
            _prune_report_archive(arch_dir, keep=180)
            # Structured data pool for later inference/analysis (links to this .md).
            if record is not None:
                record['report_md'] = os.path.basename(arch_path)
                append_to_pool(record, output_dir)
        except Exception as e:
            print(f"[{_now()}] Archive error: {e}")
        print("\n" + "=" * 60)
        print(report)
        print("=" * 60 + "\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def generate_daily_report(mode='closing', send=True):
    if mode == 'morning':
        report = generate_morning_report()
    else:
        report = generate_closing_report()
    if report and send:
        deliver_report(report)


if __name__ == '__main__':
    args    = sys.argv[1:]
    dry_run = '--dry-run' in args
    mode    = 'closing'
    for a in args:
        if a.startswith('--mode='):
            mode = a.split('=', 1)[1]
        elif a == '--morning':
            mode = 'morning'
        elif a == '--closing':
            mode = 'closing'
    generate_daily_report(mode=mode, send=not dry_run)
