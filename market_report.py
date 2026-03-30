import urllib.request
import urllib.parse
import json
import ssl
import xml.etree.ElementTree as ET
from datetime import datetime
import concurrent.futures

import os

# Securely load API Key from environment variable
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")

if not OPENROUTER_API_KEY:
    print("Error: OPENROUTER_API_KEY environment variable not set.")
    # Fallback to local config if necessary, but do not hardcode secrets
    # OPENROUTER_API_KEY = "YOUR_KEY_HERE"


def get_yfinance_data(symbol):
    # Fetch 1 month of daily data to calculate technical indicators
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=1mo"
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    
    # Use standard context for fetching
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    
    try:
        with urllib.request.urlopen(req, context=ssl_context, timeout=10) as response:
            data = json.loads(response.read().decode())
            res = data['chart']['result'][0]
            meta = res['meta']
            
            # Basic Price Info
            price = meta['regularMarketPrice']
            prev_close = meta['chartPreviousClose']
            change = price - prev_close
            change_pct = (change / prev_close) * 100
            
            # Technical Data
            adj_closes = res['indicators']['adjclose'][0]['adjclose']
            volumes = res['indicators']['quote'][0]['volume']
            
            # Clean up None values
            adj_closes = [c for c in adj_closes if c is not None]
            volumes = [v for v in volumes if v is not None]
            
            # Data Integrity Audit & Validation Rules
            data_verified = True
            
            # The 10% Price Limit Rule & Mismatch Correction
            # If the change exceeds 10% (using 10.5 as a buffer), check for a faulty previous close
            if abs(change_pct) > 10.5:
                # Attempt to correct using the second-to-last item from historical adj_closes
                if len(adj_closes) >= 2:
                    potential_prev_close = adj_closes[-2]
                    new_change = price - potential_prev_close
                    new_change_pct = (new_change / potential_prev_close) * 100 if potential_prev_close else 0
                    
                    if abs(new_change_pct) <= 10.5:
                        # Correction successful
                        prev_close = potential_prev_close
                        change = new_change
                        change_pct = new_change_pct
                    else:
                        # Still exceeds limit, flag as Data Input Error
                        data_verified = False
                else:
                    data_verified = False
            
            # Calculated Field Audit (Sync Check)
            # 1. Current Price - Prev Close = Price Change
            expected_change = price - prev_close
            if abs(change - expected_change) > 0.01:
                change = expected_change
                
            # 2. (Price Change / Prev Close) * 100 = Change %
            expected_change_pct = (change / prev_close) * 100 if prev_close else 0
            if abs(change_pct - expected_change_pct) > 0.01:
                change_pct = expected_change_pct
            
            # 1. Volume Ratio (Current Volume vs 10-day Avg)
            current_vol = volumes[-1] if volumes else 0
            avg_vol_10d = sum(volumes[-11:-1]) / 10 if len(volumes) > 10 else sum(volumes) / len(volumes)
            vol_ratio = current_vol / avg_vol_10d if avg_vol_10d > 0 else 1.0
            
            # 2. RSI (Relative Strength Index - 14 Day)
            rsi = 50.0 # Default
            if len(adj_closes) > 14:
                deltas = [adj_closes[i] - adj_closes[i-1] for i in range(1, len(adj_closes))]
                gains = [d if d > 0 else 0 for d in deltas[-14:]]
                losses = [-d if d < 0 else 0 for d in deltas[-14:]]
                avg_gain = sum(gains) / 14
                avg_loss = sum(losses) / 14
                if avg_loss > 0:
                    rs = avg_gain / avg_loss
                    rsi = 100 - (100 / (1 + rs))
                else:
                    rsi = 100 if avg_gain > 0 else 50
            
            # 3. Short Term Trend (vs 5-day Moving Average)
            ma5 = sum(adj_closes[-5:]) / 5 if len(adj_closes) >= 5 else price
            trend = "Bullish" if price > ma5 else "Bearish"
            
            # Extract Open Price
            open_price = meta.get('regularMarketOpen', prev_close)
            
            return {
                "price": price,
                "prev_close": prev_close,
                "open_price": open_price,
                "change": change,
                "change_pct": change_pct,
                "data_verified": data_verified,
                "metrics": {
                    "rsi": round(rsi, 1),
                    "vol_ratio": round(vol_ratio, 2),
                    "trend": trend,
                    "avg_vol_10d": int(avg_vol_10d)
                }
            }
    except Exception as e:
        # Fallback for some symbols that might need .TWO or .TW
        if symbol.endswith(".TW") and "404" in str(e):
            return get_yfinance_data(symbol.replace(".TW", ".TWO"))
        elif symbol.endswith(".TWO") and "404" in str(e):
            return get_yfinance_data(symbol.replace(".TWO", ".TW"))
        return None

def fetch_market_news():
    # Dynamically load news keywords
    try:
        with open('/root/Desktop/AgenticOS/news_config.json', 'r', encoding='utf-8') as f:
            config = json.load(f)
            keywords = config.get("keywords", "台股 潛力股 趨勢")
    except Exception:
        keywords = "台股 潛力股 趨勢"
        
    encoded_query = urllib.parse.quote(keywords)
    url = f'https://news.google.com/rss/search?q={encoded_query}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant'
    
    results = []
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            tree = ET.fromstring(response.read().decode('utf-8'))
            for item in tree.findall('.//item')[:5]:
                results.append(item.find('title').text)
    except Exception:
        pass
    return results

def get_twse_market_scan():
    url = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, context=ssl_context, timeout=10) as response:
            data = json.loads(response.read().decode('utf-8'))
            
            valid_data = []
            for d in data:
                try:
                    vol = int(d.get('TradeVolume', 0))
                    change_str = str(d.get('Change', '0')).replace('+', '').replace('X', '')
                    change = float(change_str) if change_str else 0.0
                    if '-' in str(d.get('Change', '')):
                        change = -abs(change)
                    valid_data.append((d['Code'], d['Name'], vol, change))
                except:
                    pass
            
            valid_data.sort(key=lambda x: x[2], reverse=True)
            top_volume = valid_data[:30]
            
            valid_data.sort(key=lambda x: x[3])
            top_dips = valid_data[:30]
            
            scan_result = "Top Volume (Code Name Vol Change):\n" + "\n".join([f"{x[0]} {x[1]} {x[2]} {x[3]}" for x in top_volume])
            scan_result += "\n\nTop Dips (Code Name Vol Change):\n" + "\n".join([f"{x[0]} {x[1]} {x[2]} {x[3]}" for x in top_dips])
            return scan_result
    except Exception as e:
        print(f"Error fetching TWSE scan: {e}")
        return "TWSE data unavailable."

def get_ai_analysis(stock_data, news_headlines, twse_scan):
    prompt = f"""
I will provide you with the latest price action, true percentage changes, and high-value technical metrics of specific Taiwanese stocks, today's news headlines, and a scan of notable TWSE stocks.
Your task is to analyze this and output ONLY a JSON object with three keys:
1. "reasons": A dictionary where keys are stock symbols and values are a sophisticated 1-sentence reason for today's price action (in Traditional Chinese). MUST be based on the ACTUAL Change % provided. Do not use extreme terminology like "Market Panic/Crash" for minor changes (e.g., -1.9%). Use terms like "Minor Correction" or "Global Sentiment Pullback". Incorporate technical signals like RSI or Volume Ratio if they are extreme.
2. "news_summary": A list of concise bullet points under the topic "相關產業研究" (Industry Research). Perform a vertical supply chain analysis for the tracked stocks. Identify their upstream (suppliers/IP), downstream (customers/testing), and direct competitors. Highlight undervalued or dip-point opportunities within these clusters based on the technical metrics provided.
3. "suggestions": A single, sophisticated paragraph (in Traditional Chinese) under the topic "其他熱門產業推薦" (Other Trending Industries). Act as a professional market analyst reporting on trending industries or macro shifts that are NOT covered in the rest of the report (e.g., shifts toward real estate, shipping, tourism, or energy). DO NOT include stock tickers (codes) in this paragraph. Focus on the "narrative" of the sector rotation.

Tracked Stock Data (Symbol: Prev Close -> Current Open (Change / Change %) [Metrics]):
{json.dumps(stock_data, ensure_ascii=False, indent=2)}

News Headlines:
{json.dumps(news_headlines, ensure_ascii=False, indent=2)}

TWSE Notable Stocks:
{twse_scan}

RETURN ONLY VALID JSON. Do not use Markdown formatting for the JSON block, just raw JSON.
"""
    data = {
        "model": "anthropic/claude-3.5-sonnet",
        "messages": [{"role": "user", "content": prompt}]
    }
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(data).encode('utf-8'),
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json"
        }
    )
    
    # Bypass SSL locally just in case
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    
    try:
        with urllib.request.urlopen(req, context=ssl_context) as response:
            result = json.loads(response.read().decode('utf-8'))
            content = result['choices'][0]['message']['content'].strip()
            
            # Remove markdown JSON blocks if the model accidentally includes them
            if content.startswith("```json"):
                content = content[7:]
            if content.startswith("```"):
                content = content[3:]
            if content.endswith("```"):
                content = content[:-3]
                
            return json.loads(content.strip())
    except Exception as e:
        print(f"AI API Error: {e}")
        return {"reasons": {}, "suggestions": "目前暫無其他熱門板塊推薦。", "news_summary": ["無法獲取最新AI分析。"]}

def generate_market_report():
    now = datetime.now()
    date_str = now.strftime('%Y-%m-%d %H:%M')
    is_afternoon = now.hour >= 13 # After 1 PM assume closing report
    
    # 1. Market Overview & Global Indices Preparation
    indices_to_fetch = {
        '^TWII': 'twii',
        '^GSPC': 'gspc',
        '^IXIC': 'ixic',
        '^SOX': 'sox',
        '^N225': 'n225'
    }
    
    # 2. Tracked Stocks (Loaded Dynamically)
    tracked_stocks_file = '/root/Desktop/AgenticOS/tracked_stocks.json'
    try:
        with open(tracked_stocks_file, 'r', encoding='utf-8') as f:
            raw_stocks = json.load(f)
            # Handle both nested and flat structures
            if any(isinstance(v, dict) for v in raw_stocks.values()):
                tracked_categories = raw_stocks
                all_tracked_stocks = {}
                for cat in tracked_categories.values():
                    all_tracked_stocks.update(cat)
            else:
                tracked_categories = {"台股重點": raw_stocks}
                all_tracked_stocks = raw_stocks
    except Exception as e:
        print(f"Error loading tracked_stocks.json: {e}. Falling back to default.")
        tracked_categories = {
            "台股重點": {
                "2317": "鴻海",
                "2330": "台積電",
                "2454": "聯發科",
                "2464": "盟立",
                "3576": "聯合再生",
                "6443": "元晶",
                "8039": "台虹",
                "6213": "聯茂",
                "7711": "永擎",
                "3515": "華擎",
                "7810": "創捷科技"
            },
            "台灣ETF名單": {
                "006208": "富邦台50",
                "009802": "富邦旗艦50",
                "009816": "凱基台灣TOP50"
            }
        }
        all_tracked_stocks = {}
        for cat in tracked_categories.values():
            all_tracked_stocks.update(cat)
    
    stock_data = {}
    stock_prompts = {}
    index_results = {}
    
    # 3. Parallel Data Gathering
    print(f"[{datetime.now()}] Starting parallel data fetching...")
    all_symbols = list(indices_to_fetch.keys()) + [f"{code}.TW" for code in all_tracked_stocks.keys()]
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=30) as executor:
        # Submit all tasks
        future_to_yfinance = {executor.submit(get_yfinance_data, sym): sym for sym in all_symbols}
        future_news = executor.submit(fetch_market_news)
        future_twse_scan = executor.submit(get_twse_market_scan)
        
        # Collect yfinance results as they complete
        for future in concurrent.futures.as_completed(future_to_yfinance):
            sym = future_to_yfinance[future]
            try:
                data = future.result()
                if data:
                    if sym in indices_to_fetch:
                        index_results[indices_to_fetch[sym]] = data
                    else:
                        code = sym.split('.')[0]
                        name = all_tracked_stocks.get(code, "Unknown")
                        stock_data[code] = data
                        m = data['metrics']
                        stock_prompts[code] = f"{name}({code}): {data['prev_close']} -> {data['price']} (Change: {data['change']} / Change %: {data['change_pct']:.2f}%) [RSI: {m['rsi']}, VolRatio: {m['vol_ratio']}, Trend: {m['trend']}]"
            except Exception as e:
                print(f"Error in parallel fetch for {sym}: {e}")

        # Finalize news and twse results
        news = future_news.result()
        twse_scan = future_twse_scan.result()
    
    print(f"[{datetime.now()}] Parallel fetching completed.")
    
    # Extract index data
    twii = index_results.get('twii')
    gspc = index_results.get('gspc')
    ixic = index_results.get('ixic')
    sox = index_results.get('sox')
    n225 = index_results.get('n225')
    
    # AI Analysis
    ai_analysis = get_ai_analysis(stock_prompts, news, twse_scan)
    
    # Market Overview Stats
    market_emoji = "🟢 穩健" if twii and twii['change'] >= 0 else "🔴 謹慎"
    twii_text = f"{int(twii['price']):,} 點" if twii else "N/A"
    twii_change = f"{'+' if twii['change']>0 else ''}{twii['change_pct']:.2f}%" if twii else "N/A"
    
    # 4. Load Custom Layout
    try:
        with open('/root/Desktop/AgenticOS/report_layout.json', 'r', encoding='utf-8') as f:
            layout = json.load(f)
    except Exception:
        layout = {
            "title": "📊 開盤快訊" if not is_afternoon else "📊 收盤戰報",
            "market_overview": "市場總覽：",
            "global_indices": "標竿表現：",
            "stock_tracking": "個股表現對比（前日收盤 → 即時報價）：",
            "suggestions": "🚨 其他熱門產業推薦：",
            "news_summary": "💡 相關產業研究："
        }
    
    # Auto-adjust title if default is used
    if layout['title'] == "📊 開盤快訊" and is_afternoon:
        layout['title'] = "📊 收盤戰報"

    # 5. Build Report
    report = []
    
    is_weekend = now.weekday() >= 5
    market_likely_closed = is_weekend or (twii and twii['change'] == 0.0)
    
    if market_likely_closed:
        report.append("⚠️ **【休市提醒】今日台股未開市，以下數據為前一交易日之收盤與結算資訊。**\n")

    report.append(f"{layout.get('title', '📊 股市戰報')}（{date_str} 數據）")
    report.append(f"{layout.get('market_overview', '市場總覽：')}\n")
    report.append(f"• 加權指數：{twii_text}")
    report.append(f"• 漲跌幅：{twii_change}")
    report.append(f"• 市場情緒評估：{market_emoji}\n")
    
    report.append(layout.get('global_indices', '標竿表現：'))
    if gspc: report.append(f"• S&P 500：{gspc['price']:,.2f} ({'+' if gspc['change']>0 else ''}{gspc['change_pct']:.2f}%)")
    if ixic: report.append(f"• Nasdaq：{ixic['price']:,.2f} ({'+' if ixic['change']>0 else ''}{ixic['change_pct']:.2f}%)")
    if sox: report.append(f"• 費城半導體：{sox['price']:,.2f} ({'+' if sox['change']>0 else ''}{sox['change_pct']:.2f}%)")
    if n225: report.append(f"• 日經225：{n225['price']:,.2f} ({'+' if n225['change']>0 else ''}{n225['change_pct']:.2f}%)\n")
    
    # Tracked Categories Section
    for category_name, category_stocks in tracked_categories.items():
        report.append(f"\n**{category_name}：**")
        for code, name in category_stocks.items():
            if code in stock_data:
                d = stock_data[code]
                m = d['metrics']
                action = "漲" if d['change'] >= 0 else "跌"
                reason = ai_analysis.get('reasons', {}).get(code, "市場波動影響。")
                
                label_start = "[昨收]" if not is_afternoon else "[開盤]"
                label_end = "[開盤]" if not is_afternoon else "[收盤]"
                
                start_price = d['prev_close'] if not is_afternoon else d.get('open_price', d['prev_close'])
                
                tech_info = f" [RSI: {m['rsi']} | 量比: {m['vol_ratio']}]"
                verified_tag = " [Data Verified]" if d.get('data_verified', True) else " [Data Input Error]"
                action_sign = "+" if d['change'] > 0 else ""
                
                # Format: [Ticker] [Name]: [Prev Close/Open] → [Opening/Closing] ([Change Amount] / [Change %]) [RSI | Volume Ratio] [Data Verified]
                report.append(f"• **{name} ({code})**：{label_start} {start_price:.1f}元 → {label_end} {d['price']:.1f}元 ({action} {abs(d['change']):.1f}元 / {action_sign}{d['change_pct']:.2f}%){tech_info}{verified_tag}")
                report.append(f"  原因：{reason}")
    
    report.append(f"\n{layout.get('news_summary', '💡 相關產業研究：')}")
    news_summary_data = ai_analysis.get('news_summary', ["暫無重大相關產業研究。"])
    if isinstance(news_summary_data, list):
        for item in news_summary_data:
            report.append(item)
    else:
        report.append(str(news_summary_data))

    report.append(f"\n{layout.get('suggestions', '🚨 其他熱門產業推薦：')}")
    report.append(ai_analysis.get('suggestions', "目前暫無其他熱門板塊推薦。"))
    
    # Append Customization Footer
    report.append("\n---\n🛠️ **個人化設定提示 (Customization Tips):**")
    report.append("您隨時可以透過 Telegram 傳送訊息給我來客製化這份報告！")
    report.append("- **更換追蹤個股：** 例如告訴我「幫我把台化移除，加入聯電(2303)」。")
    report.append("- **修改新聞主題：** 例如告訴我「將新聞焦點改為 AI 伺服器與半導體」。")
    report.append("- **調整報告版面：** 例如告訴我「把『今日操作建議』的標題改成『我的投資筆記』」。")

    final_text = "\n".join(report)
    
    # Save locally as Markdown
    with open('/root/Desktop/AgenticOS/market_report.md', 'w', encoding='utf-8') as f:
        f.write(final_text)
        
    # Save structured data as JSON for programmatic access
    structured_data = {
        "timestamp": now.isoformat(),
        "report_type": "closing" if is_afternoon else "morning",
        "market_overview": {
            "TWII": twii,
            "GSPC": gspc,
            "IXIC": ixic,
            "SOX": sox,
            "N225": n225
        },
        "tracked_stocks": stock_data,
        "ai_analysis": ai_analysis
    }
    with open('/root/Desktop/AgenticOS/market_report_data.json', 'w', encoding='utf-8') as f:
        json.dump(structured_data, f, ensure_ascii=False, indent=2)
        
    # Save to SQLite Database for permanent history
    import sqlite3
    db_path = '/root/Desktop/AgenticOS/market_history.db'
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Create table if it doesn't exist
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS daily_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT UNIQUE,
            report_date TEXT,
            report_type TEXT,
            json_data TEXT,
            markdown_report TEXT
        )
    ''')
    
    # Insert today's report
    cursor.execute('''
        INSERT OR IGNORE INTO daily_reports (timestamp, report_date, report_type, json_data, markdown_report)
        VALUES (?, ?, ?, ?, ?)
    ''', (
        structured_data["timestamp"], 
        now.strftime('%Y-%m-%d'), 
        structured_data["report_type"],
        json.dumps(structured_data, ensure_ascii=False), 
        final_text
    ))
    
    conn.commit()
    conn.close()
    
    return final_text

if __name__ == "__main__":
    print(generate_market_report())
