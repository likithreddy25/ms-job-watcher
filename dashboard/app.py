"""
ms-job-watcher local dashboard
Run: python dashboard/app.py
Then open: http://localhost:5050
"""
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from flask import Flask, render_template, jsonify, request

BASE_DIR = Path(__file__).resolve().parent.parent

# watcher.py's state file paths (STATE_PATH/JOBS_DB_PATH) are plain relative paths
# ("state/seen.json") that assume the process's CWD is the repo root — true when watcher.py
# is invoked directly (`python watcher.py` from repo root, same as GitHub Actions). The
# dashboard didn't previously need this since JOBS_DB below is anchored with Path(__file__),
# but importing watcher for the on-demand scan button (2026-07-11) means we need the same
# guarantee, regardless of the directory this script was launched from.
os.chdir(BASE_DIR)
sys.path.insert(0, str(BASE_DIR))
import watcher  # noqa: E402

app = Flask(__name__)

JOBS_DB = BASE_DIR / "state" / "jobs_db.json"


def load_jobs():
    if not JOBS_DB.exists():
        return []
    with open(JOBS_DB) as f:
        return json.load(f)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/jobs")
def api_jobs():
    jobs = load_jobs()

    bucket = request.args.get("bucket", "")
    role = request.args.get("role", "")
    exp = request.args.get("exp", "")
    clearance = request.args.get("clearance", "")
    h1b = request.args.get("h1b", "")
    bad_tech = request.args.get("bad_tech", "")
    search = request.args.get("search", "").lower()

    filtered = []
    for j in reversed(jobs):  # newest first
        if bucket and bucket != "all" and j.get("bucket") != bucket:
            continue
        if role and j.get("role_category") != role:
            continue
        if exp and j.get("exp_level") != exp:
            continue
        if clearance == "exclude" and j.get("clearance_required"):
            continue
        if h1b and h1b != "all" and j.get("h1b_status") != h1b:
            continue
        if bad_tech == "exclude" and j.get("bad_tech"):
            continue
        if search:
            haystack = f"{j.get('title','')} {j.get('company','')} {j.get('location','')}".lower()
            if search not in haystack:
                continue
        filtered.append(j)

    return jsonify({"total": len(jobs), "filtered": len(filtered), "jobs": filtered})


@app.route("/api/stats")
def api_stats():
    jobs = load_jobs()
    return jsonify({
        "total": len(jobs),
        "yes": sum(1 for j in jobs if j.get("bucket") == "yes"),
        "maybe": sum(1 for j in jobs if j.get("bucket") == "maybe"),
        "clearance": sum(1 for j in jobs if j.get("clearance_required")),
        "h1b_yes": sum(1 for j in jobs if j.get("h1b_status") == "yes"),
        "h1b_unknown": sum(1 for j in jobs if j.get("h1b_status") == "unknown"),
        "opt_friendly": sum(1 for j in jobs if j.get("opt_friendly")),
        "roles": {
            "ml_engineer": sum(1 for j in jobs if j.get("role_category") == "ml_engineer"),
            "data_scientist": sum(1 for j in jobs if j.get("role_category") == "data_scientist"),
            "data_engineer": sum(1 for j in jobs if j.get("role_category") == "data_engineer"),
            "data_analyst": sum(1 for j in jobs if j.get("role_category") == "data_analyst"),
            "swe": sum(1 for j in jobs if j.get("role_category") == "swe"),
            "cloud_engineer": sum(1 for j in jobs if j.get("role_category") == "cloud_engineer"),
            "other": sum(1 for j in jobs if j.get("role_category") == "other"),
        }
    })


@app.route("/api/jobs/today")
def api_jobs_today():
    """Up to 100 jobs found today (date_found == today, UTC), newest first. Pulls from the
    same state/jobs_db.json every pipeline writes to (main-mode direct companies, the boards
    sweep, and the on-demand quick scan below) — so this naturally covers big tech, ATS-board
    startups, and anything the quick scan just found, not just the 12 direct companies."""
    jobs = load_jobs()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    todays = [j for j in jobs if str(j.get("date_found", "")).startswith(today)]
    todays.sort(key=lambda j: j.get("date_found", ""), reverse=True)
    return jsonify({"date": today, "total": len(todays), "jobs": todays[:100]})


# -----------------------------
# On-demand scan ("Run New Scan" button)
# -----------------------------
_scan_lock = threading.Lock()
_scan_state = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "result": None,
    "error": None,
}


def _run_scan_background() -> None:
    try:
        result = watcher.run_quick_company_scan(no_email=True, dry_run=False)
        with _scan_lock:
            _scan_state["result"] = result
            _scan_state["error"] = None
    except Exception as e:
        with _scan_lock:
            _scan_state["error"] = f"{type(e).__name__}: {e}"
            _scan_state["result"] = None
    finally:
        with _scan_lock:
            _scan_state["running"] = False
            _scan_state["finished_at"] = time.time()


@app.route("/api/scan", methods=["POST"])
def api_scan_start():
    with _scan_lock:
        if _scan_state["running"]:
            return jsonify({"ok": False, "message": "A scan is already running."}), 409
        _scan_state["running"] = True
        _scan_state["started_at"] = time.time()
        _scan_state["finished_at"] = None
        _scan_state["result"] = None
        _scan_state["error"] = None
    threading.Thread(target=_run_scan_background, daemon=True).start()
    return jsonify({"ok": True, "message": "Scan started."})


@app.route("/api/scan/status")
def api_scan_status():
    with _scan_lock:
        return jsonify(dict(_scan_state))


if __name__ == "__main__":
    print("Dashboard running at http://localhost:5050")
    app.run(host="0.0.0.0", port=5050, debug=False)
