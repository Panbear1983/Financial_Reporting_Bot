# AgenticOS: AI-Driven Market Intelligence & Automated Delivery System

Welcome to the **AgenticOS Market Intelligence Workflow**. This repository contains a sophisticated, fully autonomous data pipeline that aggregates real-time Taiwanese and Global stock market data, leverages advanced Large Language Models (LLMs) for instant analysis, and securely pushes highly structured, professional market reports directly to a specified Telegram group.

This system is designed to run silently in the background, executing precise, data-verified cron jobs that handle live financial data without human intervention.

---

## 🚀 Key Features

### 1. Multi-Source Financial Data Aggregation
*   **Real-Time Metrics:** Fetches live quotes, volume ratios, and historical data via Yahoo Finance.
*   **Technical Indicators:** Automatically calculates critical technical indicators, including 14-day RSI (Relative Strength Index) and 5-day Moving Averages.
*   **Data Integrity Auditing:** Implements strict data validation rules (e.g., 10% price limit mismatch corrections) to catch and flag faulty data from upstream APIs before analysis.
*   **TWSE Market Scans:** Scrapes the Taiwan Stock Exchange (TWSE) daily reports to identify top volume movers and significant dip-point opportunities.

### 2. Autonomous Agentic Analysis (Claude 3.5 Sonnet)
*   Instead of simply reporting numbers, the pipeline formats the raw market data, alongside scraped Google News headlines, into a highly structured prompt.
*   This prompt is processed by **Anthropic's Claude 3.5 Sonnet** (via OpenRouter) to generate:
    *   **Single-Sentence Rationales:** Precise, professional explanations for the day's price action for every tracked stock.
    *   **Supply Chain Analysis:** Vertical analysis identifying upstream/downstream impacts and direct competitor movements based on current news.
    *   **Sector Rotation Narratives:** Sophisticated commentary on macroeconomic shifts and trending industries outside the immediate watchlist.

### 3. Automated & Secure Delivery (OpenClaw)
*   **Headless Execution:** Triggered via background cron jobs managed by the OpenClaw orchestration framework.
*   **Direct Push:** The finalized Markdown report is securely pushed directly to a designated Telegram group via the `openclaw message send` CLI command.

### 4. Local Data Persistence
*   Every generated report is permanently archived locally.
*   **Formats Supported:** Markdown (`.md`), structured JSON (`.json`), and long-term storage in a local SQLite database (`market_history.db`).

---

## 📁 Architecture & File Structure

The workflow is intentionally lightweight, relying on two core Python scripts and dynamic JSON configuration files to separate logic from data.

### Core Scripts
*   **`market_report.py`**
    *   **Location:** `/root/Desktop/AgenticOS/`
    *   **Function:** The "Brain." Handles all data fetching (yfinance, TWSE, Google News RSS), technical calculations, parallel processing, LLM API calls, and local data archiving (JSON, SQLite, Markdown).
*   **`market_report_push.py`**
    *   **Location:** `/root/.openclaw/workspace/`
    *   **Function:** The "Trigger." Scheduled by OpenClaw cron jobs. It imports the main generation function from `market_report.py`, captures the output, and executes the system command to push the report to Telegram.

### Dynamic Configuration (No Code Changes Required)
*   **`tracked_stocks.json`**: Defines the exact stock tickers (e.g., "2330") and categories to monitor.
*   **`news_config.json`**: Controls the specific keywords used for the Google News RSS scraper.
*   **`report_layout.json`**: Customizes the titles, headers, and structural flow of the final Telegram message.

---

## 🛠️ Setup & Execution

### Prerequisites
*   Python 3.8+
*   OpenClaw CLI framework installed and configured for Telegram messaging.
*   An active OpenRouter API Key (for LLM access).

### Installation
1.  **Clone the Repository:**
    ```bash
    git clone https://github.com/Panbear1983/AgenticOS-Market-Intelligence.git
    cd AgenticOS-Market-Intelligence
    ```
2.  **Environment Variables:**
    Ensure your OpenRouter API key is correctly set within the `market_report.py` script or exported to your environment.
3.  **Dependencies:**
    Install required standard libraries (e.g., `json`, `sqlite3`, `concurrent.futures`, `urllib`). Note: This project heavily relies on Python's standard library to minimize external dependencies, with the exception of API interactions.

### Manual Execution
To manually generate a report and push it to Telegram, simply run the push script:
```bash
python3 /root/.openclaw/workspace/market_report_push.py
```

### Automated Scheduling
The system is designed to be scheduled via OpenClaw's cron system. Ensure your OpenClaw `jobs.json` is configured to trigger `market_report_push.py` at your desired market open/close times (e.g., `30 9 * * 1-5` for 9:30 AM on weekdays).

---

## 🛡️ Security Considerations
*   **API Keys:** Never hardcode sensitive API keys or Telegram Peer IDs in public repositories. Ensure these are managed via environment variables or secure credential stores in a production setup.
*   **VM Isolation:** As this workflow handles potentially sensitive financial data and executes external commands, it is highly recommended to run this within a hardened, isolated Virtual Machine with strict network boundaries.

---
*Developed by [Panbear1983](https://github.com/Panbear1983).*
