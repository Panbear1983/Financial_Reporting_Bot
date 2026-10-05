#!/usr/bin/env python3
"""Month-end total-wealth push: one picture, a short text, a voice bubble.

Peter, 2026-10-05: "the cash sitting in a reserve should also be part of the
entire portfolio ... but that should not be taking into the stock gain, but
merely just adding a piece of information" — and the push should "fire to group
chat on telegram at the last day of each month (regardless of holiday or not)",
backed by "text and voice message".

Reads the daily closing snapshots through dashboard.wealth_history() and renders
nothing it cannot stand behind:

  * the stock-value line runs back to the first snapshot (July 2026);
  * the total-wealth line starts on 2026-10-05, the first day cash was recorded —
    it is NEVER backfilled with a balance that was not written down at the time;
  * the only "growth" shown is total wealth minus the money Peter put in himself
    (deposits − withdrawals). Cash never enters a performance percentage.

On a month whose last calendar day is a weekend or holiday, the figures are the
last trading day's close, and the header says which day that was.

    python monthly_wealth.py                 # this month to date, send
    python monthly_wealth.py --month=2026-10 # a specific month
    python monthly_wealth.py --dry-run       # build + print, send nothing
    SANDBOX_MODE=true python monthly_wealth.py   # full delivery dry run (preview files)
    python monthly_wealth.py --to=<chat_id>      # a TEST send to one chat: ignores the
                                                 # enabled switch, archives nothing
"""
import io
import os
import sys
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dashboard                                          # noqa: E402
from dashboard import _now, DATA_DIR                      # noqa: E402
import twse_daily_report as tdr                           # noqa: E402

# Traditional-Chinese-capable fonts, in order of preference, as shipped on this
# Mac. matplotlib's bundled DejaVu has no CJK glyphs — without one of these every
# Chinese label on the picture renders as an empty box.
_CJK_FONTS = (
    '/System/Library/Fonts/PingFang.ttc',
    '/System/Library/Fonts/Hiragino Sans GB.ttc',
    '/System/Library/Fonts/STHeiti Medium.ttc',
    '/System/Library/Fonts/Supplemental/Arial Unicode.ttf',
    '/System/Library/Fonts/Songti.ttc',
    '/Library/Fonts/NotoSansCJKtc-Regular.otf',
)


def cjk_font_path():
    return next((p for p in _CJK_FONTS if os.path.exists(p)), None)


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------

def last_day_of_month(day):
    """The last calendar day of `day`'s month — holiday or not, by Peter's choice."""
    nxt = (day.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
    return nxt - datetime.timedelta(days=1)


def is_last_day_of_month(day):
    return day == last_day_of_month(day)


def previous_month(day):
    return (day.replace(day=1) - datetime.timedelta(days=1)).strftime('%Y-%m')


def taipei_today():
    return datetime.datetime.now(dashboard.ZoneInfo('Asia/Taipei')).date()


# ---------------------------------------------------------------------------
# Figures
# ---------------------------------------------------------------------------

def _fmt(v):
    return f'{v:,.0f}'


def _signed(v):
    return f'{v:+,.0f}'


def _month_rows(history, month):
    return [r for r in history if r['date'].startswith(month)]


def summarise(history, month):
    """Everything the text and the picture print for `month`, derived once.

    Returns None when the month has no snapshot at all. 'prev' is the previous
    month-end row (or None); change figures are None whenever one side of the
    comparison lacks the number — a missing comparison is printed as missing,
    never as zero.
    """
    rows = _month_rows(history, month)
    if not rows:
        return None
    last = rows[-1]
    month_ends = dashboard.month_end_rows(history)
    prev = next((r for r in reversed(month_ends) if r['month'] < month), None)

    out = {
        'month': month, 'date': last['date'], 'stocks': last['stocks'], 'cost': last['cost'],
        'cash': last['cash'], 'total_wealth': last['total_wealth'],
        'net_flow': last['net_flow'], 'cash_as_of': last['cash_as_of'],
        'stock_pnl': last['stocks'] - last['cost'],
        'stock_pnl_pct': ((last['stocks'] / last['cost'] - 1.0) * 100) if last['cost'] else None,
        'prev': prev,
        'stocks_change': (last['stocks'] - prev['stocks']) if prev else None,
        'wealth_change': None, 'put_in': None, 'market_change': None,
        'equity_pct': None, 'cash_pct': None,
        'wealth_since': next((r['date'] for r in history if r['total_wealth'] is not None), None),
    }
    tw, cash = last['total_wealth'], last['cash']
    if tw and cash is not None:
        out['equity_pct'] = last['stocks'] / tw * 100
        out['cash_pct'] = cash / tw * 100
    if prev and tw is not None and prev.get('total_wealth') is not None:
        out['wealth_change'] = tw - prev['total_wealth']
        if last['net_flow'] is not None and prev.get('net_flow') is not None:
            out['put_in'] = last['net_flow'] - prev['net_flow']
            out['market_change'] = out['wealth_change'] - out['put_in']
    return out


def build_text(s, month_end=True):
    """The Telegram text. Short on purpose: it goes WITH a picture and is read
    aloud in full by the voice bubble."""
    label = '月結總資產' if month_end else '總資產月中快照'
    d = s['date'][5:].replace('-', '/')
    lines = [f"📈 {s['month']} {label}"]
    as_of = f"🕐 資料截至 {d} 收盤"
    if s['cash_as_of']:
        as_of += f"（現金 {s['cash_as_of'][5:].replace('-', '/')} 更新）"
    lines += [as_of, '']
    if s['total_wealth'] is not None and s['equity_pct'] is not None:
        lines.append(f"💼 總資產 {_fmt(s['total_wealth'])}元"
                     f"（股票 {s['equity_pct']:.1f}% · 現金 {s['cash_pct']:.1f}%）")
        lines.append(f"📊 股票市值 {_fmt(s['stocks'])}元 ｜ 🏦 現金 {_fmt(s['cash'])}元")
    else:
        lines.append(f"📊 股票市值 {_fmt(s['stocks'])}元（本月尚無現金紀錄，總資產不顯示）")
    pct = f"（{s['stock_pnl_pct']:+.1f}%）" if s['stock_pnl_pct'] is not None else ''
    lines.append(f"📊 股票損益 {_signed(s['stock_pnl'])}元{pct}，以持股成本計")
    prev = s['prev']
    if prev:
        pd_ = prev['date'][5:].replace('-', '/')
        if s['wealth_change'] is not None:
            line = f"📅 本月總資產 {_signed(s['wealth_change'])}元（上月底 {_fmt(prev['total_wealth'])}元，{pd_}）"
            if s['put_in'] is not None:
                line += f"\n　　自行存入 {_signed(s['put_in'])}元 ｜ 市場增減 {_signed(s['market_change'])}元"
            lines.append(line)
        else:
            lines.append(f"📅 本月股票市值 {_signed(s['stocks_change'])}元（上月底 {_fmt(prev['stocks'])}元，{pd_}）")
            if s['wealth_since']:
                lines.append(f"　　總資產自 {s['wealth_since'][5:].replace('-', '/')} 起記錄，首月不作月比")
    else:
        lines.append("📅 首次月結，尚無上月可比")
    return '\n'.join(lines)


def _draw_bars(ax, history, s, months_back, font, month_end):
    """Month-to-month comparison, Peter's ask (2026-10-05): one bar per month-end.

    Stacked: stocks (blue) with cash (grey) on top, so the bar's height IS total
    wealth — and months before cash was recorded are stocks-only bars, which is
    exactly what was known then. Under each month: the change in STOCK value vs
    the previous month (always comparable), and the change in total wealth only
    when both months carry it. Oct-vs-Sep is never shown as "+887k" when 486k of
    that is cash being written down for the first time."""
    rows = [r for r in dashboard.month_end_rows(history) if r['month'] <= s['month']][-months_back:]
    if not rows:
        return
    x = list(range(len(rows)))
    stocks = [r['stocks'] for r in rows]
    cash = [r['cash'] or 0.0 for r in rows]
    ax.bar(x, stocks, color='#1f4e9c', width=0.62, label='股票市值')
    ax.bar(x, cash, bottom=stocks, color='#95a5a6', width=0.62, label='現金（自 2026-10 起記錄）')
    top = max(st + c for st, c in zip(stocks, cash))
    ax.set_ylim(0, top * 1.22)
    for i, r in enumerate(rows):
        total = r['stocks'] + (r['cash'] or 0.0)
        label = f"{total:,.0f}" if r['cash'] is not None else f"{r['stocks']:,.0f}\n（僅股票）"
        ax.text(i, total + top * 0.015, label, ha='center', va='bottom', fontproperties=font,
                fontsize=10, color='#222')
        if i > 0:
            prev = rows[i - 1]
            ds = r['stocks'] - prev['stocks']
            pct = (ds / prev['stocks'] * 100) if prev['stocks'] else 0.0
            note = f"股票 {ds:+,.0f}（{pct:+.1f}%）"
            if r['cash'] is not None and prev['cash'] is not None:
                dt = total - (prev['stocks'] + prev['cash'])
                note += f"\n總資產 {dt:+,.0f}"
            colour = '#c0392b' if ds > 0 else ('#27ae60' if ds < 0 else '#555')   # TW: red up
            ax.text(i, -top * 0.035, note, ha='center', va='top', fontproperties=font,
                    fontsize=9, color=colour)
    labels = []
    for r in rows:
        lab = r['month']
        if r['month'] == s['month'] and not month_end:
            lab += f"\n(至 {s['date'][5:]})"
        labels.append(lab)
    ax.set_xticks(x)
    ax.set_xticklabels(labels, fontproperties=font, fontsize=10)
    ax.tick_params(axis='x', pad=34)                 # room for the change notes
    ax.set_title('月底總資產比較', fontproperties=font, fontsize=13, loc='left')
    ax.legend(prop=font, loc='upper left', frameon=False, fontsize=9)
    ax.grid(True, axis='y', color='#e6e6e6', linewidth=0.8)
    ax.set_axisbelow(True)


def _draw_lines(ax, history, s, months_back, font):
    """The daily path behind the bars: stock value back to July, total wealth
    from 2026-10-05 (dots while there are few points), and 投入成本."""
    import matplotlib.dates as mdates
    cutoff = (datetime.date.fromisoformat(s['date']).replace(day=1)
              - datetime.timedelta(days=31 * (months_back - 1))).replace(day=1)
    rows = [r for r in history if cutoff.isoformat() <= r['date'] <= s['date']]
    dates = [datetime.date.fromisoformat(r['date']) for r in rows]
    stocks = [r['stocks'] for r in rows]
    wealth = [r['total_wealth'] for r in rows]
    ax.plot(dates, stocks, color='#1f4e9c', linewidth=1.8, label='股票市值（每日）')
    wd = [d for d, v in zip(dates, wealth) if v is not None]
    wv = [v for v in wealth if v is not None]
    if wv:
        few = len(wv) <= 5
        ax.plot(wd, wv, color='#c0392b', linewidth=2.2, label='總資產（每日）',
                marker='o' if few else None, markersize=5)
    first = next((r for r in rows if r['cash'] is not None), None)
    if first:
        base = first['cash'] - (first['net_flow'] or 0.0)
        pd_ = [d for d, r in zip(dates, rows) if r['cash'] is not None]
        pv = [r['cost'] + base + (r['net_flow'] or 0.0) for r in rows if r['cash'] is not None]
        ax.step(pd_, pv, where='post', color='#7f8c8d', linewidth=1.2, linestyle='--',
                label='投入成本（持股成本＋開帳現金＋存入－提出）',
                marker='s' if len(pv) <= 5 else None, markersize=4)
    ax.xaxis.set_major_locator(mdates.MonthLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
    ax.grid(True, color='#e6e6e6', linewidth=0.8)
    ax.legend(prop=font, loc='upper left', frameon=False, fontsize=8)
    ax.set_title('每日走勢', fontproperties=font, fontsize=11, loc='left')


def build_png(history, s, months_back=12, month_end=True, layout='bars+line'):
    """The picture as PNG bytes. layout: 'bars+line' (month-end bars over the
    daily path) or 'bars' (bars only)."""
    import matplotlib
    matplotlib.use('Agg')                              # headless — this runs under launchd
    import matplotlib.pyplot as plt
    from matplotlib import font_manager as fm
    from matplotlib.ticker import FuncFormatter

    font = fm.FontProperties(fname=cjk_font_path()) if cjk_font_path() else fm.FontProperties()
    fmt = FuncFormatter(lambda v, _: f'{v/1e6:.1f}M' if abs(v) >= 1e6 else f'{v:,.0f}')
    if layout == 'bars':
        fig, ax_b = plt.subplots(figsize=(12, 6.5), dpi=110)
        axes = [ax_b]
    else:
        fig, (ax_b, ax_l) = plt.subplots(2, 1, figsize=(12, 9), dpi=110,
                                         gridspec_kw={'height_ratios': [3, 2]})
        axes = [ax_b, ax_l]
        _draw_lines(ax_l, history, s, months_back, font)
    _draw_bars(ax_b, history, s, months_back, font, month_end)
    for ax in axes:
        ax.yaxis.set_major_formatter(fmt)
        for lbl in ax.get_yticklabels() + ax.get_xticklabels():
            lbl.set_fontproperties(font)
        ax.set_ylabel('元', fontproperties=font)
    fig.suptitle(f"總資產　{s['month']} {'月結' if month_end else '月中快照'}　（資料截至 {s['date']}）",
                 fontproperties=font, fontsize=14)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format='png')
    plt.close(fig)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def archive_paths(month):
    arch = os.path.join(DATA_DIR, 'reports')
    return os.path.join(arch, f'wealth_{month}.md'), os.path.join(arch, f'wealth_{month}.png')


def already_sent(month):
    """The .md archive is the once-per-month marker the scheduler keys on."""
    return os.path.exists(archive_paths(month)[0])


def run(month=None, send=True, force=False, to=None):
    """`to` = a Telegram chat id for a TEST send: it overrides the destination,
    ignores the enabled switch (a human typed it), and writes no archive — so a
    test can never make the real month-end look already sent."""
    cfg = dashboard.load_bot_config()
    mcfg = cfg.get('monthly') or {}
    if send and not to and not mcfg.get('enabled', True):
        # The switch gates the REAL scheduled send only. A --dry-run preview or a
        # --to test must work while the schedule is off — that is when they matter.
        print(f"[{_now()}] Monthly wealth push disabled in bot_config.")
        return None
    today = taipei_today()
    month = month or today.strftime('%Y-%m')
    if send and already_sent(month) and not force and not to:
        print(f"[{_now()}] Month-end push for {month} already sent — skipping (use --force to resend).")
        return None

    history = dashboard.wealth_history()
    s = summarise(history, month)
    if s is None:
        print(f"[{_now()}] No closing snapshot for {month} — nothing to report.")
        return None
    month_end = (today >= last_day_of_month(datetime.date.fromisoformat(s['date'])))
    text = build_text(s, month_end=month_end)
    png = None
    try:
        png = build_png(history, s, months_back=int(mcfg.get('months_back', 12)), month_end=month_end,
                        layout=mcfg.get('layout', 'bars+line'))
    except Exception as exc:                                         # noqa: BLE001
        print(f"[{_now()}] Picture skipped: {type(exc).__name__}: {exc}")
        text += "\n⚠️ 本月圖表產生失敗，僅附文字"

    print("\n" + "=" * 60 + "\n" + text + "\n" + "=" * 60)
    if not send:
        if png:
            out = os.path.join(DATA_DIR, f'wealth_preview_{month}.png')
            with open(out, 'wb') as f:
                f.write(png)
            print(f"[{_now()}] Preview picture → {out}")
        return text

    if to:
        os.environ['TELEGRAM_CHAT_ID'] = str(to)      # deliver_report reads it at send time
        cfg = dict(cfg, delivery=None)                 # and never the configured channels
        print(f"[{_now()}] TEST send → chat {to} (no archive, schedule untouched)")
    tdr.deliver_report(text, cfg, photo=png, photo_caption=f"{s['month']} 總資產走勢")

    if os.getenv('SANDBOX_MODE', '').lower() != 'true' and not to:
        md, pngpath = archive_paths(month)
        os.makedirs(os.path.dirname(md), exist_ok=True)
        with open(md, 'w', encoding='utf-8') as f:
            f.write(text)
        if png:
            with open(pngpath, 'wb') as f:
                f.write(png)
        if month_end:
            dashboard.record_month_end(dict(month=month, **{k: s[k] for k in
                ('date', 'stocks', 'cost', 'cash', 'total_wealth', 'net_flow', 'cash_as_of')}))
        print(f"[{_now()}] Archived → {md}")
    return text


if __name__ == '__main__':
    args = sys.argv[1:]
    month = next((a.split('=', 1)[1] for a in args if a.startswith('--month=')), None)
    to    = next((a.split('=', 1)[1] for a in args if a.startswith('--to=')), None)
    run(month=month, send='--dry-run' not in args, force='--force' in args, to=to)
