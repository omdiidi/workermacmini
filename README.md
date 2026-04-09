# Plan2Bid Worker

Estimation worker daemon for Plan2Bid. Runs on Mac Minis or VMs. Polls Supabase for estimation jobs, spawns Claude Code to run `/plan2bid:run`, saves results back to the database.

**Skills repo:** https://github.com/nkpardon8-prog/claude-dotfiles (cloned to `~/.claude-dotfiles` on each worker)

## How It Connects

```
The Mac Mini connects to ONE thing: Supabase.

[Internet] <-> [Mac Mini] <-> [Supabase (DB + Storage)]

That's it. No connection to the FastAPI server. No connection to the frontend.
No firewall rules. No port forwarding. No VPN. Just internet access.
```

The FastAPI backend writes a job row to Supabase. The worker reads it. The worker writes results to Supabase. The frontend reads them. Everyone talks to Supabase -- never to each other.

---

## Quick Start (Open Claude Code and say "set up this worker")

### Prerequisites
You need TWO things on the Mac Mini before anything else:
1. **Claude Code installed** -- `npm install -g @anthropic-ai/claude-code`
2. **Logged into Claude Max** -- run `claude` once, complete the interactive login

### Then:

Clone this repo on the Mac Mini, open Claude Code in it, and say:

```
Set up this worker. Follow the CLAUDE.md instructions.
```

Claude Code will read the CLAUDE.md below and do everything automatically.

---

## Architecture

### What the worker does

```
while True:
    1. Check estimation_jobs table for pending jobs
    2. If found: claim it (optimistic lock)
    3. Download ZIP from Supabase Storage
    4. Extract to temp directory
    5. Run: claude -p "Run /plan2bid:run then /plan2bid:save-to-db {project_id}" --dangerously-skip-permissions
    6. Claude Code does the full estimation using your skills
    7. /plan2bid:save-to-db calls save_estimate.py which writes to all DB tables
    8. Update job status to completed
    9. Clean up temp files
    10. Back to polling
```

### What the worker does NOT do

- Does not run a web server
- Does not expose any ports
- Does not talk to FastAPI
- Does not need a static IP
- Does not need Docker/Kubernetes

### Files

```
plan2bid-worker/
├── worker.py           # Job polling daemon (~150 lines)
├── supabase_client.py  # Shared PostgREST helpers (~60 lines)
├── save_estimate.py    # Estimate JSON -> DB tables (~250 lines)
├── save_scenario.py    # Scenario JSON -> scenario mirror tables (~150 lines)
├── .env                # SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY
├── .env.example
├── requirements.txt    # httpx, python-dotenv
├── CLAUDE.md           # Setup instructions for Claude Code
└── README.md           # This file
```

---

## Adding More Workers

Adding a worker to the fleet:

1. Get a Mac Mini (or VM)
2. Install Claude Code + log into Max account
3. `git clone` this repo
4. Open Claude Code in it, say "set up this worker"
5. Done

**No changes needed on the backend. No config updates. No load balancer.** The new worker just starts polling the same `estimation_jobs` table and claiming jobs. The optimistic lock ensures two workers never grab the same job.

### Worker fleet example

```
Mac Mini 1 (office shelf)     -> worker-plan2bid-office-1
Mac Mini 2 (office shelf)     -> worker-plan2bid-office-2  
Mac Mini 3 (home)             -> worker-plan2bid-home-1
VM on Hetzner                 -> worker-plan2bid-cloud-1
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

**When to remove a worker:** If a worker's `jobs_completed` in the last 24h is consistently 0.

---

## Updating

### Update worker code
```bash
cd ~/plan2bid-worker
git pull
launchctl unload ~/Library/LaunchAgents/com.plan2bid.worker.plist
launchctl load ~/Library/LaunchAgents/com.plan2bid.worker.plist
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
- Verify `claude -p "hello" --dangerously-skip-permissions` works

**Jobs stuck in "pending":**
- Check `SELECT * FROM workers WHERE last_heartbeat > NOW() - INTERVAL '2 minutes'`
- No alive workers? Start one.
- Workers alive but idle? Check their logs for errors.

**Jobs stuck in "running":**
- The stale job reaper re-queues after 35 minutes automatically
- Manual fix: `UPDATE estimation_jobs SET status='pending', worker_id=NULL WHERE id='...'`

**Claude Code auth expired:**
- SSH into the worker
- Run `claude` interactively to re-authenticate
- Restart the service

**Estimation produces wrong results:**
- The working directory is cleaned up after each job
- To debug: temporarily comment out the cleanup in `worker.py` and check the temp dir for `estimate_output.json` and `analysis/` files

**Worker runs out of disk:**
- Temp directories are cleaned up after each job
- Check `/tmp/` for orphaned `plan2bid_*` directories
- `rm -rf /tmp/plan2bid_*` to clean up
