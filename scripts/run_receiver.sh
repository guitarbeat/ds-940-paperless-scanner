#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="${SCRIPT_DIR}"
export PYTHONUNBUFFERED=1
export SCANNER_IP="${SCANNER_IP:-BR5CF370DC7026.local}"
export SCANNER_HOST="${SCANNER_HOST:-BR5CF370DC7026.local}"
ICLOUD_SCANS="$HOME/Library/Mobile Documents/com~apple~CloudDocs/brother-scans"
if [ -d "$ICLOUD_SCANS" ]; then
    DEFAULT_OUTPUT="$ICLOUD_SCANS"
else
    DEFAULT_OUTPUT="$HOME/Documents/brother-scans"
fi
export OUTPUT_DIR="${OUTPUT_DIR:-$DEFAULT_OUTPUT}"
export WORK_DIR="${WORK_DIR:-$SCRIPT_DIR/work}"

mkdir -p "$WORK_DIR" "$OUTPUT_DIR"
cd "$SCRIPT_DIR"

exec /usr/bin/python3 -m ds940.worker
