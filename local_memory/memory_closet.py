import sqlite3
import json
import datetime
from pathlib import Path

class MemoryCloset:
    def __init__(self, domain_name="general", db_dir="/root/Desktop/AgenticOS/local_memory/closets"):
        """
        Initializes a Memory Closet for a specific domain (e.g., 'trading', 'sysadmin', 'openclaw').
        Each domain gets its own SQLite database file.
        """
        self.domain_name = domain_name
        self.db_dir = Path(db_dir)
        self.db_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.db_dir / f"{self.domain_name}_memory.db"
        
        self.conn = sqlite3.connect(self.db_path)
        self._setup_schema()

    def _setup_schema(self):
        """Creates the tables if they don't exist. Uses FTS5 for fast text searching."""
        cursor = self.conn.cursor()
        
        # Phase 1: The Daily Ledger (Raw Logs)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS raw_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                role TEXT,
                content TEXT
            )
        ''')
        
        # Phase 3: The Archive (Compressed Facts) - Using FTS5 for fast search
        # We use a virtual table for full-text search.
        cursor.execute('''
            CREATE VIRTUAL TABLE IF NOT EXISTS facts_archive USING fts5(
                topic,
                summary,
                tags
            )
        ''')
        
        self.conn.commit()

    def log_interaction(self, role, content):
        """Phase 1: Appends a raw interaction to the ledger."""
        cursor = self.conn.cursor()
        timestamp = datetime.datetime.now().isoformat()
        cursor.execute(
            "INSERT INTO raw_logs (timestamp, role, content) VALUES (?, ?, ?)",
            (timestamp, role, content)
        )
        self.conn.commit()

    def store_fact(self, topic, summary, tags=""):
        """Phase 3: Stores a highly compressed fact into the FTS database."""
        cursor = self.conn.cursor()
        cursor.execute(
            "INSERT INTO facts_archive (topic, summary, tags) VALUES (?, ?, ?)",
            (topic, summary, tags)
        )
        self.conn.commit()

    def search_memory(self, query, limit=3):
        """Phase 4: Intelligently recalls facts based on a keyword search."""
        cursor = self.conn.cursor()
        # The FTS match query
        cursor.execute(
            "SELECT topic, summary, tags FROM facts_archive WHERE facts_archive MATCH ? ORDER BY rank LIMIT ?",
            (query, limit)
        )
        results = cursor.fetchall()
        
        formatted_results = []
        for row in results:
            formatted_results.append({
                "topic": row[0],
                "summary": row[1],
                "tags": row[2]
            })
        return formatted_results

    def close(self):
        self.conn.close()

if __name__ == "__main__":
    # Quick Test Run
    print("Testing the Memory Closet System...")
    
    # 1. Create a closet for a specific domain
    trading_closet = MemoryCloset(domain_name="trading")
    
    # 2. Simulate logging some raw conversation (Phase 1)
    trading_closet.log_interaction("user", "Can we build a script to fetch AAPL stock prices?")
    trading_closet.log_interaction("ai", "Yes, I will use yfinance to get the AAPL ticker data.")
    
    # 3. Simulate storing the compressed fact (Phase 3)
    compressed_fact = json.dumps({
        "script_path": "/root/scripts/trading/fetch_aapl.py",
        "api_used": "yfinance",
        "ticker": "AAPL",
        "status": "working"
    })
    trading_closet.store_fact(
        topic="AAPL Stock Fetcher",
        summary=compressed_fact,
        tags="python yfinance aapl script"
    )
    
    # 4. Simulate searching memory months later (Phase 4)
    print("\n--- Searching for 'yfinance' ---")
    results = trading_closet.search_memory("yfinance")
    for r in results:
        print(f"Found Fact: {r['topic']}")
        print(f"Details: {r['summary']}")
        
    trading_closet.close()
    print("\nTest complete! Database created at: /root/Desktop/AgenticOS/local_memory/closets/")
