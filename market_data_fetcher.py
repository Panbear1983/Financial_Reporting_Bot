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
        logging.FileHandler(os.getenv('OPENCLAW_DATA_DIR', '/app/data') + '/market_data.log'),
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
        self.cache_dir = os.path.join(os.getenv('OPENCLAW_DATA_DIR', '/app/data'), 'cache')
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
        with open(os.getenv('OPENCLAW_DATA_DIR', '/app/data') + '/market_data.json', 'w', encoding='utf-8') as f:
            json.dump(market_data, f, ensure_ascii=False, indent=2)

        logger.info("Market data collection completed successfully")

    except Exception as e:
        logger.error(f"Error in main execution: {e}", exc_info=True)
        raise

if __name__ == "__main__":
    main()