"""
ms-job-watcher local dashboard
Run: python dashboard/app.py
Then open: http://localhost:5050
"""
import json
from pathlib import Path
from flask import Flask, render_template, jsonify, request

app = Flask(__name__)

JOBS_DB = Path(__file__).parent.parent / "state" / "jobs_db.json"


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
        "roles": {
            "ml_engineer": sum(1 for j in jobs if j.get("role_category") == "ml_engineer"),
            "data_scientist": sum(1 for j in jobs if j.get("role_category") == "data_scientist"),
            "data_engineer": sum(1 for j in jobs if j.get("role_category") == "data_engineer"),
            "data_analyst": sum(1 for j in jobs if j.get("role_category") == "data_analyst"),
            "swe": sum(1 for j in jobs if j.get("role_category") == "swe"),
            "other": sum(1 for j in jobs if j.get("role_category") == "other"),
        }
    })


if __name__ == "__main__":
    print("Dashboard running at http://localhost:5050")
    app.run(host="0.0.0.0", port=5050, debug=False)
