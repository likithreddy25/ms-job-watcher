# ms-job-watcher

Automated job-alert pipeline that polls 6 major company career APIs (Microsoft, NVIDIA, Amazon, Goldman Sachs, IBM, Oracle) plus ~1,200 curated ATS job boards (Greenhouse, Lever, SmartRecruiters, Workday, Ashby), filters for US-based entry/mid-level software engineering roles, deduplicates against persisted state, and emails a digest of new matches. Runs unattended on a schedule via GitHub Actions.

## Why this exists

Manually re-checking career pages misses postings that fill within days. This runs in the background, applies a consistent filter (title, seniority, location, sponsorship/clearance exclusions), and only surfaces roles worth acting on.

## How it works

Two independent pipelines share one entrypoint (watcher.py):

Main pipeline (--mode main, ~every 10 min)
- Fetches 6 hardcoded company career APIs directly (Microsoft/NVIDIA via Eightfold, Amazon Jobs, Goldman Sachs GraphQL, IBM Search API, Oracle HCM), paginated per source
- - Classifies each title yes / maybe / no with a rule-based filter -- hard excludes (internships, QA, sales, marketing, clearance/citizenship-required, non-sponsoring), soft excludes (DevOps/ops unless paired with a strong SWE signal), seniority downgrade for senior+ titles
  - - Filters to US locations via a heuristic location parser
    - - Deduplicates against state/seen.json and emails only genuinely new matches
     
      - Boards pipeline (--mode boards, ~every 30 min)
      - - Sweeps ~1,200 curated ATS boards across 5 platforms in cursor-based batches of 200, so a full sweep completes over several hours without overrunning the Actions timeout
        - - Fetches concurrently with a ThreadPoolExecutor and per-platform semaphores (Workday capped lower -- it needs a boot request plus paginated POSTs per board)
          - - A board's first-ever sweep is bootstrapped silently (jobs absorbed into seen-state, no alert) to avoid a flood of "new" jobs on day one
            - - Boards that return 404/410 are marked dead and skipped on future sweeps
              - - Deduplicates against state/seen_boards.json and emails only new matches
               
                - Both pipelines apply the same title classifier and location filter, send a digest via Gmail SMTP, and commit their updated state files back to the repo so state survives across ephemeral GitHub Actions runners.
               
                - ## Scheduling
               
                - GitHub Actions' native schedule cron is unreliable at short intervals under load. Production scheduling instead uses an external cron service (cron-job.org) hitting the workflow_dispatch API on a fixed interval, with GitHub's own cron kept only as a sparse fallback.
               
                - ## Tech stack
               
                - Python 3.11, requests + urllib3 (retry/backoff session pooling), BeautifulSoup4, GitHub Actions, Gmail SMTP, Flask dashboard for reviewing state, atomic JSON state persistence (tempfile + os.replace)
               
                - ## Repo layout
               
                - watcher.py - single-file pipeline core: fetch, classify, filter, dedupe, email, state I/O
                - dashboard/ - Flask app for browsing seen jobs and run history
                - backfill_dashboard.py - rebuilds dashboard data from historical state
                - data/boards/ - curated board CSVs (live list + historical staging files)
                - state/ - persisted JSON state (seen IDs, cursor, dead boards, run log)
                - docs/ARCHITECTURE.md - architecture reference
                - docs/STATE.md - running session/decision log
               
                - ## Setup
               
                - python -m venv .venv && source .venv/bin/activate
                - pip install -r requirements.txt
               
                - Required env vars (or GitHub Actions secrets): EMAIL_USER, EMAIL_APP_PASSWORD, ALERT_TO_EMAIL
               
                - python watcher.py --mode main --test-email
                - python watcher.py --mode boards --boards-batch-size 200
               
                - ## Status
               
                - Actively running in production on a schedule, with automated state commits from both scheduled workflows tracking tens of thousands of seen job IDs across both pipelines.
                - 
