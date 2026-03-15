import urllib.request
import json
import os
import datetime
import ssl
import xml.etree.ElementTree as ET

def fetch_global_indices():
    indices = {
        'Dow Jones (道瓊工業)': '^DJI',
        'Nasdaq (那斯達克)': '^IXIC',
        'S&P 500 (標普500)': '^GSPC',
        'SOX (費城半導體)': '^SOX',
        'Nikkei 225 (日經225)': '^N225'
    }
    results = []
    
    for name, symbol in indices.items():
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                data = json.loads(response.read().decode())
                meta = data['chart']['result'][0]['meta']
                price = meta['regularMarketPrice']
                prev_close = meta['chartPreviousClose']
                change = price - prev_close
                change_pct = (change / prev_close) * 100
                results.append(f"- **{name}**: {price:,.2f} ({'+' if change>0 else ''}{change:,.2f} / {'+' if change_pct>0 else ''}{change_pct:.2f}%)")
        except Exception as e:
            results.append(f"- **{name}**: Data unavailable ({e})")
            
    return results

def fetch_market_trends():
    url = 'https://news.google.com/rss/search?q=%E5%8F%B0%E8%82%A1+%E6%BD%9B%E5%8A%9B%E8%82%A1+%E8%B6%A8%E5%8B%A2&hl=zh-TW&gl=TW&ceid=TW:zh-Hant'
    results = []
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            tree = ET.fromstring(response.read().decode('utf-8'))
            for item in tree.findall('.//item')[:5]:
                title = item.find('title').text
                results.append(f"- {title}")
    except Exception as e:
        results.append(f"Trend data unavailable ({e})")
    return results

def generate_daily_report():
    url = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
    
    output_dir = "/root/Desktop/AgenticOS"
    data_file = os.path.join(output_dir, "twse_data.json")
    report_file = os.path.join(output_dir, "twse_daily_report.md")
    
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    
    # 1. Fetch Global Indices
    print(f"[{datetime.datetime.now()}] Fetching Global Indices...")
    global_indices = fetch_global_indices()
    
    # 2. Fetch Market Trends
    print(f"[{datetime.datetime.now()}] Fetching Market Trends...")
    trends = fetch_market_trends()
    
    # 3. Fetch TWSE Data
    print(f"[{datetime.datetime.now()}] Fetching TWSE data from {url}...")
    max_retries = 3
    data = None
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, context=ssl_context, timeout=30) as response:
                body = response.read()
                data = json.loads(body.decode('utf-8'))
                
            print(f"[{datetime.datetime.now()}] Successfully fetched data for {len(data)} TWSE stocks.")
            break
        except Exception as e:
            print(f"[{datetime.datetime.now()}] Attempt {attempt + 1} failed: {e}")
            if attempt == max_retries - 1:
                return
            import time
            time.sleep(2)
            
    if not data:
        return
        
    try:
        # Save the raw TWSE data
        with open(data_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            
        # Generate the report
        report_lines = []
        report_lines.append(f"# TWSE & Global Daily Market Report")
        report_lines.append(f"**Date:** {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        
        report_lines.append("## Global Indices (美股與日股表現)")
        report_lines.extend(global_indices)
        report_lines.append("\n")
        
        report_lines.append("## Market Trends & Rising Stars (市場趨勢與潛力股)")
        report_lines.extend(trends)
        report_lines.append("\n")
        
        report_lines.append(f"## TWSE Overview")
        report_lines.append(f"**Total Stocks Tracked:** {len(data)}\n")
        
        # Tracked Portfolio
        tracked_codes = ['2330', '2317', '2454', '6443', '3576', '1326']
        report_lines.append("### 🎯 Tracked Portfolio (重點追蹤個股)")
        for d in data:
            if d.get('Code') in tracked_codes:
                try:
                    change_str = d.get('Change', '').replace(',', '').strip()
                    change = float(change_str) if change_str else 0.0
                    sign = "+" if change > 0 else ""
                    report_lines.append(f"- **{d['Code']} {d['Name']}**: Close: {d['ClosingPrice']}, Change: {sign}{change}")
                except ValueError:
                    report_lines.append(f"- **{d['Code']} {d['Name']}**: Close: {d['ClosingPrice']}, Change: {d.get('Change')}")
        report_lines.append("\n")
        
        # Filter and sort data
        valid_data = []
        for d in data:
            try:
                change_str = d.get('Change', '').replace(',', '').strip()
                if not change_str: continue
                change = float(change_str)
                d['ChangeFloat'] = change
                d['TradeVolumeInt'] = int(d.get('TradeVolume', '0').replace(',', ''))
                valid_data.append(d)
            except ValueError:
                continue
                
        if valid_data:
            valid_data.sort(key=lambda x: x['TradeVolumeInt'], reverse=True)
            report_lines.append("### Most Active Stocks (by Volume)")
            for stock in valid_data[:5]:
                report_lines.append(f"- **{stock['Code']} {stock['Name']}**: Close: {stock['ClosingPrice']}, Volume: {stock['TradeVolume']}")
            
            report_lines.append("\n### Top Price Changes (Absolute value)")
            valid_data.sort(key=lambda x: abs(x['ChangeFloat']), reverse=True)
            for stock in valid_data[:5]:
                sign = "+" if stock['ChangeFloat'] > 0 else ""
                report_lines.append(f"- **{stock['Code']} {stock['Name']}**: Close: {stock['ClosingPrice']}, Change: {sign}{stock['ChangeFloat']}")
                
        with open(report_file, 'w', encoding='utf-8') as f:
            f.write("\n".join(report_lines))
            
        print(f"[{datetime.datetime.now()}] Report successfully generated at {report_file}")
        
    except Exception as e:
        print(f"[{datetime.datetime.now()}] Error generating report: {e}")

if __name__ == "__main__":
    generate_daily_report()