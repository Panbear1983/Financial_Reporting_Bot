"""
TWSE Daily Report — two modes:
  morning  (01:05 UTC / 09:05 Taiwan): the exchange's own real-time prices (mis.twse.com.tw),
           holdings only, no AI — see docs/MORNING_REBUILD_2026-10-02.md
  closing  (08:00 UTC / 16:00 Taiwan): TWSE official data once published

Data source rules — ZERO figure hallucination:
  All numbers come from scrapers only.
  AI (OpenRouter) is called only for the closing push's explanatory text (原因, 研究, 推薦).
"""

import urllib.request
import io
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
# Every number in this file comes from the dashboard. This module renders and
# sends; it does not fetch and it does not calculate. See dashboard.py's header.
import dashboard
import report_voice
from dashboard import (DATA_DIR, SCRIPT_DIR, format_zhang, _attach_signals, _config_path, _now,
                       _twse_row_from_yfinance, compute_positions, fetch_brave_news,
                       fetch_global_indices, fetch_live_quotes, fetch_margin,
                       fetch_period_returns, fetch_prev_closes, fetch_stock_technicals,
                       fetch_taiex, fetch_tpex_all, fetch_tpex_emerging, fetch_twse_all,
                       fetch_valuation, fetch_yfinance_stock, load_bot_config,
                       load_portfolio, load_tracked_notes, load_tracked_stocks,
                       parse_twse_valid, prefetch_stock_histories, resolve_symbols,
                       taiwan_market_open)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


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
    data_dir   = os.getenv('FRB_DATA_DIR', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data'))
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

def _render_sandbox_photo(png):
    """Sandbox twin of _render_sandbox for a picture: write it next to the text
    preview and print the path, so a dry run shows exactly what Telegram would."""
    data_dir = os.getenv('FRB_DATA_DIR', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data'))
    os.makedirs(data_dir, exist_ok=True)
    fpath = os.path.join(data_dir, f"sandbox_photo_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.png")
    with open(fpath, 'wb') as f:
        f.write(png)
    print(f"  [SANDBOX] Picture saved to:\n  {fpath}\n", flush=True)


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


def _send_telegram_photo(token, chat_id, png, caption=None):
    """POST one PNG to one Telegram destination (sendPhoto). Returns bool; never
    raises. Same shape as report_voice.send — a multipart upload with the file in
    `files` — and the caption is clipped at Telegram's 1024-char limit."""
    if not (token and chat_id and png):
        return False
    payload = {'chat_id': str(chat_id)}
    if caption:
        payload['caption'] = caption[:1024]
    for attempt, delay in ((1, 2), (2, 5), (3, 0)):
        try:
            resp = requests.post(
                f'https://api.telegram.org/bot{token}/sendPhoto',
                data=payload,
                files={'photo': ('wealth.png', io.BytesIO(png), 'image/png')},
                timeout=120)
            if resp.ok:
                print(f"[{_now()}] Photo sent → {chat_id}.")
                return True
            print(f"[{_now()}] Photo failed ({chat_id}, attempt {attempt}): {resp.text[:200]}")
            if resp.status_code < 500:
                return False
        except Exception as e:                                             # noqa: BLE001
            print(f"[{_now()}] Photo error ({chat_id}, attempt {attempt}): {e}")
        if delay:
            time.sleep(delay)
    return False


def deliver_report(text, cfg=None, photo=None, photo_caption=None):
    """Deliver the report to every enabled channel in cfg['delivery']['channels'].

    Backward compatible: when no channels are configured, falls back to the single
    env-based Telegram destination (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID). Telegram
    channels can each name their own token env var + chat_id, so multiple bots / group
    chats are supported. LINE / WhatsApp channels are config placeholders meant to be
    routed through OpenClaw — OpenClaw has no messaging gateway yet, so they are logged
    and skipped (never silently faked as delivered)."""
    if os.getenv('SANDBOX_MODE', '').lower() == 'true':
        _render_sandbox(text)
        if photo:
            _render_sandbox_photo(photo)
        return
    if cfg is None:
        cfg = load_bot_config()

    channels = [c for c in (cfg.get('delivery', {}) or {}).get('channels', []) if c.get('enabled', True)]
    voice_targets = []
    if not channels:
        token, chat_id = os.getenv('TELEGRAM_BOT_TOKEN'), os.getenv('TELEGRAM_CHAT_ID')
        _send_telegram(token, chat_id, text)
        if photo:
            _send_telegram_photo(token, chat_id, photo, photo_caption)
        voice_targets.append((token, chat_id))
    else:
        for ch in channels:
            ctype = (ch.get('type') or 'telegram').lower()
            name  = ch.get('name', ctype)
            if ctype == 'telegram':
                token   = os.getenv(ch.get('token_env', 'TELEGRAM_BOT_TOKEN')) or os.getenv('TELEGRAM_BOT_TOKEN')
                chat_id = ch.get('chat_id') or os.getenv('TELEGRAM_CHAT_ID')
                _send_telegram(token, chat_id, text)
                if photo:
                    _send_telegram_photo(token, chat_id, photo, photo_caption)
                if ch.get('voice', True):
                    voice_targets.append((token, chat_id))
            else:
                print(f"[{_now()}] Channel '{name}' (type={ctype}) configured but OpenClaw has no "
                      f"{ctype} gateway yet — skipped, not sent.")

    # STRICTLY AFTER the text. Synthesis is a slow network round-trip (minutes for a
    # long report), so rendering first would delay the report itself — and a voice
    # failure must never cost a delivered push. Rendered once, shared by all targets.
    if not voice_targets:
        return
    voice_data = report_voice.render(text, cfg)
    for token, chat_id in voice_targets:
        report_voice.send(token, chat_id, voice_data)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def get_market_sentiment(pct):
    if pct > 1:
        return '🔴 樂觀'
    elif pct < -1:
        return '🟢 謹慎'
    return '🟡 觀望'


# ---------------------------------------------------------------------------
# Yahoo batch / cache layer
#
# The urllib chart endpoint used by custom_stock_lookup.get_yfinance_data is
# hard rate-limited (HTTP 429) on this host, while the yfinance library path
# (curl_cffi browser-impersonating session) is not. A report used to fire ~4
# indices + TAIEX + ~30×(quote + technicals + period-returns) individual calls
# and trip 429, blanking data. This layer collapses that into a couple of
# batched yf.download() calls and reuses the results from an in-run cache, so
# the same symbol is never fetched twice. It also resolves each code's
# .TW/.TWO suffix from ticker_suffix_cache.json (extending it) instead of
# always trying .TW first and eating a 404.
# ---------------------------------------------------------------------------


def _suffix_cache_path():
    return os.path.join(DATA_DIR, 'ticker_suffix_cache.json')


# ---------------------------------------------------------------------------
# Scrapers — numbers only, no AI
# ---------------------------------------------------------------------------


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


def _chg_text(close, change):
    """'昨收 335.00，漲 10.0元 / +2.99%' — the day change is measured against the
    PREVIOUS CLOSE, not the open, so the reference has to be on the line. Without it
    a row like 開盤 543 → 收盤 538 (漲 11.0元) reads as self-contradictory.
    Sub-dollar moves keep 2dp so a 0.02元 ETF tick isn't printed as 0.0元."""
    prev = close - change
    pct  = (change / prev * 100) if prev else 0.0
    dp   = 1 if abs(change) >= 1 else 2
    return (f"昨收 {prev:,.2f}，{'漲' if change >= 0 else '跌'} "
            f"{abs(change):,.{dp}f}元 / {pct:+.2f}%")


def _src_tag(row):
    """Inline source marker for closing lines not sourced from TWSE/TPEX mainboard
    official close: 興櫃均價 for the emerging board (加權平均價, no true 開/收 or 量
    convention), 或 yfinance 報價 for the Yahoo fallback (volume is not an official 量)."""
    fb = row.get('_fallback')
    if fb == 'tpex_esb':
        return "（興櫃均價）"
    return "（yfinance 報價）" if fb else ""


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
        "不得臆測使用者的選股理由或動機（報告不含使用者備註）。分析聚焦於大盤與持倉個股，"
        "依當日價格動作、技術指標（RSI／量比／乖離）、融資與 1月／3月／1年 報酬給出判斷，"
        "並可參考近期走勢指出趨勢與連續性（如連續數日漲跌、與昨日比較），但結論仍以當日數據為主。"
        "不需分析觀察清單。"
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
        "1. 大盤與市場概況（可對比全球市場與近期走勢指出趨勢）；\n"
        "2. 持倉個股逐檔重點與整體狀況（引用當日 RSI／量比／融資／報酬與連續性佐證）；\n"
        "3. 需留意的風險。\n"
        "僅分析大盤與持倉，不要納入觀察清單。"
    )
    prompt = "\n\n".join(parts)
    try:
        return call_openrouter(prompt, max_tokens=max_tokens, model=model, api_key=api_key,
                               system=system, temperature=temperature)
    except Exception as e:
        print(f"[{_now()}] Report-summary error: {e}")
        return ''


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
    # Honour the TUI's per-stock 個股原因 token budget (was hardcoded 45/stock, which
    # ignored ai.max_tokens_reason). Scales with stock count; capped so a large
    # watchlist can't run away. Default matches the TUI's displayed default (100).
    per_stock  = int(ai_cfg.get('max_tokens_reason', 100) or 100)
    max_tokens = min(per_stock * len(contexts) + 120, 4000)
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
# Morning report (Mode A) — 09:05, the exchange's own real-time prices
#
# Rebuilt 2026-10-02. Contract and the measurements behind it:
# docs/MORNING_REBUILD_2026-10-02.md. Four blocks, numbers only: the overnight
# moves and the open, today's holdings, the runs worth acting on (marked inline),
# and last night's closing facts carried forward. No AI and no yesterday-sourced
# indicator (RSI / 量比 / 融資 / PE) — at 09:05 those are all yesterday's, and the
# 16:00 push already delivered them from final data.
# ---------------------------------------------------------------------------

def _morning_run_lines(h, threshold, move_min=0.0):
    """(mark, second line) for one holding: the settled streak, then whether
    today so far is extending or breaking it.

    Today's move is NEVER folded into the count — at 09:05 the day can still
    reverse by 13:30. The count is a settled fact through the last close; today
    is a separate flag beside it, worded 延續 / 中斷 rather than as a verdict."""
    st = h.get('streak')
    if not st:
        return '•', ''
    asof = f"（至 {st['as_of']:%m/%d} 收盤）" if st.get('as_of') else "（至昨收）"
    run = st['run']
    if run == 0:
        return '•', f"     平盤，無連續走勢{asof}"
    up = run > 0
    text = f"     {'連漲' if up else '連跌'} {abs(run)} 日 {st['pct']:+.2f}%{asof}"
    if abs(run) < threshold:
        return '•', text
    move = h['price'] - h['prev_cls'] if h.get('prev_cls') else 0.0
    if move == 0:
        return '•', text + "　→ 今日平盤"
    if abs(h['pct']) < move_min:
        # Under the threshold the early direction is a coin flip (measured 53%),
        # so it gets no verdict — see dashboard.open_gap_threshold.
        return '•', text + f"　→ 今日 {h['pct']:+.2f}%，變動不大"
    if (move > 0) == up:
        tail = f"　→ 今日 {h['pct']:+.2f}%，{'連漲' if up else '連跌'}延續第 {abs(run) + 1} 天"
        return '⚡', text + tail + (" → 賣出觀察" if up else "")
    tail = f"　→ 今日 {h['pct']:+.2f}%，{'連漲' if up else '連跌'}中斷"
    return '⚠️', text + tail + ("" if up else " → 止跌觀察")


def _morning_yesterday_lines(rec):
    """📌 昨日重點 — facts the previous closing push already recorded. No fetch."""
    if not rec:
        return []
    out = [f"📌 昨日重點（{rec.get('date', '')} 收盤）："]
    out.append(f"• 加權指數 {rec.get('taiex_pct', 0.0):+.2f}%　"
               f"持倉當日損益 {rec.get('pf_day_pnl', 0.0):+,.0f}元")
    held = [x for x in (rec.get('holdings') or []) if isinstance(x.get('pct'), (int, float))]
    if held:
        movers = sorted({id(x): x for x in (max(held, key=lambda x: x['pct']),
                                            min(held, key=lambda x: x['pct']))}.values(),
                        key=lambda x: -x['pct'])
        for x in movers:
            reason = (x.get('ai_reason') or '').strip()
            reason = f" — {reason[:40]}" if reason else ''
            out.append(f"• {x.get('name', '')} ({x.get('code', '')}) {x['pct']:+.2f}%{reason}")
    out.append("")
    return out


def generate_morning_report():
    # ── Everything below is the dashboard's. This function renders; it does not
    # fetch and it does not calculate. One call, one set of numbers. ──
    gate = dashboard.morning_gate()
    # The scheduler runs this process with TZ=UTC, so every displayed time is
    # converted to Taipei explicitly — a bare now()/astimezone() prints UTC.
    tpe = dashboard.ZoneInfo('Asia/Taipei')
    date_today = datetime.datetime.now(tpe).strftime('%Y-%m-%d')
    if gate['skip']:
        # A skipped push must say so: silence reads exactly like a crashed bot.
        notice = f"📊 {date_today} 台股開盤快報\n⚠️ {gate['reason']}"
        _save_and_print(notice, DATA_DIR, 'twse_daily_report.md', mode='morning', record=None)
        return notice

    snap = dashboard.snapshot('morning')
    if snap is None:
        return None
    cfg        = snap['cfg']
    sections   = cfg.get('sections', {}).get('morning', {})
    calc       = snap['positions']
    total      = calc['total']
    threshold  = snap['streak_threshold']
    date_str   = snap['date_str']
    names      = {h['code']: h['name'] for h in snap['holdings']}
    names.update({c: n for c, n in snap['missing_holdings']})

    pf_daily_total = total['daily_pnl']
    pf_gain_total  = total['pnl']
    pf_cost_total  = total['cost']

    blocks = {}

    # 🌙 the overnight moves and the open
    ov = ["🌙 隔夜與開盤："]
    t = snap['taiex']
    if sections.get('market_overview', True):
        if t:
            late = '（延遲）' if snap['taiex_src'] != 'mis' else ''
            ov.append(f"• 加權指數：昨收 {t['prev_close']:,.0f} → 開盤 {t['open']:,.0f}"
                      f" → 現 {t['last']:,.0f}（{t['pct']:+.2f}%）{late}")
        else:
            ov.append("• 加權指數：資料暫時無法取得")
    if sections.get('global_markets', True):
        ov.extend(snap['global_lines'])
    ov.append("")
    if len(ov) > 2:
        blocks['overnight'] = ov

    # 🎯 today's holdings, each with its run
    hold = ["🎯 **持倉（今日）：**"]
    if pf_cost_total > 0 and sections.get('cost_line', True):
        hold.append(f"💰 今日損益 {pf_daily_total:+,.0f}元 ｜ 總損益 {pf_gain_total:+,.0f}元"
                    f" ({pf_gain_total / pf_cost_total * 100:+.1f}%)")
        if calc['n_unpriced']:
            hold.append(f"⚠️ 僅含 {total['n_priced']}/{total['n_total']} 檔"
                        f"（其餘尚無報價，未計入）")
        if _cash_in_push(cfg):
            hold.extend(_cash_lines(total))
    hold.append("")
    for h in snap['holdings']:
        mark, run_line = _morning_run_lines(h, threshold, snap.get('open_gap_min', 0.0))
        op = f"{h['open_p']:,.2f}" if h.get('open_p') is not None else '—'
        late = '（延遲）' if h.get('src') != 'mis' else ''
        dp = h.get('daily_pnl')
        dp = f"　今日 {dp:+,.0f}元" if dp is not None else ''
        hold.append(f"{mark} {h['name']} ({h['code']})：{h['prev_cls']:,.2f} → 開 {op}"
                    f" → 現 {h['price']:,.2f}{late}　{h['pct']:+.2f}%{dp}")
        if run_line:
            hold.append(run_line)
    for code, name in snap['missing_holdings']:
        why = '尚未成交' if code in snap.get('not_traded', []) else '暫無報價'
        hold.append(f"• {name} ({code})：{why}")
    hold.append("")
    if sections.get('holdings', True):
        blocks['holdings'] = hold

    # 📌 last night's closing facts, carried forward
    yl = _morning_yesterday_lines(snap.get('yesterday'))
    if yl and sections.get('yesterday', True):
        blocks['yesterday'] = yl

    lines = []
    style_header = _style_text(cfg, 'header')
    if style_header:
        lines.append(style_header)
        lines.append("")
    lines.append(f"📊 {date_str} 台股開盤快報")
    pt = snap.get('price_time')
    if snap['price_source'] in ('mis', 'mixed') and pt:
        # The PRICE time — not the build time, which the old header printed and
        # which overstated freshness by up to 20 minutes.
        lines.append(f"🕐 價格時間 {pt.astimezone(tpe):%H:%M:%S}（證交所即時）")
    if snap['delayed_codes']:
        at = snap.get('delayed_at')
        at = f"，最後成交 {at.astimezone(tpe):%m/%d %H:%M}" if at else ''
        if snap['price_source'] == 'yahoo':
            lines.append(f"⚠️ 證交所即時報價暫時無法取得 — 以下全部為 Yahoo 延遲報價{at}，"
                         f"可能仍是前一交易日的數字")
        else:
            lines.append(f"⚠️ 延遲報價：{'、'.join(names.get(c, c) for c in snap['delayed_codes'])}"
                         f"（證交所無即時報價，改用 Yahoo{at}）")
    lines.append("")
    # The dashboard orders the toggles it shows; the index and global lines
    # share one block now, and the total line lives inside the holdings block.
    as_block = {'market_overview': 'overnight', 'global_markets': 'overnight',
                'cost_line': 'holdings'}
    order = [as_block.get(k, k) for k in (cfg.get('sections', {}).get('morning_order') or [])]
    for key in _resolve_section_order(order, ['overnight', 'holdings', 'yesterday']):
        if key in blocks:
            lines.extend(blocks[key])

    style_footer = _style_text(cfg, 'footer')
    if style_footer:
        lines.append("")
        lines.append(style_footer)

    report = "\n".join(lines).rstrip() + "\n"
    holdings_data = [
        _stock_row(h['code'], h['name'], h['price'], h['change'], h['pct'], None, None, '',
                   (cfg.get('notes') or {}).get(str(h['code']), ''))
        for h in snap['holdings']]
    record = _build_pool_record(
        'morning', date_str, snap['taiex_pct'],
        pf_daily_total, pf_gain_total, pf_cost_total,
        holdings_data, [], ai_summary='', ai_commentary='', news=[])
    _save_and_print(report, DATA_DIR, 'twse_daily_report.md', mode='morning', record=record)
    return report


# ---------------------------------------------------------------------------
# Closing report (Mode B) — TWSE official + yfinance technicals
# ---------------------------------------------------------------------------

def generate_closing_report():
    # ── Everything below is the dashboard's. This function renders; it does not
    # fetch and it does not calculate. One call, one set of numbers. ──
    snap = dashboard.snapshot('closing')
    if snap is None:
        return None
    cfg            = snap['cfg']
    tracked_notes  = snap['tracked_notes']
    portfolio      = snap['portfolio']
    date_str, time_str = snap['date_str'], snap['time_str']
    output_dir     = DATA_DIR
    ai_cfg         = cfg.get('ai', {})
    technicals_cfg = cfg.get('technicals', {})
    sections       = cfg.get('sections', {}).get('closing', {})
    taiex, taiex_pct = snap['taiex'], snap['taiex_pct']
    global_lines   = snap['global_lines']
    _calc          = snap['positions']
    data_is_stale  = snap['is_stale']
    twse_date_raw  = snap['feed_date']
    top_volume     = snap['hotlist']['top_volume']
    top_losers     = snap['hotlist']['top_losers']
    brave_headlines = snap['news']

    hold_ctx, watch_ctx = snap['holdings'], snap['watchlist']
    holding_sections = [l for code, name in snap['missing_holdings']
                        for l in (f"• **{name} ({code})**：今日無交易數據", "")]
    watch_sections   = [l for code, name in snap['missing_watch']
                        for l in (f"• **{name} ({code})**：今日無交易數據", "")]
    holdings_data, watch_data = [], []
    # The closing totals now come from the SAME compute_positions() the board and
    # the morning push use — they used to be re-accumulated here from TWSE rows.
    pf_daily_total = _calc['total']['daily_pnl']
    pf_gain_total  = _calc['total']['pnl']
    pf_cost_total  = _calc['total']['cost']

    # One batched AI call for every 原因 (holdings + watchlist) — cross-stock aware.
    # Its only inputs are the dashboard's numbers: the analysis reasons about
    # what the board shows, never about data fetched behind the board's back.
    print(f"[{_now()}] [CLOSING] AI 原因 (batched, {len(hold_ctx) + len(watch_ctx)} stocks)...")
    _reasons = ai_stock_reasons_batch(hold_ctx + watch_ctx, taiex_pct, cfg)

    # Pass 2 — render holdings
    for c in hold_ctx:
        code, name, row = c['code'], c['name'], c['row']
        reason = _reasons.get(code, '')
        line = (
            f"• **{name} ({code})**：[開盤] {c['open_p']}元 → [收盤] {c['close_p']}元"
            f"（{_chg_text(row['_close'], c['change'])}）"
            f" [成交量: {c['zhang']}張]{_src_tag(row)}"
        )
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
            f"（{_chg_text(row['_close'], c['change'])}）"
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

    hot_title = ("🔥 **市場熱點掃描：**" if data_is_stale else "🔥 **今日市場熱點掃描：**")
    hot = [hot_title]
    if data_is_stale:
        hot.append(f"（全市場排行為最近已發布交易日 {twse_date_raw}）")
    hot.append("*成交量前五：*")
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
        if _cash_in_push(cfg):
            hold.extend(_cash_lines(_calc['total']))
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
        lines.append(f"⚠️ 注意：TWSE全市場檔尚未發布今日數據（最新為 {twse_date_raw}）。"
                     f"持倉與觀察清單改用即時報價，熱點排行仍為 {twse_date_raw}。")
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
                           if not l.lstrip().startswith(("📝", "🏦", "⚠️ 現金")))
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
    record.update(_cash_record(_calc['total']))
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

    # Watchlist is intentionally EXCLUDED from the analyst summary (2026-07-08 spec):
    # the AI analysis covers only the market overview + holding positions.
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


def _cash_lines(total):
    """🏦 brokerage cash, total wealth and the stock/cash split — or nothing at
    all when no cash ledger exists, so a cash-free push is byte-identical to the
    pushes before cash existed.

    Placed AFTER the 💰 line and its ⚠️ notes on purpose: the voice reads the 💰
    line, then ⚠️ lines, and stops at the first other line, so cash is never
    spoken; and the closing AI input scrubs 🏦 / ⚠️ 現金 lines. Cash never enters
    a performance number — only a balance, a total and a share of that total."""
    if total.get('cash') is None:
        return []
    line = f"🏦 交割戶現金 {total['cash']:,.0f}元（{total.get('cash_as_of') or '?'} 更新）"
    if total.get('allocation_valid'):
        line += (f" ｜ 總資產 {total['total_wealth']:,.0f}元"
                 f"（股票 {total['equity_pct']:.1f}% · 現金 {total['cash_pct']:.1f}%）")
    else:
        line += " ｜ 總資產暫不顯示（尚有持股無報價）"
    out = [line]
    if total.get('cash_stale'):
        out.append(f"⚠️ 現金餘額已 {total['cash_age_days']} 天未更新 — 總資產與比例可能失真")
    if total.get('cash_bad_lines'):
        out.append(f"⚠️ 現金帳本有 {total['cash_bad_lines']} 行無法讀取，餘額僅計可讀部分")
    return out


def _cash_in_push(cfg):
    """Whether the 🏦 cash line goes out in the Telegram pushes. OFF unless
    bot_config cash.in_push is true: Peter's rule (2026-10-02) is that his cash
    never goes on the internet, and Telegram is the internet. The dashboard shows
    cash regardless, and the closing pool record keeps the day's wealth locally."""
    return bool(((cfg or {}).get('cash') or {}).get('in_push', False))


def _cash_record(total):
    """The day's wealth, for the closing pool record — the history that a future
    total-wealth chart and return will be built on. Empty with no ledger, so the
    record keeps its old shape; metrics.csv is never widened."""
    if total.get('cash') is None:
        return {}
    return {
        'cash': round(total['cash'], 2),
        'cash_as_of': total.get('cash_as_of'),
        'cash_net_flow': round(total.get('cash_net_flow') or 0.0, 2),
        'total_wealth': (round(total['total_wealth'], 2)
                         if total.get('total_wealth') is not None else None),
    }


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
