#!/bin/bash
set -euo pipefail

# Plan2Bid Worker Setup Script
# Provisions N worker instances on a Mac Mini with launchd auto-restart.
#
# Usage:
#   bash setup.sh                          # 1 worker, primary role (5s poll)
#   bash setup.sh --workers 2 --role primary
#   bash setup.sh --workers 3 --role secondary
#   bash setup.sh --workers 1 --poll-interval 10

# --- Defaults ---
NUM_WORKERS=1
ROLE="primary"
POLL_INTERVAL=""

# --- Parse args ---
usage() {
    echo "Usage: bash setup.sh [OPTIONS]"
    echo ""
    echo "Options:"
    echo "  --workers N        Number of worker instances (1-5, default: 1)"
    echo "  --role ROLE        Machine role: primary (5s poll) or secondary (15s poll)"
    echo "  --poll-interval N  Custom poll interval in seconds (overrides --role)"
    echo "  --help             Show this help"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --workers)     NUM_WORKERS="$2"; shift 2 ;;
        --role)        ROLE="$2"; shift 2 ;;
        --poll-interval) POLL_INTERVAL="$2"; shift 2 ;;
        --help)        usage; exit 0 ;;
        *)             echo "Unknown arg: $1"; usage; exit 1 ;;
    esac
done

# --- Validate ---
if ! [[ "$NUM_WORKERS" =~ ^[1-5]$ ]]; then
    echo "Error: --workers must be 1-5"
    exit 1
fi
if ! [[ "$ROLE" =~ ^(primary|secondary)$ ]]; then
    echo "Error: --role must be primary or secondary"
    exit 1
fi

WORKER_DIR="$(cd "$(dirname "$0")" && pwd)"

# Verify we're in the worker repo
if [ ! -f "$WORKER_DIR/worker.py" ]; then
    echo "Error: worker.py not found in $WORKER_DIR"
    echo "Run this script from inside the plan2bid-worker repo."
    exit 1
fi

VENV_DIR="$WORKER_DIR/.venv"
LOG_DIR="$WORKER_DIR/logs"
PLIST_DIR="$HOME/Library/LaunchAgents"
PYTHON="$VENV_DIR/bin/python"
WORKER_PY="$WORKER_DIR/worker.py"

echo "=== Plan2Bid Worker Setup ==="
echo "  Workers: $NUM_WORKERS"
echo "  Role:    $ROLE"
if [ -n "$POLL_INTERVAL" ]; then
    echo "  Poll:    ${POLL_INTERVAL}s (custom)"
else
    if [ "$ROLE" = "primary" ]; then
        echo "  Poll:    5s (primary default)"
    else
        echo "  Poll:    15s (secondary default)"
    fi
fi
echo ""

# --- Step 1: Verify claude is available ---
echo "[1/6] Checking claude CLI..."
if command -v claude &> /dev/null; then
    echo "  Found: $(which claude)"
else
    echo "  Error: claude not found in PATH"
    echo "  Install: npm install -g @anthropic-ai/claude-code"
    echo "  Then log in: claude (complete interactive auth)"
    exit 1
fi

# --- Step 2: Create venv + install deps ---
echo "[2/6] Setting up Python environment..."
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
    echo "  Created venv at $VENV_DIR"
fi
"$VENV_DIR/bin/pip" install -q -r "$WORKER_DIR/requirements.txt"
echo "  Dependencies installed"

# --- Step 3: .env from template ---
echo "[3/6] Checking .env..."
if [ ! -f "$WORKER_DIR/.env" ]; then
    cp "$WORKER_DIR/.env.example" "$WORKER_DIR/.env"
    echo "  Created .env from template"
    echo ""
    echo "  *** ACTION REQUIRED ***"
    echo "  Edit $WORKER_DIR/.env and fill in:"
    echo "    SUPABASE_URL=https://your-project.supabase.co"
    echo "    SUPABASE_SERVICE_ROLE_KEY=eyJ..."
    echo ""
    echo "  Then re-run this script."
    exit 0
else
    echo "  .env exists"
fi

# --- Step 4: Clone/update skills repo ---
echo "[4/6] Checking skills repo..."
SKILLS_REPO="https://github.com/nkpardon8-prog/claude-dotfiles.git"
if [ ! -d ~/.claude-dotfiles ]; then
    git clone "$SKILLS_REPO" ~/.claude-dotfiles
    echo "  Cloned skills to ~/.claude-dotfiles"
else
    (cd ~/.claude-dotfiles && git pull --quiet)
    echo "  Skills updated"
fi

# --- Step 5: Create log directory ---
echo "[5/6] Creating log directory..."
mkdir -p "$LOG_DIR"
echo "  Logs at $LOG_DIR"

# --- Step 6: Generate and load launchd plists ---
echo "[6/6] Installing launchd services..."
mkdir -p "$PLIST_DIR"

for i in $(seq 1 "$NUM_WORKERS"); do
    LABEL="com.plan2bid.worker-$i"
    PLIST="$PLIST_DIR/$LABEL.plist"

    # Unload existing if present
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true

    # Build ProgramArguments
    ARGS_XML="        <string>$PYTHON</string>
        <string>$WORKER_PY</string>
        <string>--instance</string>
        <string>$i</string>
        <string>--role</string>
        <string>$ROLE</string>"

    if [ -n "$POLL_INTERVAL" ]; then
        ARGS_XML="$ARGS_XML
        <string>--poll-interval</string>
        <string>$POLL_INTERVAL</string>"
    fi

    cat > "$PLIST" << PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
$ARGS_XML
    </array>
    <key>WorkingDirectory</key>
    <string>$WORKER_DIR</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>ThrottleInterval</key>
    <integer>30</integer>
    <key>StandardOutPath</key>
    <string>$LOG_DIR/worker-$i.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/worker-$i.err</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin:$HOME/.local/bin</string>
    </dict>
</dict>
</plist>
PLIST_EOF

    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    echo "  Loaded $LABEL (instance $i, role=$ROLE)"
done

echo ""
echo "=== Setup Complete ==="
echo "$NUM_WORKERS worker(s) running as $ROLE."
echo ""
echo "Check status:  launchctl list | grep plan2bid"
echo "View logs:     tail -f $LOG_DIR/worker-1.log"
echo "Stop workers:  bash teardown.sh"
