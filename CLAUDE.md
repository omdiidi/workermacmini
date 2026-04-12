# Plan2Bid Worker Setup

This is the estimation worker daemon for Plan2Bid. It runs on Mac Minis and opens a **visible Terminal window** with Claude Code for each estimation job.

**Required repos:**
- This worker repo: https://github.com/omdiidi/workermacmini (you're in it)
- Skills/commands: https://github.com/nkpardon8-prog/claude-dotfiles (cloned to `~/.claude-dotfiles`)

**Prerequisites (must be done BEFORE setup):**
- macOS machine (Mac Mini) — the worker uses `osascript` to open Terminal.app
- A display connected (or VNC/Screen Sharing) — Terminal windows must be able to open
- Claude Code installed: `npm install -g @anthropic-ai/claude-code`
- Logged into Claude Max: run `claude` once interactively and authenticate
- `claude` accessible in PATH — check with `which claude`. Common locations: `~/.local/bin/claude` or `/usr/local/bin/claude`
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

## 4. Verify claude is in PATH

```bash
claude --version
```

If this fails, check where claude is installed (`find / -name claude -type f 2>/dev/null`) and either add that directory to PATH or create a symlink:
```bash
sudo ln -s $(find ~/.local/bin /usr/local/bin /opt/homebrew/bin -name claude -type f 2>/dev/null | head -1) /usr/local/bin/claude
```

## 5. Set the hostname (optional)

The worker automatically uses the machine's hostname for its ID. If you want a custom name:

```bash
sudo scutil --set HostName plan2bid-{name}
sudo scutil --set LocalHostName plan2bid-{name}
```

This is optional — the default hostname works fine.

## 6. Test the worker

```bash
source .venv/bin/activate
python worker.py
```

It should print "Worker worker-{hostname}-1 starting..." and begin polling. When a job comes in, a Terminal window will open with Claude Code running. Press Ctrl+C to stop the worker once verified.

## 7. Install as background service(s) (macOS launchd)

**Important:** The worker opens Terminal windows, so it must run as the logged-in user (not as a system daemon).

Ask the user how many worker instances to run (1-3, default 1). Each instance processes jobs independently. For each instance N (replace N with 1, 2, or 3):

```bash
mkdir -p ~/Library/LaunchAgents logs

cat > ~/Library/LaunchAgents/com.plan2bid.worker-N.plist << 'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.plan2bid.worker-N</string>
    <key>ProgramArguments</key>
    <array>
        <string>VENV_PYTHON</string>
        <string>WORKER_PY</string>
        <string>--instance</string>
        <string>N</string>
    </array>
    <key>WorkingDirectory</key>
    <string>WORKER_DIR</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>WORKER_DIR/logs/worker-N.log</string>
    <key>StandardErrorPath</key>
    <string>WORKER_DIR/logs/worker-N.err</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin</string>
    </dict>
</dict>
</plist>
PLIST
```

Replace `VENV_PYTHON`, `WORKER_PY`, `WORKER_DIR` with actual absolute paths. Replace `N` with the instance number.

Then load each service:

```bash
launchctl load ~/Library/LaunchAgents/com.plan2bid.worker-N.plist
```

For a single worker, just create instance 1. For 3 workers: create instances 1, 2, and 3.

**Note:** If a trust dialog appears unexpectedly on a multi-instance setup, restart the affected instance — this is a rare race condition when two workers launch simultaneously.

## 8. Verify

```bash
launchctl list | grep plan2bid
tail -f logs/worker-1.log  # or worker-2.log, worker-3.log
```

Check the `workers` table in Supabase — you should see one row per instance (e.g., `worker-plan2bid-minim4-1`, `worker-plan2bid-minim4-2`). Each instance picks up jobs independently from the shared queue.
