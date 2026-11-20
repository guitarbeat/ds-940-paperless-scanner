#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOKEN_FILE="${CF_TOKEN_FILE:-$HOME/.caddy_cloudflare_token}"

if [ -f "$TOKEN_FILE" ]; then
    export CF_API_TOKEN="$(cat "$TOKEN_FILE" | tr -d '\r\n')"
fi

CADDY_BIN="${CADDY_BIN:-/opt/homebrew/bin/caddy}"
if [ ! -x "$CADDY_BIN" ]; then
    CADDY_BIN="$(command -v caddy || true)"
fi

if [ -z "$CADDY_BIN" ] || [ ! -x "$CADDY_BIN" ]; then
    echo "Error: Caddy binary not found at /opt/homebrew/bin/caddy or in PATH" >&2
    exit 1
fi

exec "$CADDY_BIN" run --config "${SCRIPT_DIR}/caddy/Caddyfile"
