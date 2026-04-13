# Plan2Bid Worker Setup

This is the estimation worker daemon for Plan2Bid. It runs on Mac Minis and opens a **visible Terminal window** with Claude Code for each estimation job.

## Architecture

Workers are **stateless pollers**. Each worker:
1. Polls the `estimation_jobs` table for pending jobs
2. Claims a job with an optimistic lock (prevents double-claiming)
3. Opens Terminal.app with Claude Code to process the job
4. Sends heartbeats every 30s to the `workers` table
5. Cleans up and loops

**The backend API owns the reaper** — stale job detection, expired job cancellation, and offline worker marking all happen in the backend's `reaper.py`, not in the worker. Workers never reap each other's jobs.

**Priority is controlled by polling interval** — primary machines poll every 5s, secondary machines poll every 15s. No inter-worker communication. The database is the sole coordination layer.

**Required repos:**
- This worker repo: https://github.com/omdiidi/workermacmini (you're in it)
- Skills/commands: https://github.com/nkpardon8-prog/claude-dotfiles (cloned to `~/.claude-dotfiles`)

**Prerequisites (must be done BEFORE setup):**
- macOS machine (Mac Mini) — the worker uses `osascript` to open Terminal.app
- A display connected (or VNC/Screen Sharing) — Terminal windows must be able to open
- Claude Code installed: `npm install -g @anthropic-ai/claude-code`
- Logged into Claude Max: run `claude` once interactively and authenticate
- `claude` accessible in PATH — check with `which claude`
- Supabase `estimation_jobs` and `workers` tables created (see README.md for SQL)
- Supabase `project-files` Storage bucket created

## Quick Setup

When asked to "set up this worker", run the setup script:

```bash
# Primary machine (1 worker, 5s poll interval)
bash setup.sh

# Primary machine with 2 workers
bash setup.sh --workers 2 --role primary

# Secondary/backup machine with 2 workers (15s poll interval)
bash setup.sh --workers 2 --role secondary

# Custom poll interval
bash setup.sh --workers 1 --poll-interval 10
```

The script will:
1. Verify `claude` is in PATH
2. Create Python venv and install dependencies
3. Create `.env` from template (prompts to fill in Supabase credentials)
4. Clone/update the skills repo (`~/.claude-dotfiles`)
5. Create log directory
6. Generate and load launchd services for each worker instance

If `.env` doesn't exist yet, the script creates it and exits — fill in the credentials, then re-run.

## Configuration

| Option | Default | Description |
|--------|---------|-------------|
| `--workers N` | 1 | Number of instances (1-3) |
| `--role` | primary | `primary` (5s poll) or `secondary` (15s poll) |
| `--poll-interval N` | auto | Custom poll interval in seconds (overrides role) |
| `--instance N` | 1 | Instance number (set automatically by setup.sh) |

Environment variable overrides (in `.env` or system env):
- `WORKER_ROLE` — same as `--role`
- `POLL_INTERVAL` — same as `--poll-interval`

## Teardown

```bash
bash teardown.sh
```

Stops and removes all launchd worker services. Workers will mark themselves as offline in the database via their shutdown handler.

## Manual Testing

To run a worker directly (without launchd):

```bash
source .venv/bin/activate
python worker.py                          # primary, instance 1
python worker.py --role secondary          # secondary polling
python worker.py --instance 2 --role primary  # instance 2 as primary
```

Press Ctrl+C to stop — the worker will gracefully finish or requeue the current job, mark itself offline, and exit.

## Updating

```bash
# Update worker code
git pull
bash teardown.sh
bash setup.sh --workers 2 --role primary  # re-create with same config

# Update skills (no restart needed)
cd ~/.claude-dotfiles && git pull

# Update Claude Code (no restart needed)
npm update -g @anthropic-ai/claude-code
```

## Verify

```bash
launchctl list | grep plan2bid
tail -f logs/worker-1.log
```

Check the `workers` table in Supabase — you should see one row per instance with regular heartbeats.
