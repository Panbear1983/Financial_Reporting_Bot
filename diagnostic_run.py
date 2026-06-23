import os
import sys
import json
from twse_daily_report import generate_daily_report

def diagnostic_run():
    print("="*50)
    print("OPENCLAW FINANCIAL BOT: DIAGNOSTIC MODE")
    print("="*50)
    
    # Check Environment
    print("\n[1] Environment Configuration Check:")
    config_vars = ['AGENT_ID', 'OPENROUTER_API_KEY', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID', 'OPENCLAW_DATA_DIR']
    for var in config_vars:
        val = os.getenv(var, 'NOT SET')
        mask = val[:6] + "..." if len(val) > 10 and 'KEY' in var else val
        print(f"  - {var}: {mask}")

    # Trigger the Scraper and Report Generator
    print("\n[2] Executing Scraper and Report Generator Logic...")
    try:
        generate_daily_report()
        
        # Read the generated report
        report_path = os.path.join(os.getenv('OPENCLAW_DATA_DIR', '/app/data'), 'twse_daily_report.md')
        
        if os.path.exists(report_path):
            print("\n[3] DIAGNOSTIC OUTPUT (Daily Report Contents):")
            print("-" * 30)
            with open(report_path, 'r', encoding='utf-8') as f:
                print(f.read())
            print("-" * 30)
            print("\nDiagnostic complete. Report successfully generated and verified.")
        else:
            print(f"\n[ERROR] Report file not found at {report_path}")
            
    except Exception as e:
        print(f"\n[CRITICAL ERROR] Diagnostic run failed: {e}")

if __name__ == "__main__":
    diagnostic_run()
