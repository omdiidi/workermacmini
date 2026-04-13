#!/bin/bash
set -euo pipefail

# Plan2Bid Worker Teardown Script
# Stops and removes all worker launchd services.

PLIST_DIR="$HOME/Library/LaunchAgents"
REMOVED=0

for i in 1 2 3; do
    LABEL="com.plan2bid.worker-$i"
    PLIST="$PLIST_DIR/$LABEL.plist"
    if [ -f "$PLIST" ]; then
        launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
        rm -f "$PLIST"
        echo "Removed $LABEL"
        REMOVED=$((REMOVED + 1))
    fi
done

if [ "$REMOVED" -eq 0 ]; then
    echo "No plan2bid worker services found."
else
    echo ""
    echo "$REMOVED worker service(s) stopped and removed."
    echo "Workers will set themselves to 'offline' in the database."
fi
