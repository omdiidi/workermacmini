# Plan2Bid Worker

Estimation worker daemon for Plan2Bid. Runs on Mac Minis. Polls Supabase for estimation jobs, opens a **visible Terminal window** with Claude Code to run `/plan2bid:run`, saves results back to the database.

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
    3. Download ZIP/PDF from Supabase Storage
    4. Extract to temp directory (ZIPs) or leave as-is (single files)
    5. Pre-trust the directory in ~/.claude.json (bypasses trust dialog)
    6. Open a VISIBLE Terminal window with:
       claude --dangerously-skip-permissions "Run /plan2bid:run ... then /plan2bid:save-to-db {project_id}"
    7. You can WATCH Claude Code work in the Terminal window
    8. Claude reads docs, extracts items, prices materials, estimates labor
    9. /plan2bid:save-to-db calls save_estimate.py → sets project.status = "completed"
    10. Worker polls DB every 15s — detects status change to "completed"
        (ignores "error" — Claude may be retrying a failed save)
    11. Worker sends double Ctrl+C to exit Claude, Terminal window closes automatically
    12. Worker cleans up temp files + Claude session data, marks job complete
    13. Back to polling
```

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
├── save_estimate.py    # Estimate JSON → DB tables (called by /plan2bid:save-to-db skill)
├── save_scenario.py    # Scenario JSON → scenario mirror tables
├── .env                # SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY (not in git)
├── .env.example
├── requirements.txt    # httpx, python-dotenv
├── CLAUDE.md           # Setup instructions for Claude Code
└── README.md           # This file
```

---

## Adding More Workers

1. Get a Mac Mini
2. Install Claude Code + log into Max account
3. `git clone https://github.com/omdiidi/workermacmini.git`
4. Open Claude Code in it, say "set up this worker"
5. Done

**No changes needed on the backend. No config updates. No load balancer.** The new worker just starts polling the same `estimation_jobs` table and claiming jobs.

### Worker fleet example

```
Mac Mini 1 (office shelf)     → worker-plan2bid-office-1
Mac Mini 2 (office shelf)     → worker-plan2bid-office-2  
Mac Mini 3 (home)             → worker-plan2bid-home-1
```

All polling the same table. All running the same skills. All writing to the same database.

---

## Scaling

| Workers | Jobs/Hour (10 min avg) | Cost/Month (Max plan) |
|---------|------------------------|-----------------------|
| 1       | ~6                     | $100-200              |
| 2       | ~12                    | $200-400              |
| 3       | ~18                    | $300-600              |
| 5       | ~30                    | $500-1000             |

**When to add a worker:** If average queue depth during peak hours stays above 5 and users wait > 15 minutes.

---

## Updating

### Update worker code
```bash
cd ~/plan2bid-worker
git pull
# Restart worker (Ctrl+C then python worker.py, or reload launchd service)
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
