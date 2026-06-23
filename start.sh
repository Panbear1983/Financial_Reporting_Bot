#!/bin/bash
set -e

python scheduler.py &
SCHEDULER_PID=$!
echo "Started scheduler (PID $SCHEDULER_PID) — OpenClaw native Telegram handles chat"

wait $SCHEDULER_PID
