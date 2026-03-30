import sys
import os

# Add the workspace and Desktop directories to path
sys.path.append('/root/.openclaw/workspace')
sys.path.append('/root/Desktop/AgenticOS')

from market_report import generate_market_report

def main():
    # Generate the dynamic report using the crawler
    report_text = generate_market_report()
    
    # Check if report generation was skipped (e.g., weekend/holiday)
    if report_text is None:
        return
        
    # Target recipient: ONLY the Market Report Group
    peer_id = "-4951640836"
    
    try:
        # Escape characters for shell command
        safe_text = report_text.replace('\\', '\\\\').replace('"', '\\"').replace('$', '\\$').replace('`', '\\`')
        
        # The command is 'openclaw message send'
        cmd = f'openclaw message send --channel telegram --target "{peer_id}" --message "{safe_text}"'
        
        exit_code = os.system(cmd)
        if exit_code == 0:
            print(f"Successfully sent to group {peer_id}")
        else:
            print(f"Failed to send to group {peer_id} with exit code {exit_code}")
    except Exception as e:
        print(f"Exception while sending to group {peer_id}: {e}")

if __name__ == "__main__":
    main()
