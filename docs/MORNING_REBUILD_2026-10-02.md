# Morning push rebuild — 09:05 Taipei, exchange real-time prices

Authoritative build contract. Written 2026-10-02 after a mapping/adversarial pass that produced 21
distinct high-severity findings, most of them contract mismatches between independently-written
plans. **Implement from this file, not from any other plan.** Every field claim below was measured
against the live endpoint from this Mac on 2026-10-02 (market closed) — the probe is reproduced at
the bottom.

## Why

Yahoo's TWSE quotes are on a hard ~20-minute delay. Measured 2026-09-30: at 09:15–09:19 Taipei
every quote was still stamped with the previous session's 13:30 close and `interval='1m'` returned
"no price data found"; the feed rolled to today only at **09:20:37**, with its first stamp for today
being 09:00:36. So the old 09:30 push was built on ~09:10 prices, and a 09:00 push would have been
built on 100% of yesterday's. Measured consequence that morning: the push printed 加權指數 47,767 while the exchange's own
live figure was 48,291, and a large-cap holding was shown at +1.32% when the truth was +3.36%.

The exchange publishes its own real-time feed, free, and it works from this Mac. At 09:05 that is
the only usable source.

## Measured field semantics — `mis.twse.com.tw/stock/api/getStockInfo.jsp`

Request: `?ex_ch=tse_t00.tw|tse_2330.tw|...&json=1&delay=0`, pipe-separated, `tse_` listed /
`otc_` OTC. Requires a browser `User-Agent` **and** `Referer: https://mis.twse.com.tw/stock/index.jsp`.

| Field | Meaning | Trap |
|---|---|---|
| `c` | bare code | **the index arrives as `c == "t00"`**, not `_t00` |
| `z` | last price | `"-"` between matching intervals and before a stock's first match |
| `y` | previous close | authoritative 昨收; use this, never a history frame |
| `o` | today's open | `"-"` before the first match |
| `h` / `l` | today's high / low | — |
| `v` | cumulative volume, 張 | **absent on the index** |
| `d` + `t` | session date + **last-trade time** | **this is the price time** |
| `tlong` | ms epoch | **for stocks this is the 14:30 盤後定價 stamp, NOT the price time** |
| `ot` / `oz` | after-hours fixed-price time / price | not the regular-session close |
| `b` / `a` | bid / ask ladders, `_`-joined | — |

Measured proof of the `tlong` trap: `2330` returned `t='13:30:00'`, `ot='14:30:00'`,
`tlong=1790922600000` → **14:30:00**. The index returned `t='13:33:00'`,
`tlong=1790919180000` → 13:33:00 (matches `t`, because the index has no after-hours session).
**So quote_at and `intraday` are derived from `d`+`t`, never from `tlong`.**

Three failure shapes, all measured:
- **Unservable code** (`3595`, on both `tse_` and `otc_`): a junk row `{"c":"", "z":"-", "s":"-",
  "tv":"-"}` with everything else absent — and `rtcode` is still `"0000"`. Skip any row with an
  empty `c`; misses are `requested − resolved`.
- **Empty `ex_ch`**: `{"rtcode":"9999","rtmessage":"參數不足"}`.
- **Malformed `ex_ch`**: ~20 bytes of newlines and **no JSON at all** — `json.loads` raises.

And two things that look like a trading-day signal but are not:
- `rtcode` is `"0000"` with the market shut.
- `queryTime.sysDate`/`sysTime` is the **server wall clock** (measured `20261002 17:32:50` after the
  close). Only `d`/`t` carry the session.

## Contract 1 — the quote dict

`fetch_mis_quotes(codes, want_index=False, timeout=20, chunk=40) -> {code: quote}`, inserted as a
NEW function after `fetch_live_quotes()`. **Do not modify `fetch_live_quotes()`** — the board calls
it 3×/minute and the closing push calls it on the stale path.

Every quote carries exactly the keys `fetch_live_quotes` emits, plus three:

```
name, price, prev_close, today_open, change, intraday_change, quote_at, intraday   # existing shape
src          : 'mis' | 'yahoo' | 'official'      # NEW — the one provenance key. Not 'source', not 'quote_src'.
exch_open    : float | None                      # NEW — today's open from the exchange
session_date : 'YYYYMMDD' | None                 # NEW
```

- `quote_at` = `datetime.strptime(d + t, '%Y%m%d%H:%M:%S')` localised to `Asia/Taipei`, then
  `.astimezone()` to match `_quote_time()`'s convention.
- `intraday` = `session_date == today in Taipei` **and** `t <= 13:35`.
- Every numeric field goes through one helper: `_f(v) -> float(v) if v not in (None,'','-') else None`.
- The whole per-row parse sits in `try/except`; a row that raises is skipped.
- **A code whose `price` or `prev_close` is None is NOT emitted at all.** It must land in `missing`
  and take the Yahoo fallback leg. (Proven crash otherwise: `compute_positions` guards only
  `if not q`, so a dict with `price=None` is truthy and reaches `price - prev` →
  `TypeError: unsupported operand type(s) for -: 'NoneType' and 'float'`.)
- Price resolution order: `z`, else top of the `b` bid ladder, else `o`. **Never 0.**
- The index is special-cased **before** the generic keying: the row with `c == 't00'` is parsed and
  stored under `'_t00'`, then `continue`. Verification must assert that `fetch_mis_quotes(['2330'],
  want_index=True)` contains `'_t00'` and contains neither `'t00'` nor `'tse_t00'`.
- Symbol prefix (`tse_`/`otc_`/`none`) is cached in `data/mis_prefix_cache.json`; unknown codes probe
  `tse_` then `otc_` once. **Pre-register `3595` as `'none'`** so it never costs a probe and never
  triggers the delayed-prices warning.
- Transport: `requests.get(..., headers=..., timeout=...)`, and on any exception retry once through
  `subprocess.run(['curl','-sS','-m',...])`. 19 symbols in one call measured at 1.33 s.

## Contract 2 — the TAIEX dict

`fetch_taiex()` today returns exactly `{'close','open','change','pct'}` — no `prev_close`, no
`last`. The closing renderer reads `taiex['close']`.

For `mode == 'morning'` only, `snapshot()` overrides it from the `_t00` quote and emits **six** keys:

```
close, last, open, prev_close, change, pct      # 'close' and 'last' hold the same live value
```

`close` is kept so the closing branch and any other reader are untouched; `last` is what the new
🌙 block prints as 現. When the `_t00` quote is absent, fall back to `fetch_taiex()` and synthesise
`prev_close = close - change` and `last = close`, and set the delayed warning.
Verification must assert `set(snap['taiex']) >= {'close','last','open','prev_close','pct'}`.

## Contract 3 — `close_streak`

ONE implementation, four positional parameters:
`close_streak(closes, market_open=None, today=None, flat_rule=None) -> {'run','pct','as_of'} | None`

- `as_of` = the date of the newest close actually counted. The renderer prints the streak as "through
  yesterday's close" and needs this to be honest.
- `flat_rule` resolves from `bot_config` `streak.flat_rule`, **defaulting to `'break'` — today's
  exact behaviour** (a flat newest close returns `run 0`; a flat day mid-run stops the backward
  walk). The alternative `'skip'` makes a flat day neither count nor break; only a real reversal
  ends a run. Peter has not chosen, so the default must not change what he already relies on.
- Under `'skip'`, track the run's start index explicitly — the existing `start = vals[-1 - run]`
  computes the wrong Run% once flat days are passed over.
- `live_portfolio.py` calls this and **must keep behaving identically**, so the new parameters are
  keyword-defaulted and the default path is byte-identical in behaviour.

Marker threshold comes from config (`streak.marker_threshold`, falling back to
`streak_alert.threshold`, then 3). The renderer must not hardcode it.

## Contract 4 — snapshot provenance

`snapshot('morning')` adds to its returned dict:

```
price_source    : 'mis' | 'yahoo' | 'mixed'
delayed_codes   : [codes that fell back to Yahoo]   # scoped to codes actually RENDERED
price_time      : tz-aware Asia/Taipei datetime     # the newest exchange price stamp
exchange_quoting: True | False | None
yesterday       : the previous closing pool record, for 📌 昨日重點
```

**Routing (critical):** today's line is `use_live = (mode == 'morning') or is_stale`, and `is_stale`
is regularly True at 16:00 when MI_INDEX has not published yet. Putting the new feed inside that
branch **would change the closing push**. Restructure as `if mode == 'morning': ... elif is_stale or
...:` with the existing closing code kept verbatim in the second branch.

`_context()` must emit `open_p` as a **float** (from `exch_open`). Today it is the literal string
`'N/A'` on every morning run, because `snapshot` sets `rows = {}` for morning — so the new
昨收 → 開 → 現 line would crash on it.

Morning must not fetch what it cannot use: gate `fetch_valuation()` / `fetch_margin()` to closing,
and skip `fetch_stock_technicals` when `opening_mode`. **Keep emitting `rsi: None` and
`vol_ratio: None`** — `_stock_row`, `_rsi_state` and `_summary_digest` all index those keys, and
deleting them raises where None does not.

## Contract 5 — the trading-day gate

`exchange_is_quoting()` returns three states, and the caller must respect all three:
- `True` — a well-formed index row whose session date equals today in Taipei.
- `False` — a well-formed index row with a parseable **earlier** date. This is the 颱風假 case.
- `None` — anything else (rtcode ≠ 0000, non-JSON body, empty `msgArray`, junk row, decode failure).
  **Unknown is not a closure.** Never skip a push on `None`.

It must be callable from exactly one place: a thin `morning_gate()` invoked **before** `snapshot()`.
It must NEVER be reachable from `taiwan_market_open()`, `is_trading_day()` or
`market_holiday_name()` — `taiwan_market_open()` is on the board's hot path at ~4 renders/second,
and one 1.3 s HTTP call there freezes the dashboard.

`generate_morning_report()` also needs the `if snap is None: return None` guard that
`generate_closing_report()` already has — that is a pre-existing one-line hole.

When the gate skips the push, **say so on Telegram**. A silent skip is indistinguishable from a
crashed bot.

## Contract 6 — the report layout

Four blocks. The marker sits at **column zero** on flagged holdings (no `• ` prefix), because the
voice selector and the renderer must agree on where it is.

```
📊 {date} 台股開盤快報
🕐 價格時間 {HH:MM:SS}（證交所即時）        ← the PRICE time, not the build time

🌙 隔夜與開盤：
• 加權指數：昨收 {prev_close} → 開盤 {open} → 現 {last}（{pct:+.2f}%）
• S&P 500 / Nasdaq / 費城半導體 / 日經225   ← unchanged overnight lines

🎯 持倉（今日）：
💰 今日損益 {x}元 ｜ 總損益 {y}元 ({z:+.1f}%)

⚡ 範例甲 (1111)：10.00 → 開 10.10 → 現 10.30　+3.00%　今日 +300元
     連漲 5 日 +9.15%（至昨收）→ 今日 +3.00%，連漲延續第 6 天 → 賣出觀察
⚠️ 範例乙 (2222)：100.00 → 開 101.00 → 現 100.50　+0.50%　今日 +500元
     連跌 4 日 -2.32%（至昨收）→ 今日 +0.50%，連跌中斷 → 止跌觀察
• 範例丙 (3333)：50.00 → 開 50.20 → 現 50.40　+0.80%　今日 +40元
     連跌 1 日 -0.06%（至昨收）

📌 昨日重點（承接 {date} 收盤報告）：
• 2–3 factual carried-over lines
```

- `⚡` = a run of ≥ threshold still extending. `⚠️` = a run of ≥ threshold breaking. Below threshold
  gets `•` and no verdict tail.
- Wording is 連漲延續 / 連跌延續 / 連漲中斷 / 連跌中斷, always "延續/中斷" rather than a settled
  claim, because at 09:05 the day has five minutes on it and can reverse by 13:30. **Today's move is
  never folded into the run count.**
- `（至昨收）` on every streak so the number is never read as including today.
- A holding with no price yet renders as `尚未成交` and is excluded from the totals, which is already
  what `compute_positions` does for an unpriced holding (it excludes that holding's cost too).
- The 💰 line stays under its existing `sections.cost_line` config gate.
- 📌 昨日重點 is built from the previous closing pool record via a new
  `dashboard.last_pool_record('closing')`. **`taiex_close` does not exist in any of the 65 closing
  pool records** — build the block from `taiex_pct` and the holdings' own `pct` values, or add the
  field to `_build_pool_record` as a deliberate, separate decision. `_POOL_CSV_COLS` must be left
  alone so `metrics.csv` stays byte-compatible.

## Cut from the morning push

`ai_stock_reasons_batch`, `ai_morning_outlook`, `ai_report_summary`, and every yesterday-sourced
indicator (RSI, 量比, 融資, PE). All of it either duplicates the 16:00 push — which publishes the
same figures from final data the evening before — or is speculation about a day that has not
happened. The morning push should make **no OpenRouter call at all**. Remove them from
`generate_morning_report()` only; the closing function keeps every one.

`config_tui.py`'s `MORNING_SECTION_LABELS` still offers 「AI 報告總結」 as a morning toggle and must
drop it.

## Voice

`for_speech()` currently selects by prefix. Measured against a mock of the new layout it returned
**430 of 430 characters — the entire report**, which is the 9-minute bubble that was deliberately
removed on 2026-09-11. The morning selector must be rewritten to take: the date line, the index
line, the 💰 total, and the run fragments — and it must match the markers by **substring**, not
`startswith`, or anchor on 連漲/連跌. Verification must assert the spoken slice is shorter than the
report and contains both 💰 and a run marker. **The 16:00 push's spoken content must be
byte-identical to today's.**

## Scheduler

`morning_utc` `01:30` → `01:05` in `data/bot_config.json`, and the fallback in `scheduler.py`.
Restart only outside the 01:05 / 08:00 / 08:05 UTC slots — a restart kills a push mid-flight.
`catch_up_missed` dedupes on the archived report file, so the changeover cannot double-send.
The 16:05 streak alert stays running, untouched.

Stale strings to update: `twse_daily_report.py` module docstring line 3 (both the time **and** the
price-source sentence), `sandbox_run.py` banner, `README.md` lines ~145 and ~277.

## Decisions taken without an answer (all reversible, all told to Peter)

1. The 16:05 streak alert stays running and untouched.
2. Morning voice reads date + index + total + runs.
3. Feed failure at 09:05 → still send, on the delayed source, with a loud warning line. Never
   present stale prices as live.
4. Flat-close rule defaults to today's behaviour behind a config switch.

## Not yet measurable

Everything above was measured with the market **closed**. Two gaps remain until Monday 2026-10-05:
the in-session behaviour of `z`/`o`/`b` before a stock's first match (the actual 09:05 case), and
the gate's reading on a true non-trading day. There is **no test suite anywhere in this project**
(no `test_*.py`, no `conftest.py`), so the verification harness below is the only safety net.

## Verification harness

`tools/verify_morning.py` must, with no network dependency on being in-session:
1. Feed `fetch_mis_quotes`' parser a synthetic row with `z='-'`, `o='-'`, empty `b` → assert the
   code is dropped, not emitted with `price=None`.
2. Assert `'_t00'` is present and `'t00'` is absent from a `want_index=True` result.
3. Assert `set(snap['taiex']) >= {'close','last','open','prev_close','pct'}`.
4. Assert `isinstance(c['open_p'], float)` for every morning holding context.
5. Run `close_streak` under both `flat_rule` values against the real cached closes and assert the
   `'break'` default reproduces today's numbers exactly.
6. Render the morning report in sandbox mode and assert: no OpenRouter call was made, the header
   carries the price time, a run marker is at column zero, and the spoken slice is shorter than the
   report and contains a run marker.
7. Assert `exchange_is_quoting()` returns `None` rather than `False` for a non-JSON body.

## Probe reproduced (2026-10-02 17:32 Taipei, market closed)

```
t00    : z=48475.74 y=48353.49 o=48390.65 d=20261002 t=13:33:00 tlong→13:33:00  v=None
2330   : z=2500.00  y=2510.00  o=2505.00  d=20261002 t=13:30:00 tlong→14:30:00  ot=14:30:00 oz=2505.00 v=13869
3595   : junk row — c='' z='-' s='-' tv='-', on BOTH tse_ and otc_, rtcode still 0000
rtcode=0000, queryTime=20261002 17:32:50 (server wall clock, not the session)
```

## As built (2026-10-02, same evening)

Where the build departed from the plan above, and what it found on the way:

- **The morning push has its own builder, `dashboard._morning_snapshot()`.** `snapshot('morning')`
  returns it on its first line, and every line of `snapshot()` below that is the closing path,
  unchanged. So `_context()` was never touched — the morning holding dicts carry `open_p` as a float
  directly, and the `use_live = (mode == 'morning') or is_stale` hazard cannot reach the closing push.
- **`fetch_mis_quotes(..., unmatched=[])`** reports codes the exchange answered for but could not
  price yet. Those render as 尚未成交 and are NOT filled from Yahoo: at 09:05 Yahoo has not rolled
  over, so it would put yesterday's move into today's figures. Only codes the exchange did not answer
  for at all fall through to Yahoo, flagged （延遲）.
- **Yahoo's daily history drops whole sessions for ETFs — measured, not suspected.** Over 65 sessions,
  every ETF checked was missing 2026-10-01 and the ordinary stocks checked missed nothing. On that
  day's data most streaks were wrong, and one pointed the wrong way (Yahoo-only +1, true −1). `repair_daily_closes()` fills dropped sessions from TWSE MI_INDEX (one call
  per missing date, cached in `data/official_close_cache.json`) and pins the newest session to the
  exchange's own 昨收. **The live board's Streak column and the 16:05 streak alert still read the
  unrepaired Yahoo series** — left as they were, by agreement; offered as a follow-up.
- **The scheduler runs the report with `TZ=UTC`** (`scheduler.py` sets it). Every time the morning push
  displays is converted to Asia/Taipei explicitly. The closing push's header still prints the build
  time in UTC — e.g. 「（08:02 數據）」 for a 16:02 run — a pre-existing bug left untouched because the
  closing push is byte-identical by design.
- **Gate:** `dashboard.morning_gate()` runs before the snapshot; it is a no-op before 09:00 Taipei and
  on non-trading days, and re-asks twice, 60 s apart, before it ever skips. A skip archives and sends a
  one-line notice.
- **Cash** lives in an append-only `data/cash_ledger.jsonl` (types open / deposit / withdraw / update,
  signed amounts, a note on every entry) and is entered only from the dashboard (`run_tui.sh` →
  Portfolio → `[c] Cash`, also offered after every CSV import). With no ledger, the closing report, its
  voice and its pool record were proven byte-identical to the pre-build output, and the live board's
  rendering identical to the old code. With a ledger: one 🏦 line after 💰 in both pushes, never
  spoken, scrubbed from the AI input; four pool keys (cash, cash_as_of, cash_net_flow, total_wealth)
  on closing records only; `metrics.csv` unchanged.
- **Verification:** `tools/verify_morning.py` — 44 checks, all passing, on a throwaway data copy.

## Follow-up the same night — opening-gap arrows, and the board's streak repaired

Peter's case: a holding on a long rising run that opens below yesterday's close is a signal to
re-adjust during the session. Measured before building (Yahoo daily OHLC, his holdings):

- The open's direction (vs the previous close) matched the close's direction on 77% of 1,222
  holding-days — but only 53% when the gap was under 0.3% (a coin flip), and 81% at 0.3% or more.
- A run of 2+ that opened against itself broke by the close 76–78% of the time; one that opened
  with itself carried on 73–80%.
- A 4+-day up-run that opened ≥0.3% lower broke by the close in 37 of 41 cases (90%, two years).

What shipped:
- **Live board Streak column:** a ▲ (red) / ▼ (green) after the count when today's open is ≥0.3%
  from yesterday's close (`bot_config` `streak.open_gap_min_pct`), shown 09:00–13:30 only — at the
  close the count itself absorbs today. Opens come from the exchange via `dashboard.opening_gaps()`
  (Yahoo shows yesterday's open until ~09:20), asked every 30 s until every holding has opened.
- **The board's count is now repaired** like the morning push's: `dashboard.settled_closes()` counts
  through the last settled session (yesterday during the session, today after 13:30) on closes with
  Yahoo's dropped sessions filled from MI_INDEX. **The 16:05 streak alert still counts on Yahoo
  as-is** — unchanged by agreement.
- **The morning push's ⚡ / ⚠️ verdict now also needs a ≥0.3% move**; below it the line reads
  「→ 今日 ±x%，變動不大」 instead of calling a coin flip.
- Checks: `tools/verify_morning.py`, 53 passing.
