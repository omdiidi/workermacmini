# Plan2Bid Worker

Estimation and scenario worker daemon for Plan2Bid. Runs on Mac Minis. Polls Supabase for jobs (estimations and what-if scenarios), opens a **visible Terminal window** with Claude Code to run `/plan2bid:run` or `/plan2bid:scenarios`, saves results back to the database.

**Skills repo:** https://github.com/nkpardon8-prog/claude-dotfiles (cloned to `~/.claude-dotfiles` on each worker)

## How It Connects

```
The Mac Mini connects to ONE thing: Supabase.

[Internet] <-> [Mac Mini] <-> [Supabase (DB + Storage)]

That's it. No connection to the FastAPI server. No connection to the frontend.
No firewall rules. No port forwarding. No VPN. Just internet access.
```

The FastAPI backend writes a job row to Supabase. The worker reads it. The worker writes results to Supabase. The frontend reads them. Everyone talks to Supabase — never to each other.

---

## Quick Start (Open Claude Code and say "set up this worker")

### Prerequisites
You need TWO things on the Mac Mini before anything else:
1. **Claude Code installed** — `npm install -g @anthropic-ai/claude-code`
2. **Logged into Claude Max** — run `claude` once, complete the interactive login

### Then:

Clone this repo on the Mac Mini, open Claude Code in it, and say:

```
Set up this worker. Follow the CLAUDE.md instructions.
```

Claude Code will read the CLAUDE.md and do everything automatically.

---

## How It Works

### The worker loop

```
while True:
    1. Check estimation_jobs table for pending jobs (polls every 5s)
    2. If found: claim it (optimistic lock — prevents double-claiming)
    3. Route by job_type:
       - "estimation" → _run_estimation_job (download ZIP, run /plan2bid:run)
       - "scenario"   → _run_scenario_job (fetch base estimate, run /plan2bid:scenarios)
    4. Pre-trust the temp directory in ~/.claude.json (bypasses trust dialog)
    5. Open a VISIBLE Terminal window with:
       claude --dangerously-skip-permissions "{prompt}"
    6. You can WATCH Claude Code work in the Terminal window
    7. Worker polls DB every 15s — detects status change to "completed"
    8. Worker sends /exit to Claude Code, Terminal window closes automatically
    9. Worker cleans up temp files + Claude session data, marks job complete
    10. Back to polling
```

### Estimation jobs
- Downloads ZIP/PDF from Supabase Storage, extracts to temp dir
- Claude runs `/plan2bid:run` → reads docs, extracts items, prices materials, estimates labor
- `/plan2bid:save-to-db` writes results → sets `projects.status = "completed"`

### Scenario jobs (what-if re-pricing)
- Fetches the base estimate data from `material_items`/`labor_items` tables, merges into flat `line_items` format with `is_material`/`is_labor` flags
- Includes project context (location, facility type, trades) in the prompt
- Claude runs `/plan2bid:scenarios` → re-prices affected items based on user's scenario context
- `/plan2bid:save-scenario-to-db` writes results to `scenario_*` tables → sets `scenarios.status = "completed"`
- Scenarios share the same job queue as estimations — 3 workers can process any mix of estimates and scenarios

### Why visible Terminal (not headless)

- **All tools guaranteed loaded** — interactive Claude Code has full access to Skill, Read, Bash, WebSearch, etc.
- **Skills work reliably** — headless `-p` mode sometimes just responds with text instead of executing skills
- **You can watch it** — see exactly what Claude is doing in real time
- **Same as running manually** — identical to you opening Claude Code and typing the command yourself
- **`--dangerously-skip-permissions`** — no permission prompts, Claude just runs

### What the worker does NOT do

- Does not run a web server
- Does not expose any ports
- Does not talk to FastAPI
- Does not need a static IP
- Does not need Docker/Kubernetes

### Important: macOS only

The Terminal launch uses `osascript` (AppleScript) which is macOS-specific. This worker is designed for Mac Minis. For Linux VMs, a different approach would be needed (tmux/screen).

### Files

```
plan2bid-worker/
├── worker.py           # Job polling daemon + Terminal launcher
├── supabase_client.py  # Shared PostgREST helpers
├── save_estimate.py    # Estimate JSON → DB tables (called by /plan2bid:save-to-db, invoked at end of /plan2bid:run)
├── save_scenario.py    # Scenario JSON → scenario mirror tables
├── .env                # SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY (not in git)
├── .env.example
├── requirements.txt    # httpx, python-dotenv
├── CLAUDE.md           # Setup instructions for Claude Code
└── README.md           # This file
```

---

## Adding More Workers

### Adding capacity on the same machine

Each Mac Mini can run up to 3 worker instances:

1. Create additional launchd plists with `--instance 2` and `--instance 3` (see CLAUDE.md step 7)
2. Load them: `launchctl load ~/Library/LaunchAgents/com.plan2bid.worker-2.plist`
3. Each instance gets its own job, log file, and heartbeat

### Adding a new Mac Mini

1. Get a Mac Mini
2. Install Claude Code: `npm install -g @anthropic-ai/claude-code`
3. Log into Claude Max: run `claude` once interactively
4. `git clone https://github.com/omdiidi/workermacmini.git`
5. Open Claude Code in it, say "set up this worker"
6. Done — the new worker immediately starts polling the same job queue

**No changes needed on the backend, database, or other workers.**

### Worker fleet example

```
Mac Mini 1 (office)  → worker-plan2bid-office-1, worker-plan2bid-office-2
Mac Mini 2 (home)    → worker-plan2bid-home-1
```

All polling the same table. All running the same skills. All writing to the same database.

---

## Scaling

| Setup                | Workers | Jobs/Hour (10 min avg) |
|----------------------|---------|------------------------|
| 1 Mini, 1 instance   | 1       | ~6                     |
| 1 Mini, 2 instances  | 2       | ~12                    |
| 1 Mini, 3 instances  | 3       | ~18                    |
| 2 Minis, 3 each      | 6       | ~36                    |

**Note:** All instances on the same machine share one Claude Max account. Throughput may be lower than linear if the account hits rate limits under heavy concurrent use.

**When to add a worker:** If average queue depth during peak hours stays above 5 and users wait > 15 minutes.

---

## Updating

### Update worker code
```bash
cd ~/workermacmini  # or wherever the worker repo is cloned
git pull
# Restart each instance:
launchctl unload ~/Library/LaunchAgents/com.plan2bid.worker-1.plist
launchctl load ~/Library/LaunchAgents/com.plan2bid.worker-1.plist
# Repeat for worker-2, worker-3 if running multiple instances
```

### Update skills (no restart needed)
```bash
cd ~/.claude-dotfiles
git pull
```
Claude Code loads skills fresh on each invocation.

### Update Claude Code (no restart needed)
```bash
npm update -g @anthropic-ai/claude-code
```

---

## Monitoring

### Quick health check (run from any machine with Supabase access)

```sql
-- Worker fleet status
SELECT id, status, last_heartbeat, jobs_completed, jobs_failed FROM workers ORDER BY id;

-- Queue depth
SELECT COUNT(*) AS pending FROM estimation_jobs WHERE status = 'pending';

-- Recent job performance
SELECT 
    status, 
    COUNT(*) AS count,
    AVG(EXTRACT(EPOCH FROM (completed_at - started_at)))::int AS avg_seconds
FROM estimation_jobs 
WHERE created_at > NOW() - INTERVAL '24 hours'
GROUP BY status;
```

### What to watch

| Metric | Healthy | Concerning | Action |
|--------|---------|------------|--------|
| Queue depth | 0-5 | 10+ | Add a worker |
| Worker heartbeat | < 60s ago | > 2 min | Check machine, restart service |
| Job success rate | > 90% | < 80% | Check worker logs |
| Avg job duration | 5-15 min | > 20 min | Check document sizes |

---

## Troubleshooting

**Worker won't start:**
- Check `.env` exists and has correct values
- Run `python worker.py` directly to see error output
- Verify `claude --version` works

**Jobs stuck in "pending":**
- Check `SELECT * FROM workers WHERE last_heartbeat > NOW() - INTERVAL '2 minutes'`
- No alive workers? Start one.
- Workers alive but idle? Check their logs for errors.

**Jobs stuck in "running":**
- The stale job reaper re-queues after 150 minutes automatically
- If Claude is retrying a failed save, this is expected — check the Terminal window
- Manual fix: `UPDATE estimation_jobs SET status='pending', worker_id=NULL WHERE id='...'`

**Scenarios stuck in "pending":**
- Scenarios are dispatched via `estimation_jobs` with `job_type='scenario'`
- Check that the worker is running and polling
- Scenario jobs have a 24-hour `expires_at` TTL — expired jobs are auto-cancelled

**Scenarios stuck in "running":**
- Check the Terminal window for the scenario job
- If the worker crashed, the scenario stays "running" (stale reaper handles `estimation_jobs` but not `scenarios` table)
- Manual fix: `UPDATE scenarios SET status='error', error_message='Manual reset' WHERE id='...'`

**Terminal window doesn't open:**
- Make sure the Mac Mini has a display connected (or VNC/Screen Sharing active)
- Terminal.app must be available in /Applications/Utilities/
- Check if `osascript` works: `osascript -e 'tell application "Terminal" to activate'`

**Claude Code auth expired:**
- Open Terminal manually on the Mac Mini
- Run `claude` interactively to re-authenticate
- Restart the worker

**Estimation produces wrong results:**
- The working directory is cleaned up after each job
- To debug: temporarily comment out the cleanup in `worker.py` and check the temp dir for `estimate_output.json` and `analysis/` files

**Worker runs out of disk:**
- Temp directories are cleaned up after each job
- Claude session data (`~/.claude/projects/` and `~/.claude/session-env/`) is cleaned up automatically after each job
- Check `/tmp/` for orphaned `plan2bid_*` directories: `rm -rf /tmp/plan2bid_*`
- Manual session cleanup: `find ~/.claude/projects/-private-tmp-* -mtime +7 -exec rm -rf {} +`
