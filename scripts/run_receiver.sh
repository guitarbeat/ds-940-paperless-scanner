#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="${SCRIPT_DIR}"
export PYTHONUNBUFFERED=1
export SCANNER_IP="${SCANNER_IP:-BR5CF370DC7026.local}"
export SCANNER_HOST="${SCANNER_HOST:-BR5CF370DC7026.local}"
export OUTPUT_DIR="${OUTPUT_DIR:-$HOME/Documents/brother-scans}"
export WORK_DIR="${WORK_DIR:-$SCRIPT_DIR/work}"

mkdir -p "$WORK_DIR" "$OUTPUT_DIR"
cd "$SCRIPT_DIR"

exec /usr/bin/python3 -m ds940.worker
