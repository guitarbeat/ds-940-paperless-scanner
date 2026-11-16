#!/bin/bash
# Control Brother DS-940DW scanner receiver on Mac Mini
set -euo pipefail

TARGET="aaron@100.96.177.69"
PLIST="~/Library/LaunchAgents/com.aaron.ds940receiver.plist"

cmd="${1:-}"

case "$cmd" in
    start|on)
        echo "🟢 Starting Brother scanner receiver on Mac Mini..."
        ssh "$TARGET" "launchctl load -w $PLIST"
        echo "Receiver is now active."
        ;;
    stop|off)
        echo "🛑 Stopping Brother scanner receiver on Mac Mini..."
        ssh "$TARGET" "launchctl unload $PLIST 2>/dev/null || true"
        echo "Receiver is now stopped."
        ;;
    restart)
        echo "🔄 Restarting Brother scanner receiver on Mac Mini..."
        ssh "$TARGET" "launchctl unload $PLIST 2>/dev/null || true; sleep 1; launchctl load -w $PLIST"
        echo "Receiver restarted."
        ;;
    status)
        STATUS=$(ssh "$TARGET" "launchctl list | grep com.aaron.ds940receiver || true")
        if [ -n "$STATUS" ]; then
            echo "🟢 Brother scanner receiver is RUNNING on Mac Mini."
        else
            echo "⚪ Brother scanner receiver is STOPPED on Mac Mini."
        fi
        ;;
    *)
        echo "Usage: scanner {start|stop|restart|status}"
        exit 1
        ;;
esac
