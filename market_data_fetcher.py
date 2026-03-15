import urllib.request
import json
import os
import datetime
import ssl
import time
import yfinance as yf
import pandas as pd
import requests
from typing import Dict, List, Optional, Any
from abc import ABC, abstractmethod
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('/root/Desktop/AgenticOS/market_data.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

class MarketDataSource(ABC):
    """Abstract base class for market data sources"""
    
    @abstractmethod
    def fetch_data(self, symbol: str) -> Dict[str, Any]:
        """Fetch market data for a given symbol"""
        pass
    
    @abstractmethod
    def is_available(self) -> bool:
        """Check if the data source is currently available"""
        pass

class TWSDDataSource(MarketDataSource):
    """Taiwan Stock Exchange (TWSE) data source"""
    
    def __init__(self):
        self.base_url = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
        self.ssl_context = ssl.create_default_context()
        self.ssl_context.check_hostname = False
        self.ssl_context.verify_mode = ssl.CERT_NONE

    def is_available(self) -> bool:
        try:
            req = urllib.request.Request(
                self.base_url,
                headers={'User-Agent': 'Mozilla/5.0'}
            )
            with urllib.request.urlopen(req, context=self.ssl_context, timeout=5):
                return True
        except:
            return False

    def fetch_data(self, symbol: str = None) -> Dict[str, Any]:
        """Fetch all TWSE market data"""
        try:
            req = urllib.request.Request(
                self.base_url,
                headers={'User-Agent': 'Mozilla/5.0'}
            )
            with urllib.request.urlopen(req, context=self.ssl_context, timeout=30) as response:
                data = json.loads(response.read().decode('utf-8'))
            return {'source': 'TWSE', 'data': data}
        except Exception as e:
            logger.error(f"TWSE data fetch error: {e}")
            raise

class YahooFinanceDataSource(MarketDataSource):
    """Yahoo Finance data source"""
    
    def is_available(self) -> bool:
        try:
            yf.download("2330.TW", period="1d")
            return True
        except:
            return False

    def fetch_data(self, symbol: str) -> Dict[str, Any]:
        try:
            stock = yf.Ticker(symbol)
            info = stock.info
            history = stock.history(period="1d")
            return {
                'source': 'Yahoo Finance',
                'data': {
                    'info': info,
                    'history': history.to_dict('records')
                }
            }
        except Exception as e:
            logger.error(f"Yahoo Finance data fetch error: {e}")
            raise

class AlphaVantageDataSource(MarketDataSource):
    """Alpha Vantage data source"""
    
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://www.alphavantage.co/query"

    def is_available(self) -> bool:
        try:
            params = {
                'function': 'TIME_SERIES_INTRADAY',
                'symbol': '2330.TW',
                'interval': '1min',
                'apikey': self.api_key
            }
            response = requests.get(self.base_url, params=params, timeout=5)
            return 'Error Message' not in response.json()
        except:
            return False

    def fetch_data(self, symbol: str) -> Dict[str, Any]:
        try:
            params = {
                'function': 'GLOBAL_QUOTE',
                'symbol': symbol,
                'apikey': self.api_key
            }
            response = requests.get(self.base_url, params=params)
            return {'source': 'Alpha Vantage', 'data': response.json()}
        except Exception as e:
            logger.error(f"Alpha Vantage data fetch error: {e}")
            raise

class MarketDataAggregator:
    """Aggregates data from multiple sources with failover support"""
    
    def __init__(self):
        self.data_sources: List[MarketDataSource] = []
        self.cache_dir = "/root/Desktop/AgenticOS/cache"
        os.makedirs(self.cache_dir, exist_ok=True)

    def add_data_source(self, source: MarketDataSource):
        """Add a new data source"""
        self.data_sources.append(source)

    def _cache_data(self, data: Dict[str, Any], source_name: str):
        """Cache the fetched data"""
        cache_file = os.path.join(
            self.cache_dir,
            f"{source_name}_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        )
        with open(cache_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _get_latest_cache(self, source_name: str) -> Optional[Dict[str, Any]]:
        """Get the most recent cached data for a source"""
        cache_files = [f for f in os.listdir(self.cache_dir) if f.startswith(source_name)]
        if not cache_files:
            return None
        latest_file = max(cache_files)
        with open(os.path.join(self.cache_dir, latest_file), 'r', encoding='utf-8') as f:
            return json.load(f)

    def fetch_market_data(self, symbol: str = None) -> Dict[str, Any]:
        """
        Fetch market data from all available sources
        Returns aggregated data or falls back to cached data if all sources fail
        """
        results = {}
        errors = {}

        # Try all data sources in parallel
        with ThreadPoolExecutor(max_workers=len(self.data_sources)) as executor:
            future_to_source = {
                executor.submit(source.fetch_data, symbol): source
                for source in self.data_sources if source.is_available()
            }

            for future in as_completed(future_to_source):
                source = future_to_source[future]
                try:
                    data = future.result()
                    source_name = data['source']
                    results[source_name] = data['data']
                    self._cache_data(data, source_name)
                except Exception as e:
                    errors[source.__class__.__name__] = str(e)
                    # Try to get cached data
                    cached_data = self._get_latest_cache(source.__class__.__name__)
                    if cached_data:
                        results[f"{source.__class__.__name__} (cached)"] = cached_data

        if not results:
            raise Exception(f"All data sources failed. Errors: {errors}")

        return {
            'timestamp': datetime.datetime.now().isoformat(),
            'data': results,
            'errors': errors if errors else None
        }

def generate_market_report(data: Dict[str, Any]) -> str:
    """Generate a comprehensive market report from aggregated data"""
    report_lines = []
    report_lines.append("# Taiwan Stock Market Daily Report")
    report_lines.append(f"Generated at: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

    # Process TWSE data
    if 'TWSE' in data['data']:
        twse_data = data['data']['TWSE']
        df = pd.DataFrame(twse_data)
        
        # Market overview
        report_lines.append("## Market Overview")
        report_lines.append(f"Total Stocks: {len(df)}")
        if 'Change' in df.columns:
            gainers = len(df[df['Change'].astype(float) > 0])
            losers = len(df[df['Change'].astype(float) < 0])
            report_lines.append(f"Advancing: {gainers}")
            report_lines.append(f"Declining: {losers}\n")

        # Most active stocks
        report_lines.append("## Most Active Stocks")
        if 'TradeVolume' in df.columns:
            most_active = df.nlargest(5, 'TradeVolume')
            for _, stock in most_active.iterrows():
                report_lines.append(
                    f"- {stock['Name']} ({stock['Code']}): "
                    f"Volume {stock['TradeVolume']}, "
                    f"Close {stock['ClosingPrice']}"
                )

    # Add data from other sources
    for source, source_data in data['data'].items():
        if source != 'TWSE':
            report_lines.append(f"\n## {source} Data")
            report_lines.append(f"```json\n{json.dumps(source_data, indent=2)}\n```")

    # Add error information if any
    if data.get('errors'):
        report_lines.append("\n## Data Source Errors")
        for source, error in data['errors'].items():
            report_lines.append(f"- {source}: {error}")

    return "\n".join(report_lines)

def main():
    """Main function to run the market data collection and report generation"""
    try:
        # Initialize data sources
        aggregator = MarketDataAggregator()
        aggregator.add_data_source(TWSDDataSource())
        aggregator.add_data_source(YahooFinanceDataSource())
        
        # Add Alpha Vantage if API key is available
        alpha_vantage_key = os.getenv('ALPHA_VANTAGE_API_KEY')
        if alpha_vantage_key:
            aggregator.add_data_source(AlphaVantageDataSource(alpha_vantage_key))

        # Fetch market data
        logger.info("Fetching market data...")
        market_data = aggregator.fetch_market_data()

        # Save raw data
        with open('/root/Desktop/AgenticOS/market_data.json', 'w', encoding='utf-8') as f:
            json.dump(market_data, f, ensure_ascii=False, indent=2)

        # Generate and save report
        logger.info("Generating market report...")
        report = generate_market_report(market_data)
        with open('/root/Desktop/AgenticOS/market_report.md', 'w', encoding='utf-8') as f:
            f.write(report)

        logger.info("Market data collection and report generation completed successfully")

    except Exception as e:
        logger.error(f"Error in main execution: {e}", exc_info=True)
        raise

if __name__ == "__main__":
    main()