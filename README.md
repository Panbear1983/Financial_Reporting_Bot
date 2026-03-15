# Taiwan Stock Market & Global Index Data Scraper Package

A robust, multi-source financial data scraping and reporting tool designed for the Taiwan Stock Exchange (TWSE) and global market indices. This package is part of the **AgenticOS** ecosystem, providing reliable market data for AI agents and financial analysis.

## 🚀 Features

- **Multi-Source Data Aggregation**: Fetches data from TWSE OpenAPI, Yahoo Finance (`yfinance`), and Alpha Vantage with built-in failover support.
- **Global Index Tracking**: Monitors key global markets including:
  - Dow Jones (^DJI)
  - Nasdaq (^IXIC)
  - S&P 500 (^GSPC)
  - PHLX Semiconductor (SOX)
  - Nikkei 225 (^N225)
- **Automatic Reporting**: Generates comprehensive daily market reports in Markdown format (`twse_daily_report.md`).
- **SSL/TLS Resiliency**: Custom SSL context handling to bypass certificate verification issues often encountered with government/financial APIs in certain environments.
- **Parallel Processing**: Uses `ThreadPoolExecutor` for high-performance concurrent data fetching.
- **Portfolio Tracking**: Customizable `tracked_stocks.json` to monitor specific symbols (e.g., TSMC 2330, Foxconn 2317).
- **Extensible Architecture**: Clean, abstract base classes for adding new data sources easily.

## 📁 Package Structure

- `market_data_fetcher.py`: The core engine for aggregating data from multiple APIs with caching and failover logic.
- `twse_daily_report.py`: A specialized script for generating the final Markdown report, including global trends and news.
- `custom_stock_lookup.py`: A utility tool for looking up specific stock details.
- `tracked_stocks.json`: Configuration file for your target portfolio.
- `requirements.txt`: Project dependencies.

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
Run the following command to fetch global indices, TWSE data, and latest news to generate a report:
```bash
python twse_daily_report.py
```
This will produce `twse_daily_report.md` in the directory.

### Using the Data Aggregator
You can use `market_data_fetcher.py` in your own scripts for more advanced data needs:
```python
from market_data_fetcher import MarketDataAggregator, TWSDDataSource, YahooFinanceDataSource

aggregator = MarketDataAggregator()
aggregator.add_data_source(TWSDDataSource())
aggregator.add_data_source(YahooFinanceDataSource())

data = aggregator.fetch_market_data("2330.TW")
print(data)
```

## ⚙️ Configuration

Edit `tracked_stocks.json` to update the list of stocks you want to monitor in the "Tracked Portfolio" section of the daily report.

## 📜 License

MIT License

---
*Created as part of the AgenticOS ecosystem for advanced market analysis and agentic automation.*
