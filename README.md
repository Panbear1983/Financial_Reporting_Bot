# Taiwan Stock Market & Global Index Data Scraper Package

A robust, multi-source financial data scraping and reporting tool designed for the Taiwan Stock Exchange (TWSE) and global market indices. This package is part of the **AgenticOS** ecosystem, providing reliable market data for AI agents and financial analysis.

## 🚀 Features

- **Multi-Source Data Aggregation**: Fetches data from TWSE OpenAPI, Yahoo Finance (`yfinance`), and Alpha Vantage with built-in failover support.
- **Global Index Tracking**: Monitors key global markets (Dow Jones, Nasdaq, S&P 500, SOX, Nikkei 225).
- **Automatic Reporting**: Generates comprehensive daily market reports in Markdown format.
- **SSL/TLS Resiliency**: Custom SSL context handling to bypass certificate verification issues with financial APIs.
- **Parallel Processing**: Uses `ThreadPoolExecutor` for high-performance concurrent data fetching.
- **Portfolio Tracking**: Customizable `tracked_stocks.json` to monitor specific symbols.

## 📁 Package Structure

- `market_data_fetcher.py`: Core engine for aggregating data with caching and failover logic.
- `twse_daily_report.py`: Specialized script for generating the Markdown report and latest news.
- `custom_stock_lookup.py`: Utility tool for looking up specific stock details.
- `tracked_stocks.json`: Configuration for your target portfolio.
- `requirements.txt`: Project dependencies.

## 📊 Sample Report Output

The bot generates a structured report (`twse_daily_report.md`) that looks like this:

```markdown
# TWSE & Global Daily Market Report
**Date:** 2026-03-15 22:34:12

## Global Indices (美股與日股表現)
- **Dow Jones (道瓊工業)**: 46,558.47 (-119.38 / -0.26%)
- **Nasdaq (那斯達克)**: 22,105.36 (-206.62 / -0.93%)
- **S&P 500 (標普500)**: 6,632.19 (-40.43 / -0.61%)
- **SOX (費城半導體)**: 7,646.64 (+3.46 / +0.05%)
- **Nikkei 225 (日經225)**: 53,819.61 (-633.35 / -1.16%)

## Market Trends & Rising Stars (市場趨勢與潛力股)
- 2026具爆發力「AI潛力股名單」曝光...
- 法人點火提前衝！內資狂敲「14檔新年潛力股」...

## TWSE Overview
### 🎯 Tracked Portfolio (重點追蹤個股)
- **2330 台積電**: Close: 1865.00, Change: -20.0
- **2317 鴻海**: Close: 214.50, Change: 0.0
- **2454 聯發科**: Close: 1720.00, Change: -65.0
- **6443 元晶**: Close: 50.60, Change: -0.4

### Most Active Stocks (by Volume)
- **3481 群創**: Close: 29.70, Volume: 1,369,365,716
- **2409 友達**: Close: 16.55, Volume: 324,520,122
```

## 🛠️ Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/Panbear1983/Stock-Scraper-Package.git
   cd Stock-Scraper-Package
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

## 📈 Usage

### Generating a Daily Report
Run the following command to fetch global indices, TWSE data, and latest news:
```bash
python twse_daily_report.py
```

### Using the Data Aggregator
```python
from market_data_fetcher import MarketDataAggregator, TWSDDataSource

aggregator = MarketDataAggregator()
aggregator.add_data_source(TWSDDataSource())
data = aggregator.fetch_market_data("2330.TW")
```

## 📜 License
MIT License

---
*Created as part of the AgenticOS ecosystem for advanced market analysis and agentic automation.*
