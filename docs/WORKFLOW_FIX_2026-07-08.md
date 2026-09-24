# Financial_Reporting_Bot — Workflow Fix Draft (2026-07-08)

Running log of the "preview scrapes nothing" investigation and the fixes applied.

## Symptom
Previewing the report (host venv) shows the global-market section entirely empty —
`S&P 500 / Nasdaq / 費城半導體 / 日經225：資料暫時無法取得` — and several watchlist/
portfolio names as `今日無交易數據`. Looked like "nothing gets scraped."

## What is actually happening (diagnosis)
The TWSE/TPEX official feeds are **healthy** — a preview scraped 1369 TWSE + 9976 TPEX
stocks, 1078 valuations, 1279 margin rows, and rendered the full portfolio. The failures
are confined to the **Yahoo Finance** paths:

| Path | Uses | Status |
|---|---|---|
| Prices (closing) | TWSE OpenAPI (`STOCK_DAY_ALL`) via urllib **with custom ssl_context** | ✅ works |
| TAIEX, technicals (RSI/量比), 1M/3M/1Y strips | **yfinance library** (`yf.Ticker.history`) | ✅ works (requests+certifi) |
| Global indices, yfinance price fallback | `custom_stock_lookup.get_yfinance_data` → **raw urllib, no CA bundle** | ❌ broken |

### Root cause #1 — SSL trust (the "empty global indices")
`get_yfinance_data` called `urllib.request.urlopen(...)` with **no SSL context**, so it used
the OS CA store. On the macOS python.org framework venv that store is empty →
`SSL: CERTIFICATE_VERIFY_FAILED` on **every** call → the function swallowed the exception and
returned `None` → `資料暫時無法取得`. Confirmed empirically:
- raw urllib, default ctx → `CERTIFICATE_VERIFY_FAILED`
- raw urllib, **certifi ctx** → `^GSPC` OK 7503.85, `^SOX` OK, `2330.TW` OK
- **Container** (`python:3.11-slim`, has `ca-certificates`) → urllib works. So this bug is
  **host-preview-only**; production container was unaffected. The fix hardens both.

### Root cause #2 — rate-limiting (429)
Yahoo returns `HTTP 429 Too Many Requests` under bursty access. A report fires ~4 indices +
TAIEX + ~30 per-stock technicals in quick succession; the raw urllib path had **no retry and
no backoff**, so a rate-limit window blanked results. (yfinance library path survives better
because it reuses a session.)

### Root cause #3 — per-symbol `.TW` 404s
A few symbols (山太士 3595, 台灣微脂體 7924, etc.) 404 on Yahoo → `今日無交易數據`. Likely
wrong suffix (上櫃/興櫃 need `.TWO`/other) or genuinely absent on Yahoo. Needs better
suffix/ISIN resolution and graceful degradation. **→ handed to subagent audit.**

## Fixes applied (this pass)
1. **`custom_stock_lookup.get_yfinance_data` hardened** (`custom_stock_lookup.py`):
   - certifi-backed `ssl.create_default_context(cafile=certifi.where())` (fallback to default),
     built once at module load → fixes SSL on host + container.
   - realistic desktop UA.
   - retry loop (3 attempts) with linear backoff; **429 honours `Retry-After`** (capped 20s);
     genuine **404 is not retried**.
   - no more silent swallow — set `FRB_DEBUG_FETCH=1` to log why a fetch returned `None`.
2. **`requirements.txt`** — pinned `certifi>=2024.0.0` (was only transitive).

## Verification
- [x] certifi ctx returns live data for ^GSPC/^SOX/2330.TW (host)
- [x] container urllib path already worked (ca-certificates)
- [ ] full host preview re-run after Yahoo rate-limit cools (global indices populate)
- [ ] container rebuild + sandbox dry-run (no push)

## Handed to subagent (thorough audit)
Verify every data source end-to-end and fix remaining bottlenecks — especially #2 (batch the
global-index + technicals fetches to cut call volume / add shared session + caching) and #3
(per-symbol suffix/ISIN resolution so `今日無交易數據` shrinks). See task list.

## Subagent pass (2026-07-08) — remaining bottlenecks fixed

### Decisive empirical finding
On this host the **urllib chart endpoint** (`query1.finance.yahoo.com/v8/finance/chart`,
used by `custom_stock_lookup.get_yfinance_data`) is **hard 429-throttled** — even a *single*
call returns `HTTP 429`. But the **yfinance library path** (`yf.download`, curl_cffi 0.15.0
browser-impersonating session) is **not** throttled: one `yf.download(['^GSPC','^IXIC','^SOX',
'^N225'])` returned all four indices in 1.2 s. So the real fix is to route data through
**batched `yf.download`** and stop leaning on the per-symbol urllib path.

### Bottleneck A — global indices blanked (0/4), 429 on urllib
`fetch_global_indices` fired 4 individual `get_yfinance_data` (urllib) calls → all 429 →
`資料暫時無法取得`. **Fix:** new `_prefetch_indices()` does **one** `yf.download(..., period='5d')`
for every index symbol into `_INDEX_CACHE`; `fetch_global_indices` reads last-two-closes from
the cache (urllib kept only as a last-ditch fallback). Output format unchanged.

### Bottleneck B — per-stock call storm (quote + technicals + 績效 = ~3 Yahoo calls × 30)
A closing/morning run fired ~90 individual Yahoo calls in seconds → 429 → whole sections
blanked (see the 09:02 morning preview: every line `資料暫時無法取得`). **Fix:** new
`prefetch_stock_histories()` does **one** batched `yf.download(period='1y', auto_adjust=True)`
for all tracked codes into `_HISTORY_CACHE` (keyed by bare code). `_ticker_history` now serves
from that cache via `_slice_period` (`'20d'`→`tail(20)`; verified `.history('20d')` returns 20
*trading* rows, so `tail(20)` reproduces the exact RSI/量比 window — values unchanged);
`fetch_stock_technicals` and `fetch_period_returns` ride the same cache. `fetch_yfinance_stock`
now derives its quote from the cached history first (`_quote_from_history`), only falling back
to urllib (now wrapped by `_cached_get_yfinance`, an in-run dedupe cache). `auto_adjust=True`
was chosen deliberately to match yfinance's `Ticker.history()` default so no computed value
shifts. Net: ~90 calls → ~2 batched calls; no 429.

### Bottleneck C — `.TW` 404 waste + suffix cache unused in the report path
`ticker_suffix_cache.json` existed but only `live_portfolio.py` used it; the report always
tried `.TW` first, eating a 404 for every 上櫃/.TWO stock (e.g. 3105 穩懋). **Fix:**
`prefetch_stock_histories` resolves each code's suffix — known codes download their cached
suffix, unknown codes download **both** `.TW`+`.TWO` in the same batch and keep whichever
returns data, persisting the result. `_ticker_history`/`fetch_yfinance_stock` fallbacks now
probe `_suffix_order(code)` (cached suffix first). The suffix cache was extended in place.

### Bottleneck D — emerging-board (興櫃) stocks → `今日無交易數據`
3595 山太士 and 7924 台灣微脂體 are **興櫃** — absent from `STOCK_DAY_ALL`, the TPEX mainboard
feed, *and* Yahoo (both `.TW`/`.TWO` 404). **Fix:** new `fetch_tpex_emerging()` pulls the
official TPEX emerging API (`openapi/v1/tpex_esb_latest_statistics`, ~346 names) and builds
TWSE-row-shaped dicts from 加權平均價 (`Average` as close, `PreviousAveragePrice` as prev,
`TransactionVolume` as 量), tagged `_fallback='tpex_esb'`. Closing merges these into
`twse_by_code` (only for codes on neither mainboard feed). `_src_tag` now renders `（興櫃均價）`
for them — official data, preferred over Yahoo. `今日無交易數據` on the current watchlist: **2 → 0**.

### Audit of the other sources (item 3)
- **TAIEX** (`fetch_taiex`, `yf.Ticker('^TWII').history`): library path, healthy — left as-is.
- **TWSE "not yet published today"**: `data_is_stale` compares the feed's ROC date to today and
  clearly prints `⚠️ 注意：TWSE尚未發布今日數據，以下為最近交易日（<date>）數據`. Verified correct
  and clearly labelled — no change.
- **valuation / margin joins**: keyed by code, best-effort `→ {}` with a log line on failure;
  they only cover the TWSE main board (openapi), so TPEX/興櫃 names simply carry no PE — a data
  limit, not a silent swallow. Left as-is.
- **Brave news**: was a silent `return []` when `BRAVE_API_KEY` was unset — now **logs the
  reason**. Also added a guarded `.env` loader (`_load_env_file`, honours `OPENCLAW_ENV_FILE`,
  `override=False`, silent no-op without python-dotenv) so standalone/preview runs actually pick
  up `BRAVE_API_KEY` / `OPENROUTER_*` — previously the module never loaded `.env` at all, so
  every preview ran with no news and no AI text even though the task's run command passed
  `OPENCLAW_ENV_FILE`.

### Before / after (closing `--dry-run` preview, same session/day)
| Metric | Before | After |
|---|---|---|
| 全球市場 populated | 0/4 (`資料暫時無法取得`) | **4/4** (S&P/Nasdaq/費半/日經 all with %) |
| 今日無交易數據 | 2 (3595, 7924) | **0** (both via 興櫃均價) |
| Stocks with RSI+量比 in pool | (429-degraded) | **29/30** (1 興櫃 has no history — expected) |
| Yahoo calls / run | ~90 individual → frequent 429 | ~2 batched → no 429 |
| Brave-news no-key | silent | logged |

### Files modified
- `twse_daily_report.py` — new batch/cache layer (`_HISTORY_CACHE`/`_INDEX_CACHE`/`_QUOTE_CACHE`,
  suffix cache helpers, `prefetch_stock_histories`, `_prefetch_indices`, `_slice_period`,
  `_quote_from_history`, `_cached_get_yfinance`, `fetch_tpex_emerging`); rewired
  `fetch_global_indices`, `_ticker_history`, `fetch_yfinance_stock`, `_src_tag`,
  `fetch_brave_news`; added `_load_env_file`; wired prefetch + 興櫃 merge into both report modes.
- (No change needed in `custom_stock_lookup.py` — the earlier certifi/retry hardening stands as
  the last-ditch fallback; the batch path is now primary.)

### Still open / needs a decision
- The urllib chart endpoint stays 429-throttled on this host; it's now only a rarely-hit
  fallback, but if Yahoo ever throttles the library path too there is no third source. A
  paid/official quote source (or the TWSE per-stock intraday API) would remove the single point
  of failure — out of scope here.
- 興櫃 lines show 開盤=收盤 (emerging board has no opening auction; 加權平均價 is the only
  representative price). Tagged `（興櫃均價）` so it's not mistaken for a real O/H/L/C.
- `.env` is now auto-loaded; confirm the production container still relies on real env vars
  (it does — `override=False` means the loader can't clobber them).

## TUI single-source-of-truth audit + fixes (2026-07-08)
Audited every config key the report reads vs what config_tui.py writes. Result: ~95% already
TUI-driven (schedule, sections+order, global indices, notes, header/footer, delivery channels,
AI model/summary/tokens, technicals thresholds, news query/count). Gaps closed:
- **`ai.max_tokens_reason`** — the batched 個股原因 call hardcoded `45*n+120` and IGNORED the TUI
  value. Now honours `ai.max_tokens_reason` (per-stock, default 100, capped 4000). n=28: 1380→2920.
- **`ai.reason_temperature`** — report already read it; TUI couldn't set it. Added to the AI panel
  (blank = model default) + display row.
Remaining cosmetic (documented, no functional impact): `news.query_morning` (morning report has no
news section) and `schedule.weekdays_only` (scheduler always skips TW weekends — correct anyway).
