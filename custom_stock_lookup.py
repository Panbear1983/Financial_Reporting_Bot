import urllib.request
import urllib.error
import json
import sys
import ssl
import time
import os
from datetime import datetime

# --- SSL trust store -------------------------------------------------------
# The bare urllib path used below relies on the OS CA store, which is missing
# on the macOS python.org framework build (and any minimal env), producing
# "CERTIFICATE_VERIFY_FAILED" — the reason *every* global index and the
# yfinance price fallback used to come back empty. Build the context from
# certifi's bundle (shipped transitively via requests/yfinance) so verification
# works identically on host and in the container, without disabling it.
def _build_ssl_context():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        try:
            return ssl.create_default_context()
        except Exception:
            return None

_SSL_CTX = _build_ssl_context()

# Set FRB_DEBUG_FETCH=1 to surface why a fetch returned None instead of silence.
_DEBUG = os.getenv('FRB_DEBUG_FETCH', '').lower() in ('1', 'true', 'yes')

_UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
       'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')


def get_yfinance_data(symbol, retries=3):
    """Fetch a single quote from Yahoo's chart endpoint.

    Hardened 2026-07-08: certifi-backed SSL context, realistic UA, and retry
    with backoff on transient/rate-limit errors. Returns a dict or None; on
    None the reason is logged when FRB_DEBUG_FETCH is set (no more silent
    swallowing). A genuine 404 (delisted / wrong suffix) is not retried.
    """
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    last_err = None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, headers={'User-Agent': _UA})
        try:
            with urllib.request.urlopen(req, timeout=10, context=_SSL_CTX) as response:
                data = json.loads(response.read().decode())
                meta = data['chart']['result'][0]['meta']
            
                price = meta['regularMarketPrice']
                prev_close = meta['chartPreviousClose']

                try:
                    today_open = data['chart']['result'][0]['indicators']['quote'][0]['open'][0]
                    if today_open is None:
                        today_open = prev_close
                except (KeyError, IndexError, TypeError):
                    today_open = prev_close

                change = price - prev_close
                intraday_change = price - today_open

                # Use the actual symbol name if longName isn't available
                name = meta.get('longName', symbol.split('.')[0])
                if 'shortName' in meta and not meta.get('longName'):
                    name = meta['shortName']

                return {
                    "name": name,
                    "price": price,
                    "prev_close": prev_close,
                    "today_open": today_open,
                    "change": change,
                    "intraday_change": intraday_change
                }
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 404:
                if _DEBUG:
                    print(f"[fetch] {symbol}: 404 not found (delisted / wrong suffix)")
                return None  # genuine "no such symbol" — retrying won't help
            if e.code == 429 and attempt < retries:
                # Rate-limited: honour Retry-After when present, capped so a single
                # symbol can't stall the whole report.
                try:
                    wait = min(float(e.headers.get('Retry-After', 0)) or 0, 20)
                except (TypeError, ValueError):
                    wait = 0
                time.sleep(wait or (2.0 * attempt))
                continue
            # other 401/403/5xx are often transient — fall through to backoff+retry
        except (urllib.error.URLError, ssl.SSLError, TimeoutError,
                json.JSONDecodeError, KeyError, IndexError, TypeError) as e:
            last_err = e
        if attempt < retries:
            time.sleep(1.5 * attempt)  # linear backoff between attempts
    if _DEBUG:
        print(f"[fetch] {symbol}: giving up after {retries} attempts -> {last_err}")
    return None

def main():
    if len(sys.argv) < 2:
        print("請提供股票代號，例如: python3 /root/Desktop/AgenticOS/custom_stock_lookup.py 2330 2317 2454")
        return
        
    codes = sys.argv[1:]
    
    now = datetime.now()
    is_afternoon = now.hour >= 13
    
    print(f"📊 即時個股查詢 ({now.strftime('%Y-%m-%d %H:%M')})\n")
    
    for code in codes:
        # Assume Taiwanese stocks if no suffix is provided
        symbol = f"{code}.TW" if not code.endswith('.TW') and not code.endswith('.TWO') else code
        data = get_yfinance_data(symbol)
        
        if data:
            if is_afternoon:
                action = "漲" if data['intraday_change'] >= 0 else "跌"
                print(f"• {data['name']} ({code})：[開盤] {data['today_open']:.1f}元 → [收盤] {data['price']:.1f}元 ({action} {abs(data['intraday_change']):.1f}元)")
            else:
                action = "漲" if data['change'] >= 0 else "跌"
                print(f"• {data['name']} ({code})：[昨收] {data['prev_close']:.1f}元 → [開盤] {data['price']:.1f}元 ({action} {abs(data['change']):.1f}元)")
        else:
            print(f"• 無法獲取代號 {code} 的即時資料。")

if __name__ == "__main__":
    main()
