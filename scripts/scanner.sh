#!/bin/bash
# Control Brother DS-940DW scanner receiver on local machine or remote hub
set -euo pipefail

TARGET="${SCANNER_SSH_TARGET:-}"

run_cmd() {
    local cmd="$1"
    if [ -n "$TARGET" ]; then
        ssh "$TARGET" "$cmd"
    else
        eval "$cmd"
    fi
}

cmd="${1:-}"

case "$cmd" in
    start|on)
        echo "🟢 Starting Brother scanner receiver..."
        run_cmd '
            PLIST=$(ls "$HOME/Library/LaunchAgents/"*ds940receiver.plist 2>/dev/null | head -n 1)
            if [ -n "$PLIST" ]; then
                launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
                launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || launchctl load -w "$PLIST" 2>/dev/null || true
            fi
        '
        echo "Receiver is now active."
        ;;
    stop|off)
        echo "🛑 Stopping Brother scanner receiver..."
        run_cmd '
            PLIST=$(ls "$HOME/Library/LaunchAgents/"*ds940receiver.plist 2>/dev/null | head -n 1)
            if [ -n "$PLIST" ]; then
                launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || launchctl unload "$PLIST" 2>/dev/null || true
            fi
        '
        echo "Receiver is now stopped."
        ;;
    restart)
        echo "🔄 Restarting Brother scanner receiver..."
        run_cmd '
            PLIST=$(ls "$HOME/Library/LaunchAgents/"*ds940receiver.plist 2>/dev/null | head -n 1)
            if [ -n "$PLIST" ]; then
                launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || launchctl unload "$PLIST" 2>/dev/null || true
                sleep 1
                launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || launchctl load -w "$PLIST" 2>/dev/null || true
            fi
        '
        echo "Receiver restarted."
        ;;
    status)
        STATUS=$(run_cmd "launchctl list | grep ds940receiver || true")
        if [ -n "$STATUS" ]; then
            echo "🟢 Brother scanner receiver is RUNNING."
        else
            echo "⚪ Brother scanner receiver is STOPPED."
        fi
        ;;
    *)
        echo "Usage: scanner {start|stop|restart|status}"
        echo ""
        echo "Optional Environment Variables:"
        echo "  SCANNER_SSH_TARGET   Remote host (e.g. user@mac-mini.local) if running daemon on a separate hub"
        exit 1
        ;;
esac
