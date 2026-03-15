import urllib.request
import json
import sys
from datetime import datetime

def get_yfinance_data(symbol):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
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
    except Exception as e:
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
