# Plan2Bid Worker Setup

This is the estimation worker daemon for Plan2Bid.

**Required repos:**
- This worker repo (you're in it)
- Skills/commands: https://github.com/nkpardon8-prog/claude-dotfiles (cloned to `~/.claude-dotfiles`)

**Prerequisites (must be done BEFORE setup):**
- Claude Code installed: `npm install -g @anthropic-ai/claude-code`
- Logged into Claude Max: run `claude` once interactively and authenticate
- Supabase `estimation_jobs` and `workers` tables created (see README.md for SQL)
- Supabase `project-files` Storage bucket created

When asked to "set up this worker", do the following:

## 1. Install Python dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 2. Clone the Plan2Bid skills (if not already present)

```bash
if [ ! -d ~/.claude-dotfiles ]; then
    git clone https://github.com/nkpardon8-prog/claude-dotfiles.git ~/.claude-dotfiles
    echo "Skills cloned to ~/.claude-dotfiles"
else
    cd ~/.claude-dotfiles && git pull && cd -
    echo "Skills updated"
fi
```

## 3. Set up .env

```bash
if [ ! -f .env ]; then
    cp .env.example .env
    echo "Created .env — fill in your Supabase credentials"
fi
```

Ask the user to fill in `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` in `.env`. These come from the Supabase dashboard (Settings > API).

## 4. Set the hostname (for unique worker ID)

Ask the user what to name this worker (e.g., "worker-1", "worker-office", "worker-mini-m4"). Then:

```bash
sudo scutil --set HostName plan2bid-{name}
sudo scutil --set LocalHostName plan2bid-{name}
```

## 5. Test the worker

```bash
source .venv/bin/activate
python worker.py
```

It should print "Worker worker-plan2bid-{name} starting..." and begin polling. Press Ctrl+C to stop once verified.

## 6. Install as a background service (macOS launchd)

Create the launchd plist:

```bash
mkdir -p ~/Library/LaunchAgents logs

cat > ~/Library/LaunchAgents/com.plan2bid.worker.plist << 'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.plan2bid.worker</string>
    <key>ProgramArguments</key>
    <array>
        <string>VENV_PYTHON</string>
        <string>WORKER_PY</string>
    </array>
    <key>WorkingDirectory</key>
    <string>WORKER_DIR</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>WORKER_DIR/logs/worker.log</string>
    <key>StandardErrorPath</key>
    <string>WORKER_DIR/logs/worker.err</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin</string>
    </dict>
</dict>
</plist>
PLIST
```

Replace `VENV_PYTHON`, `WORKER_PY`, and `WORKER_DIR` with the actual absolute paths for this machine. For example:
- VENV_PYTHON: `/Users/username/plan2bid-worker/.venv/bin/python`
- WORKER_PY: `/Users/username/plan2bid-worker/worker.py`
- WORKER_DIR: `/Users/username/plan2bid-worker`

Then load the service:

```bash
launchctl load ~/Library/LaunchAgents/com.plan2bid.worker.plist
```

## 7. Verify

```bash
launchctl list | grep plan2bid
tail -f logs/worker.log
```

The worker is now running as a background service. It will survive reboots, auto-restart on crashes, and immediately start picking up jobs.
