# Financial Reporting Bot

Automated Taiwan-market reporting for OpenClaw: a scheduled TWSE/TPEX intelligence pipeline that turns portfolio data, watchlist notes, technical signals, global indices, and AI-written commentary into Telegram-ready morning and closing reports.

## What It Does

- Generates two report modes:
  - `morning`: opening-session snapshot using live `yfinance` prices.
  - `closing`: post-market report using official TWSE/TPEX data when available.
- Tracks portfolio holdings with shares, cost basis, current value, and estimated P/L.
- Tracks non-holding watchlist symbols, including optional user notes that now flow directly into the generated report artifact.
- Adds technical context such as RSI, volume ratio, return strips, and divergence signals.
- Uses OpenRouter only for explanatory text. Market figures stay scraper-derived.
- Sends chunked Markdown reports to Telegram, with sandbox previews for dry runs.
- Archives every live report as a dated Markdown artifact under `data/reports/`.
- Provides a Rich terminal UI for portfolio, watchlist, schedule, layout, AI, sandbox, and delivery configuration.
- Includes a Telegram control bot for mobile-friendly status checks, watchlist commands, portfolio review, and on-demand report triggers.

## What's New — 2026-07 Overhaul

A full reliability + cost pass over data acquisition, AI analysis, delivery, and tooling
(diagnosis log in `docs/WORKFLOW_FIX_2026-07-08.md`):

### Market-data acquisition rebuilt (no more blank sections)

- **Batched Yahoo fetch.** One `yf.download` prefetch now feeds global indices, per-stock
  technicals (RSI/量比), quotes, and 1M/3M/1Y return strips — replacing ~90 individual
  chart-API calls per run that routinely tripped `HTTP 429` and blanked whole sections
  (`資料暫時無法取得`). Verified: global indices 0/4 → 4/4 populated, ~2 network calls per run.
- **TPEX emerging-board (興櫃) feed.** Stocks absent from the TWSE/TPEX mainboard feeds and
  Yahoo (e.g. 興櫃 names) now resolve via the official `tpex_esb_latest_statistics` API and
  render with a `（興櫃均價）` source tag instead of `今日無交易數據`.
- **Ticker-suffix cache.** The report now consults `ticker_suffix_cache.json` (previously
  only the live view used it), probing `.TW`/`.TWO` once per symbol and persisting the result.
- **Hardened fallback path.** `custom_stock_lookup.get_yfinance_data` gained a certifi-backed
  SSL context (fixes `CERTIFICATE_VERIFY_FAILED` on hosts with an empty OS trust store),
  a realistic UA, retry with backoff, `Retry-After`-aware 429 handling, and loud failure
  logging via `FRB_DEBUG_FETCH=1`. It is now only the last-ditch fallback.
- **Live board fixed.** The auto-refreshing holdings table in the TUI graphs view
  (`live_portfolio.py`) derives quotes from the same batched download instead of 8 parallel
  raw chart-API calls, which 429'd into an empty board.
- **`.env` auto-load + honest logging.** The report module loads `OPENCLAW_ENV_FILE` (or the
  data-dir sibling `.env`) at import, and Brave news logs when `BRAVE_API_KEY` is missing
  instead of silently returning nothing.

### AI analysis engine

- **Pluggable summary engine.** The closing analyst summary (報告總結) runs on its own model:
  `ai.summary_model` (default in this deployment: `deepseek/deepseek-chat` via OpenRouter,
  ~18× cheaper than an Opus-class model), keyed by `ai.summary_key_env`, with
  `ai.max_tokens_summary` (2400 — fixes mid-table truncation), `ai.summary_temperature`,
  and `ai.summary_history_days` (feeds the last N days of pooled results so the summary can
  reason about streaks and trajectories).
- **Summary scope: market + holdings only.** The analyst summary now deliberately excludes
  the watchlist — it covers 大盤與市場概況, per-holding highlights (RSI/量比/融資/報酬), and
  risks. Watchlist lines still render in the report body.
- **TUI-controlled inference.** The batched per-stock 原因 call honours `ai.max_tokens_reason`
  (was hardcoded) and `ai.reason_temperature` is now editable from the AI Settings panel.

### Delivery & operations

- **Line-safe Telegram chunking.** Reports split on line boundaries so a Markdown entity is
  never cut across a chunk (previously produced `400 can't parse entities` and dropped
  chunks), with a plain-text resend fallback if parsing still fails.
- **Self-contained Docker build.** `Dockerfile` + `.dockerignore` live in the repo; the image
  is a lean Python-only build and runtime data stays in a bind-mounted silo.
- **TUI data-dir guard.** `config_tui.py` resolves its data dir preferring a directory that
  actually contains `portfolio.json`, prints the resolved path in the main menu, and shows a
  red warning when holdings would be empty (the "watchlist-only report" failure mode).
- **Secret-scan guardrail.** Dependency-free `pre-commit`/`pre-push` hooks under
  `scripts/git-hooks/` block API keys, tokens, private keys, and secret filenames before they
  can reach the public remote. Activate after cloning:

  ```bash
  git config core.hooksPath scripts/git-hooks
  ```

- **Privacy:** `tracked_stocks.json` and `portfolio.json` are git-ignored — personal watchlist
  and holdings never ship with the repo. Runtime config lives in your `OPENCLAW_DATA_DIR` silo.

### Earlier changes retained

- Structured watchlist entries (legacy string or `{name, note}` object) with notes rendered
  under each watchlist line, editable from the TUI.
- UTC-pinned scheduler with startup catch-up for missed weekday slots.
- Host-side "Send Now" uses the same resolved `.env` credentials Docker injects in-container.
- Brokerage CSV imports live in `Import CSV/` (scanned alongside `~/Downloads`, archived
  there after import).

## Repository Layout

- `twse_daily_report.py`: report generator — batched data prefetch, 興櫃 merge, archive writer, line-safe Telegram sender, sandbox renderer, OpenRouter AI hooks (reasons / commentary / analyst summary).
- `scheduler.py`: weekday morning/closing scheduler with UTC timing and missed-slot catch-up.
- `config_tui.py`: Rich-based admin console — portfolio (+CSV import), watchlist, schedule, layout/sections, AI engine, sandbox/Send Now, delivery channels, market diary.
- `live_portfolio.py`: live holdings board + candlestick graphs (TUI `[g] Graphs`), batched quote feed.
- `telegram_control_bot.py`: Telegram command and natural-language control interface.
- `market_data_fetcher.py`: TWSE/global market data aggregation and failover logic.
- `custom_stock_lookup.py`: hardened single-quote fallback (certifi SSL, retry/backoff, 429-aware).
- `sandbox_run.py`: preview runner that avoids live Telegram delivery.
- `Dockerfile` / `.dockerignore`: self-contained container build (runtime data bind-mounted).
- `scripts/git-hooks/`: secret-scan `pre-commit` / `pre-push` guardrail.
- `docs/`: engineering logs (e.g. `WORKFLOW_FIX_2026-07-08.md`, the data-pipeline diagnosis).
- `Import CSV/`: dedicated home for brokerage export CSVs used by the TUI's portfolio importer (git-ignored — contains personal position data).
- `tracked_stocks.json` / `portfolio.json`: watchlist + holdings (git-ignored — personal data; live in your data silo).
- `requirements.txt`: Python dependencies (now pins `certifi` and `schedule`).

Runtime data is resolved from `OPENCLAW_DATA_DIR` when set, otherwise from a local `data/` directory. The bot reads `.env` values from `OPENCLAW_ENV_FILE`, the data directory parent, the OpenClaw agent directory, or the repository root, and the TUI loads the resolved file into the environment at startup (Docker-injected values always win).

## Configuration Artifacts

`tracked_stocks.json`

Supports both legacy and structured watchlist formats:

```json
{
  "2330": "台積電",
  "2412": {
    "name": "中華電",
    "note": "Yield and telecom-defensive watch"
  }
}
```

`portfolio.json`

Stores holdings used for position value and P/L calculations:

```json
{
  "2330": {
    "name": "台積電",
    "shares": 1000,
    "cost_basis": 950000
  }
}
```

`bot_config.json`

Controls schedule, AI models, report layout, technical thresholds, delivery channels, and optional per-stock notes. The TUI writes this file atomically and keeps a `.bak` rollback copy. The `ai` block drives every LLM call:

```json
{
  "schedule": { "morning_utc": "01:30", "closing_utc": "08:00" },
  "ai": {
    "model": "anthropic/claude-haiku-4.5",
    "max_tokens_reason": 100,
    "max_tokens_outlook": 200,
    "max_tokens_research": 350,
    "summary_model": "deepseek/deepseek-chat",
    "summary_key_env": "OPENROUTER_DEEPSEEK_KEY",
    "max_tokens_summary": 2400,
    "summary_temperature": 0.3,
    "summary_history_days": 5
  },
  "technicals": {
    "rsi_overbought": 80,
    "rsi_oversold": 30,
    "divergence_threshold": 1.5,
    "period_days": 20
  }
}
```

- `model` is the worker for the high-volume calls (per-stock 原因, 開盤展望, 收盤研究).
- `summary_model` / `summary_key_env` route the analyst 報告總結 to its own model + API key —
  swap engines without touching the worker calls.
- Every field is editable from the TUI's AI Settings panel; changes take effect on the next
  run (config lives in the mounted data volume — no container restart needed).

`.env`

Common keys:

```bash
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
CONTROL_CHAT_ID=
OPENROUTER_API_KEY=
OPENROUTER_DEEPSEEK_KEY=
BRAVE_API_KEY=
OPENCLAW_DATA_DIR=
OPENCLAW_ENV_FILE=
```

Add one env var per `summary_key_env` (or any per-channel `token_env`) you configure.

## Installation

```bash
git clone https://github.com/Panbear1983/Financial_Reporting_Bot.git
cd Financial_Reporting_Bot
pip install -r requirements.txt
git config core.hooksPath scripts/git-hooks   # activate the secret-scan guardrail
```

### Docker

The repo is the build context — the image bakes the code, and all runtime data/config stays
in a bind-mounted silo:

```bash
docker build -t financial-bot .
docker run -d --name financial-bot \
  --env-file /path/to/silo/.env \
  -e OPENCLAW_DATA_DIR=/app/data \
  -v /path/to/silo/data:/app/data \
  --restart unless-stopped \
  financial-bot
```

The container runs `scheduler.py` (via `start.sh`) and pushes on the configured UTC slots.

## Usage

Run the interactive admin console:

```bash
python config_tui.py
```

Generate a report without sending it:

```bash
python twse_daily_report.py --mode=morning --dry-run
python twse_daily_report.py --mode=closing --dry-run
```

Run the scheduler:

```bash
python scheduler.py
```

Run one scheduled mode immediately:

```bash
python scheduler.py --now=morning
python scheduler.py --now=closing
python scheduler.py --now=all
```

Preview safely without Telegram delivery:

```bash
python scheduler.py --sandbox --now=all
```

Start the Telegram control bot:

```bash
python telegram_control_bot.py
```

## Report Artifacts

Live runs write:

- `data/twse_daily_report.md`: latest generated Markdown report.
- `data/reports/twse_YYYY-MM-DD_HHMMSS_morning.md`: archived morning report.
- `data/reports/twse_YYYY-MM-DD_HHMMSS_closing.md`: archived closing report.

Sandbox runs write preview files such as:

- `data/sandbox_preview_YYYYMMDD_HHMMSS.txt`

## Design Notes

- Scrapers own the numbers; AI only writes narrative context (prompts forbid inventing figures).
- Official sources first: TWSE `STOCK_DAY_ALL` → TPEX mainboard → TPEX 興櫃 ESB → batched
  yfinance → hardened single-quote fallback, in that order.
- One batched Yahoo download per run feeds indices, technicals, quotes, and return strips —
  per-symbol chart-API calls are a last resort (they rate-limit aggressively).
- The analyst summary reads a note-free structured digest of objective signals (never the
  AI-written per-stock text) to avoid AI-of-AI echo, and covers market + holdings only.
- Scheduler times are UTC by design: default `01:30` UTC for Taiwan open and `08:00` UTC for Taiwan close.
- Weekend checks use Taiwan local weekday logic.
- Telegram delivery is chunked at line boundaries with a plain-text fallback, so Markdown
  entities never split across messages and a parse failure can't drop content.
- Atomic config writes prevent a scheduled report from reading half-written JSON.
- Personal data (holdings, watchlist, brokerage CSVs) is git-ignored; secret-scan hooks guard
  every commit and push to this public repo.

## License

MIT License
