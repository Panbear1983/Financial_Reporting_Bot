#!/bin/bash
set -e

cd "$(dirname "$0")"
source .venv/bin/activate
exec python config_tui.py "$@"
