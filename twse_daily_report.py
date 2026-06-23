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
import os
import re
import sys
import datetime
import xml.etree.ElementTree as ET
import requests
import yfinance as yf
import numpy as np
import pandas as pd
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
        return json.load(f)          # {"2330": "台積電", ...}


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


# ---------------------------------------------------------------------------
# OpenRouter — text only, numbers always provided via prompt context
# ---------------------------------------------------------------------------

def call_openrouter(prompt, max_tokens=200, model='anthropic/claude-haiku-3-5'):
    api_key = os.getenv('OPENROUTER_API_KEY')
    if not api_key:
        return ''
    try:
        resp = requests.post(
            'https://openrouter.ai/api/v1/chat/completions',
            headers={
                'Authorization': f'Bearer {api_key}',
                'Content-Type': 'application/json',
            },
            json={
                'model': model,
                'messages': [{'role': 'user', 'content': prompt}],
                'max_tokens': max_tokens,
            },
            timeout=20,
        )
        if resp.ok:
            return resp.json()['choices'][0]['message']['content'].strip()
    except Exception as e:
        print(f"[{_now()}] OpenRouter error: {e}")
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
    tracked   = load_tracked_stocks()   # {"2330": "台積電", ...}
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
    stock_summary    = []
    pf_daily_total   = 0.0
    pf_gain_total    = 0.0
    pf_cost_total    = 0.0

    for code, pos in portfolio.items():
        name = pos.get('name', code)
        d = fetch_yfinance_stock(code)
        rsi, vol_ratio = fetch_stock_technicals(code, opening_mode=True, period_days=period_days)
        rsi_s   = f"{rsi}"       if rsi       is not None else 'N/A'
        ratio_s = f"{vol_ratio}" if vol_ratio is not None else 'N/A'

        if d:
            price    = d['price']
            prev_cls = d['prev_close']
            change   = d['change']
            pct      = change / prev_cls * 100 if prev_cls else 0.0
            direction = '漲' if change >= 0 else '跌'
            reason = ai_stock_reason(
                name, code, d['today_open'], price, change, pct, rsi_s, ratio_s, 'N/A', taiex_pct,
                divergence_threshold=technicals_cfg.get('divergence_threshold', 1.5),
                rsi_overbought=technicals_cfg.get('rsi_overbought', 80),
                rsi_oversold=technicals_cfg.get('rsi_oversold', 30),
                max_tokens=ai_cfg.get('max_tokens_reason', 100),
                model=ai_cfg.get('model', 'anthropic/claude-haiku-3-5'),
            )
            line = (
                f"• **{name} ({code})**：目前 {price:,.1f}元"
                f" [昨收 {prev_cls:,.1f} | {direction}{abs(change):.1f}元 ({pct:+.2f}%)]"
            )
            try:
                pf_line = format_portfolio_line(pos['shares'], pos['cost_basis'], price)
                line += '\n' + pf_line
                pf_daily_total += pos['shares'] * change
                pf_gain_total  += pos['shares'] * price - pos['cost_basis']
                pf_cost_total  += pos['cost_basis']
            except (ValueError, TypeError, KeyError):
                pass
            if reason:
                line += f"\n  展望：{reason}"
            holding_sections.append(line)
            holding_sections.append("")
            stock_summary.append(f"{name}({code}) {direction}{abs(pct):.1f}%")
        else:
            holding_sections.append(f"• **{name} ({code})**：資料暫時無法取得")
            holding_sections.append("")

    print(f"[{_now()}] [MORNING] Fetching watchlist (yfinance live)...")
    watch_sections = []
    for code, name in tracked.items():
        if code in portfolio:
            continue
        d = fetch_yfinance_stock(code)
        rsi, vol_ratio = fetch_stock_technicals(code, opening_mode=True, period_days=period_days)
        rsi_s   = f"{rsi}"       if rsi       is not None else 'N/A'
        ratio_s = f"{vol_ratio}" if vol_ratio is not None else 'N/A'

        if d:
            price    = d['price']
            prev_cls = d['prev_close']
            change   = d['change']
            pct      = change / prev_cls * 100 if prev_cls else 0.0
            direction = '漲' if change >= 0 else '跌'
            reason = ai_stock_reason(
                name, code, d['today_open'], price, change, pct, rsi_s, ratio_s, 'N/A', taiex_pct,
                divergence_threshold=technicals_cfg.get('divergence_threshold', 1.5),
                rsi_overbought=technicals_cfg.get('rsi_overbought', 80),
                rsi_oversold=technicals_cfg.get('rsi_oversold', 30),
                max_tokens=ai_cfg.get('max_tokens_reason', 100),
                model=ai_cfg.get('model', 'anthropic/claude-haiku-3-5'),
            )
            line = (
                f"• **{name} ({code})**：目前 {price:,.1f}元"
                f" [昨收 {prev_cls:,.1f} | {direction}{abs(change):.1f}元 ({pct:+.2f}%)]"
            )
            if reason:
                line += f"\n  展望：{reason}"
            watch_sections.append(line)
            watch_sections.append("")
        else:
            watch_sections.append(f"• **{name} ({code})**：資料暫時無法取得")
            watch_sections.append("")

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
    lines.append(f"📊 {date_str} 台股開盤快報（{time_str} 數據）")
    lines.append(f"⚠️ 數據來源：Yahoo Finance（TWSE官方數據於收盤後發布）")
    lines.append("")
    morning_order = cfg.get('sections', {}).get('morning_order')
    for key in _resolve_section_order(
            morning_order,
            ['market_overview', 'global_markets', 'holdings', 'watchlist', 'ai_outlook']):
        if sections.get(key, True) and key in blocks:
            lines.extend(blocks[key])

    report = "\n".join(lines)
    _save_and_print(report, output_dir, 'twse_daily_report.md')
    return report


# ---------------------------------------------------------------------------
# Closing report (Mode B) — TWSE official + yfinance technicals
# ---------------------------------------------------------------------------

def generate_closing_report():
    tracked    = load_tracked_stocks()
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

    print(f"[{_now()}] [CLOSING] Fetching technicals + AI for holdings...")
    holding_sections = []
    pf_daily_total   = 0.0
    pf_gain_total    = 0.0
    pf_cost_total    = 0.0

    for code, pos in portfolio.items():
        name = pos.get('name', code)
        row = twse_by_code.get(code)
        if not row:
            holding_sections.append(f"• **{name} ({code})**：今日無交易數據")
            holding_sections.append("")
            continue

        open_p  = row.get('OpeningPrice', 'N/A')
        close_p = row.get('ClosingPrice', 'N/A')
        change  = row['_change']
        pct     = row['_pct']
        zhang   = format_zhang(row.get('TradeVolume', '0'))
        direction = '漲' if change >= 0 else '跌'

        rsi, vol_ratio = fetch_stock_technicals(code, period_days=period_days)
        rsi_s   = f"{rsi}"       if rsi       is not None else 'N/A'
        ratio_s = f"{vol_ratio}" if vol_ratio is not None else 'N/A'

        reason = ai_stock_reason(
            name, code, open_p, close_p, change, pct, rsi_s, ratio_s, zhang, taiex_pct,
            divergence_threshold=technicals_cfg.get('divergence_threshold', 1.5),
            rsi_overbought=technicals_cfg.get('rsi_overbought', 80),
            rsi_oversold=technicals_cfg.get('rsi_oversold', 30),
            max_tokens=ai_cfg.get('max_tokens_reason', 100),
            model=ai_cfg.get('model', 'anthropic/claude-haiku-3-5'),
        )
        line = (
            f"• **{name} ({code})**：[開盤] {open_p}元 → [收盤] {close_p}元"
            f" ({direction} {abs(change):.1f}元 / {pct:+.2f}%)"
            f" [成交量: {zhang}張]"
        )
        try:
            pf_line = format_portfolio_line(pos['shares'], pos['cost_basis'], row['_close'])
            line += '\n' + pf_line
            pf_daily_total += pos['shares'] * change
            pf_gain_total  += pos['shares'] * row['_close'] - pos['cost_basis']
            pf_cost_total  += pos['cost_basis']
        except (ValueError, TypeError, KeyError):
            pass
        if reason:
            line += f"\n    原因：{reason}"
        holding_sections.append(line)
        holding_sections.append("")

    print(f"[{_now()}] [CLOSING] Fetching technicals + AI for watchlist...")
    watch_sections = []
    for code, name in tracked.items():
        if code in portfolio:
            continue
        row = twse_by_code.get(code)
        if not row:
            watch_sections.append(f"• **{name} ({code})**：今日無交易數據")
            watch_sections.append("")
            continue

        open_p  = row.get('OpeningPrice', 'N/A')
        close_p = row.get('ClosingPrice', 'N/A')
        change  = row['_change']
        pct     = row['_pct']
        zhang   = format_zhang(row.get('TradeVolume', '0'))
        direction = '漲' if change >= 0 else '跌'

        rsi, vol_ratio = fetch_stock_technicals(code, period_days=period_days)
        rsi_s   = f"{rsi}"       if rsi       is not None else 'N/A'
        ratio_s = f"{vol_ratio}" if vol_ratio is not None else 'N/A'

        reason = ai_stock_reason(
            name, code, open_p, close_p, change, pct, rsi_s, ratio_s, zhang, taiex_pct,
            divergence_threshold=technicals_cfg.get('divergence_threshold', 1.5),
            rsi_overbought=technicals_cfg.get('rsi_overbought', 80),
            rsi_oversold=technicals_cfg.get('rsi_oversold', 30),
            max_tokens=ai_cfg.get('max_tokens_reason', 100),
            model=ai_cfg.get('model', 'anthropic/claude-haiku-3-5'),
        )
        line = (
            f"• **{name} ({code})**：[開盤] {open_p}元 → [收盤] {close_p}元"
            f" ({direction} {abs(change):.1f}元 / {pct:+.2f}%)"
            f" [成交量: {zhang}張]"
        )
        if reason:
            line += f"\n    原因：{reason}"
        watch_sections.append(line)
        watch_sections.append("")

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

    report = "\n".join(lines)
    _save_and_print(report, output_dir, 'twse_daily_report.md')
    return report


# ---------------------------------------------------------------------------
# Shared save + print
# ---------------------------------------------------------------------------

def _save_and_print(report, output_dir, filename):
    path = os.path.join(output_dir, filename)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(report)
    print(f"[{_now()}] Report saved to {path}")
    if os.getenv('SANDBOX_MODE', '').lower() != 'true':
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
        send_telegram_report(report)


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
