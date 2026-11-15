#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p "${SCRIPT_DIR}/bin"

echo "Compiling native Apple Vision OCR tool with swiftc..."
swiftc -O "${SCRIPT_DIR}/tools/ocr.swift" -o "${SCRIPT_DIR}/bin/ds940-ocr"
chmod +x "${SCRIPT_DIR}/bin/ds940-ocr"
echo "Build successful: ${SCRIPT_DIR}/bin/ds940-ocr"
