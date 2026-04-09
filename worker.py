"""Plan2Bid worker daemon — polls estimation_jobs, dispatches to Claude Code."""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from datetime import datetime, timezone

import supabase_client as db

WORKER_ID = f"worker-{socket.gethostname()}"
JOB_TIMEOUT = 1800
POLL_INTERVAL = 5
HEARTBEAT_INTERVAL = 30
STALE_THRESHOLD_MINUTES = 35

_current_job_id = None
_lock = threading.Lock()


def claim_job():
    rows = db.get(
        "estimation_jobs",
        status="eq.pending",
        order="priority.desc,created_at.asc",
        limit="1",
        select="*",
    )
    if not rows:
        return None

    job = rows[0]

    if job.get("expires_at"):
        expires = datetime.fromisoformat(job["expires_at"].replace("Z", "+00:00"))
        if expires < datetime.now(timezone.utc):
            db.patch("estimation_jobs", {"status": "cancelled"}, id=job["id"])
            return None

    updated = db.patch(
        "estimation_jobs",
        {"status": "running", "worker_id": WORKER_ID, "started_at": datetime.now(timezone.utc).isoformat()},
        id=job["id"],
        status="pending",
    )
    if not updated:
        return None

    return updated[0]


def run_job(job):
    global _current_job_id
    job_id = job["id"]
    project_id = job["project_id"]
    job_type = job.get("job_type", "estimation")

    with _lock:
        _current_job_id = job_id

    try:
        if job_type == "scenario":
            _run_scenario_job(job)
        else:
            _run_estimation_job(job)
    finally:
        with _lock:
            _current_job_id = None


def _run_estimation_job(job):
    job_id = job["id"]
    project_id = job["project_id"]
    storage_path = job.get("zip_storage_path", "")

    tmpdir = tempfile.mkdtemp(prefix="plan2bid_")
    try:
        db.patch("projects", {"stage": "ingestion", "progress": 5, "message": "Downloading documents..."}, id=project_id)

        zip_bytes = db.download_storage("project-files", storage_path)
        zip_path = os.path.join(tmpdir, "files.zip")
        with open(zip_path, "wb") as f:
            f.write(zip_bytes)
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(tmpdir)
        os.unlink(zip_path)

        # Clean __MACOSX junk
        macosx = os.path.join(tmpdir, "__MACOSX")
        if os.path.isdir(macosx):
            shutil.rmtree(macosx)

        db.patch("projects", {"stage": "extraction", "progress": 10, "message": "Analyzing documents..."}, id=project_id)

        cmd = [
            "claude", "-p",
            f"Run /plan2bid:run then /plan2bid:save-to-db {project_id}",
            "--dangerously-skip-permissions",
        ]
        result = subprocess.run(
            cmd, cwd=tmpdir, capture_output=True, text=True, timeout=JOB_TIMEOUT,
        )

        if result.returncode != 0:
            error_msg = result.stderr[:2000] if result.stderr else "Non-zero exit code"
            db.patch("estimation_jobs", {
                "status": "error",
                "error_message": error_msg,
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }, id=job_id)
            db.patch("projects", {"status": "error"}, id=project_id)
            return

        db.patch("estimation_jobs", {
            "status": "completed",
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }, id=job_id)
        db.patch("projects", {"status": "completed"}, id=project_id)

    except subprocess.TimeoutExpired:
        db.patch("estimation_jobs", {
            "status": "error",
            "error_message": f"Job timed out after {JOB_TIMEOUT}s",
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }, id=job_id)
        db.patch("projects", {"status": "error"}, id=project_id)

    except Exception as e:
        db.patch("estimation_jobs", {
            "status": "error",
            "error_message": str(e)[:2000],
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }, id=job_id)
        db.patch("projects", {"status": "error"}, id=project_id)

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _run_scenario_job(job):
    job_id = job["id"]
    project_id = job["project_id"]
    scenario_id = job.get("scenario_id")

    tmpdir = tempfile.mkdtemp(prefix="plan2bid_scenario_")
    try:
        db.patch("scenarios", {"status": "running"}, id=scenario_id)

        base_data = _get_base_estimate_data(project_id)
        base_path = os.path.join(tmpdir, "base_estimate.json")
        with open(base_path, "w") as f:
            json.dump(base_data, f)

        scenario_context = job.get("scenario_context", "")
        cmd = [
            "claude", "-p",
            f"Run /plan2bid:scenarios to re-price project {project_id}. "
            f"Scenario context: {scenario_context}\n"
            f"Base estimate is at ./base_estimate.json\n"
            f"When done, run /plan2bid:save-scenario-to-db {scenario_id} {project_id}",
            "--dangerously-skip-permissions",
        ]
        result = subprocess.run(
            cmd, cwd=tmpdir, capture_output=True, text=True, timeout=JOB_TIMEOUT,
        )

        if result.returncode != 0:
            error_msg = result.stderr[:2000] if result.stderr else "Non-zero exit code"
            db.patch("estimation_jobs", {
                "status": "error",
                "error_message": error_msg,
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }, id=job_id)
            db.patch("scenarios", {"status": "error"}, id=scenario_id)
            return

        db.patch("estimation_jobs", {
            "status": "completed",
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }, id=job_id)
        db.patch("scenarios", {"status": "completed"}, id=scenario_id)

    except subprocess.TimeoutExpired:
        db.patch("estimation_jobs", {
            "status": "error",
            "error_message": f"Job timed out after {JOB_TIMEOUT}s",
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }, id=job_id)
        db.patch("scenarios", {"status": "error"}, id=scenario_id)

    except Exception as e:
        db.patch("estimation_jobs", {
            "status": "error",
            "error_message": str(e)[:2000],
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }, id=job_id)
        db.patch("scenarios", {"status": "error"}, id=scenario_id)

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _get_base_estimate_data(project_id):
    materials = db.get("material_items", project_id=f"eq.{project_id}", select="*")
    labor = db.get("labor_items", project_id=f"eq.{project_id}", select="*")
    return {"material_items": materials, "labor_items": labor}


def heartbeat_loop():
    while True:
        try:
            with _lock:
                current = _current_job_id
            status = "busy" if current else "idle"
            db.upsert("workers", {
                "id": WORKER_ID,
                "status": status,
                "current_job_id": current,
                "last_heartbeat": datetime.now(timezone.utc).isoformat(),
            }, on_conflict="id")
        except Exception as e:
            print(f"[heartbeat] error: {e}", file=sys.stderr)
        time.sleep(HEARTBEAT_INTERVAL)


def reap_stale_jobs():
    cutoff = datetime.now(timezone.utc).isoformat()
    rows = db.get(
        "estimation_jobs",
        status="eq.running",
        select="id,started_at",
    )
    for row in rows:
        started = datetime.fromisoformat(row["started_at"].replace("Z", "+00:00"))
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        if elapsed > STALE_THRESHOLD_MINUTES * 60:
            print(f"[reaper] re-queuing stale job {row['id']} (running for {int(elapsed)}s)")
            db.patch("estimation_jobs", {
                "status": "pending",
                "worker_id": None,
                "started_at": None,
                "error_message": f"Re-queued: stale after {int(elapsed)}s",
            }, id=row["id"])


def reap_expired_pending():
    rows = db.get(
        "estimation_jobs",
        status="eq.pending",
        select="id,expires_at",
    )
    now = datetime.now(timezone.utc)
    for row in rows:
        if row.get("expires_at"):
            expires = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
            if expires < now:
                print(f"[reaper] cancelling expired job {row['id']}")
                db.patch("estimation_jobs", {"status": "cancelled"}, id=row["id"])


def main():
    print(f"Worker {WORKER_ID} starting...")

    t = threading.Thread(target=heartbeat_loop, daemon=True)
    t.start()

    stuck = db.get(
        "estimation_jobs",
        status="eq.running",
        worker_id=f"eq.{WORKER_ID}",
        select="id",
    )
    for row in stuck:
        print(f"[recovery] re-queuing stuck job {row['id']}")
        db.patch("estimation_jobs", {
            "status": "pending",
            "worker_id": None,
            "started_at": None,
        }, id=row["id"])

    while True:
        try:
            reap_stale_jobs()
            reap_expired_pending()

            job = claim_job()
            if job:
                print(f"[worker] claimed job {job['id']} (type={job.get('job_type', 'estimation')})")
                run_job(job)
                print(f"[worker] finished job {job['id']}")
            else:
                time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            print(f"\nWorker {WORKER_ID} shutting down.")
            db.upsert("workers", {
                "id": WORKER_ID,
                "status": "offline",
                "current_job_id": None,
                "last_heartbeat": datetime.now(timezone.utc).isoformat(),
            }, on_conflict="id")
            break
        except Exception as e:
            print(f"[worker] error in poll loop: {e}", file=sys.stderr)
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
