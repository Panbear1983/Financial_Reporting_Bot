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

## Recent Artifact Changes

The latest local changes affect both generated output and operational reliability:

- Watchlist entries can now be stored as either the legacy string value or a structured object:
  ```json
  {
    "2330": "台積電",
    "2412": {
      "name": "中華電",
      "note": "Dividend stability watch"
    }
  }
  ```
- Structured watchlist notes render into both morning and closing report artifacts under the matching watchlist line.
- `config_tui.py` now supports adding and editing watchlist notes from the terminal UI.
- The TUI header has a uniform width across pages and shortens the data path so navigation feels stable in narrow terminals.
- `scheduler.py` now pins scheduling to UTC and performs a startup catch-up run if a weekday morning or closing slot already passed without a saved report.
- `config_tui.py` loads the resolved `.env` at startup, so host-side "Send Now" runs deliver to Telegram with the same credentials Docker injects in-container.
- Brokerage CSV imports now live in a dedicated `Import CSV/` folder inside the repo: the importer scans it first (plus `~/Downloads` for fresh exports) and moves each file there after a successful import.
- `schedule` is now declared in `requirements.txt` (it was previously an undeclared dependency of `scheduler.py`).

## Repository Layout

- `twse_daily_report.py`: report generator, archive writer, Telegram sender, sandbox renderer, OpenRouter commentary hooks.
- `scheduler.py`: weekday morning/closing scheduler with UTC timing and missed-slot catch-up.
- `config_tui.py`: Rich-based admin console for configuration and report orchestration.
- `telegram_control_bot.py`: Telegram command and natural-language control interface.
- `market_data_fetcher.py`: TWSE/global market data aggregation and failover logic.
- `custom_stock_lookup.py`: symbol lookup and `yfinance` fallback helpers.
- `sandbox_run.py`: preview runner that avoids live Telegram delivery.
- `tracked_stocks.json`: watchlist configuration.
- `Import CSV/`: dedicated home for brokerage export CSVs used by the TUI's portfolio importer (git-ignored — contains personal position data).
- `requirements.txt`: Python dependencies.

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

Controls schedule, AI models, report layout, technical thresholds, delivery channels, and optional per-stock notes. The TUI writes this file atomically and keeps a `.bak` rollback copy.

`.env`

Common keys:

```bash
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
CONTROL_CHAT_ID=
OPENROUTER_API_KEY=
BRAVE_API_KEY=
OPENCLAW_DATA_DIR=
OPENCLAW_ENV_FILE=
```

## Installation

```bash
git clone https://github.com/Panbear1983/Financial_Reporting_Bot.git
cd Financial_Reporting_Bot
pip install -r requirements.txt
```

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

- Scrapers own the numbers; AI only writes narrative context.
- Scheduler times are UTC by design: default `01:30` UTC for Taiwan open and `08:00` UTC for Taiwan close.
- Weekend checks use Taiwan local weekday logic.
- Telegram delivery is chunked to stay below message size limits.
- Atomic config writes prevent a scheduled report from reading half-written JSON.

## License

MIT License
