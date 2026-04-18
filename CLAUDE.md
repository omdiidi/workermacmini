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

## Estimation Quality

- **Trade coverage**: GC mode estimates ALL trades found in documents. The frontend's "Run All Trades" button pre-populates `selected_trades` with all 21 canonical trades (14 UI-selectable + 7 CSI coordination trades). The worker prompt also instructs Claude to discover additional trades via `find_additional=True` on the GC group.
- **Web pricing**: The worker prompt instructs Claude to use WebSearch for real-time vendor pricing. Estimates should include vendor-specific prices, not just internal knowledge.
- **Direct costs only**: Line items are direct costs. The frontend applies markups (overhead, profit, contingency) separately.
- **PDF rasterization (mandatory)**: Each group terminal rasterizes every PDF to ≤1800px PNGs via `pdftoppm -scale-to 1800` before reading. Raw PDF reads via Claude Code's `Read` tool produce page images >2000px on ARCH D drawings, which trip Anthropic's many-image request ceiling (>20 images in conversation → 2000px per-image limit enforced retroactively, session becomes unrecoverable). Rasterizing to 1800px PNGs up front sidesteps this entirely.
- **Cross-trade coordination**: Each group terminal (MEP, ARCH, GC) receives a boundary-specific coordination category block appended to its prompt. Items like HVAC electrical connections, in-wall blocking, fire stopping, ceiling framing, and drywall consumables that commonly fall between trade buckets get caught by the group whose boundary they touch. Categories are defined in `COORDINATION_CATEGORIES` at module level in `worker.py`.
- **Observability — fabricated item detection**: `save_estimate.py` logs a stderr warning AND writes an `anomaly_flags` row with `category="missing_source_refs"` for any item saved without source_refs. After a run, query `anomaly_flags` for this category to catch items Claude invented without documentation.

## Multi-Terminal Architecture

For GC (general contractor) estimates with 5+ trades, the worker launches multiple parallel Terminal windows instead of one session spawning internal sub-agents. Each window is an independent Claude Code session with full WebSearch access and full document reading.

**How it works:**
- **1-4 trades (non-GC)**: Single terminal, runs `/plan2bid:run` (single-pass)
- **5+ trades OR GC mode (`trade="general_contractor"`)**: 3 group terminals in parallel + 1 merge terminal
- **"Run All Trades" button**: frontend pre-populates `selected_trades` with all 21 canonical trades, which always routes to multi-terminal

**Trade groups (worker.py:44-48 `MEP_TRADES` / `ARCH_TRADES` / `GC_TRADES`):**
- **MEP (5):** electrical, plumbing, hvac, fire_protection, low_voltage
- **Architectural (7):** framing, drywall, flooring, painting, roofing, ceiling_systems, doors_hardware
- **GC/Specialty (9):** demolition, concrete, structural_steel, storefront_glazing, signage_graphics, specialties, millwork, general_conditions, landscaping

Each group terminal rasterizes docs, reads PNGs one at a time, uses WebSearch for pricing, and writes `trade_items.json`. The merge terminal reads all group outputs, deduplicates by `item_id`, validates (project-type-aware $/SF ranges, labor ratio, trade coverage), and calls `/plan2bid:save-to-db`.

**Why multi-terminal instead of sub-agents:**
- Sub-agents spawned via the Agent tool can't use WebSearch (0 calls in all worker runs)
- Sub-agents never see the actual drawings — only scope files on disk
- Independent Terminal sessions have full tool access including WebSearch (17-31 calls each)

## Idle Watchdog

The worker's `_launch_claude_terminal` (worker.py:605-647) monitors each terminal's file activity recursively (os.walk over the cwd tree). Two hard-fail conditions:

- **25 minutes of no recursive file activity AND no `estimate_output.json`** → stuck Claude session. Terminal gets killed, job returns `RESULT_TIMEOUT`.
- **`estimate_output.json` exists but hasn't been modified in 15+ minutes** → hung `save-to-db`. Same teardown.

Guarded by `elapsed > 25min` so merge terminals starting from empty cwds aren't falsely killed before they produce output. Brings recovery time on stuck jobs from ~2 hours (`JOB_TIMEOUT`) to ≤30 minutes.

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
