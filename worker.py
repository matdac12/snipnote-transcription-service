#!/usr/bin/env python3
"""
Background worker for processing transcription jobs

This script can be run as:
1. Cron job (runs once, processes all pending jobs)
2. Continuous loop (keeps running and checks periodically)

For Render cron job:
    python worker.py

For continuous mode:
    python worker.py --continuous
"""

import time
import sys
from jobs import process_pending_jobs


def run_once():
    """
    Run worker once: process all pending jobs and exit
    """
    print("🚀 Starting transcription worker (single run)...")
    process_pending_jobs()
    print("✅ Worker finished\n")


def run_continuous(interval_seconds: int = 60):
    """
    Run worker continuously: check for pending jobs every N seconds

    Args:
        interval_seconds: Time to wait between checks (default 60)
    """
    print(f"🚀 Starting transcription worker (continuous mode, checking every {interval_seconds}s)...")
    print("   Press Ctrl+C to stop\n")

    try:
        while True:
            process_pending_jobs()
            print(f"⏰ Waiting {interval_seconds}s before next check...")
            time.sleep(interval_seconds)
    except KeyboardInterrupt:
        print("\n👋 Worker stopped by user")
        sys.exit(0)


if __name__ == "__main__":
    # Check command line arguments
    if len(sys.argv) > 1 and sys.argv[1] == "--continuous":
        # Continuous mode for local testing
        run_continuous(interval_seconds=60)
    else:
        # Single run mode for cron job
        run_once()
