# ms-job-watcher — Session Handoff

## Current status

**Three pipelines running as of 2026-07-01 — ~7,366 total boards.** External triggering via cron-job.org verified for all three. Pipeline 1 (`--mode main`) polls Microsoft, NVIDIA, Amazon, Goldman Sachs, IBM, and Oracle (10-min cadence). Pipeline 2 (`boards.yml`) sweeps 1,200 ATS boards — GH/Lever/SR/Workday/Ashby — in batches of 200 (30-min cadence). **Pipeline 3 (`boards2.yml`) is LIVE** — sweeps 6,166 net-new GH+Lever boards in batches of 2,000 (30-min cadence); first `workflow_dispatch` run landed 2026-07-01T16:39Z (success, 19s, 4 new jobs emailed, cursor→2000).

> **PAT SECURITY REMINDER (2026-07-01):** The fine-grained PAT used to trigger all three cron-job.org jobs was **exposed in a screenshot on 2026-07-01**. It expires **2026-08-31** — rotate it well before that date. On rotation: update the `Authorization: Bearer <token>` header in **all three** cron-job.org jobs (watcher, boards, boards2). If all three pipelines go silent simultaneously, the PAT is the first suspect.

## Open bugs / issues

- [x] **External scheduling via cron-job.org is live and verified in production.** GitHub cron deprioritization confirmed (median 268/273 min despite 10/30-min target). Switched to cron-job.org → `workflow_dispatch` API: watcher every 10 min, boards every 30 min. Verified via `gh run list`: watcher `workflow_dispatch` runs landed at 20:40 and 20:50 UTC on Jun 2, exactly 10 min apart, all success. Boards dispatch confirmed (204 + successful run). GitHub `schedule:` downgraded to sparse fallback (`13 */3 * * *`). PAT expires 2026-08-31.
- [ ] **Dead-board single-strike permanent marking — no resurrection.** One 404/410 = dead forever. 16 boards in current CSV are marked dead; some may be transient failures. Implement N-strikes (3 consecutive) or monthly TTL re-probe.
- [ ] **`boards_dead.json` has 921 orphaned entries (stale, not wasting throughput but misleading).** Only 16 of 937 entries overlap with the current CSV. Prune to match live CSV.
- [ ] **Large untapped board pool.** `greenhouse_us_verified.csv` (4,659 rows), `lever_us_verified.csv` (1,806 rows), `workday_us_verified.csv` (4,770 rows) — none ingested. Verify first, add in tranches.
- [ ] **Gmail account mismatch.** Connected Gmail is the wrong account; the alerts inbox hasn't been analyzed. Reconnect the correct inbox before doing Gmail-based funnel analysis.

## Next steps

1. **After ~1 day of live dispatch runs, check `run_log.json` funnel data** — look at per-source `title_ok` and `loc_ok` counts to confirm the title classifier isn't over-dropping. If a source's "kept" count drops sharply after a filter tweak, that's the signal. Recall-first: err toward alerting.
2. **Reconnect the correct Gmail inbox**, then analyze which boards actually produce relevant alerts.
3. **Selectively ingest from the ~10k curated lists** (greenhouse/lever/workday_us_verified) — verify first, add in tranches; do NOT bulk-add (cycle staleness wrecks latency).
4. **[low] Dead-board resurrection + prune orphaned entries** — implement N-strikes (3 consecutive 404s) or monthly TTL re-probe instead of single-strike permanent marking; prune `boards_dead.json` down to the 16 entries that actually overlap the current CSV (921 are stale orphans).
5. **[optional, deferred by choice] Automated test harness** — pytest on `classify_title` / `is_us_location` was considered and deliberately deferred. `run_log.json` is the lightweight safety net for regressions. Not urgent unless the classifier is changed.

## Key facts & gotchas

- **Single file:** all logic lives in `watcher.py` (~2,160 lines after pagination additions). No external modules beyond `requests`.
- **State is committed to git** by the `github-actions` bot after every run. Push conflicts handled by a 5-retry loop with `git merge -X ours`. Remote state is always source of truth; pull before editing state files locally.
- **Scheduling is now EXTERNAL** — cron-job.org calls the `workflow_dispatch` API (watcher every 10 min, boards/boards2 every 30 min). The GitHub `schedule:` cron is a sparse fallback only. Auth = a fine-grained PAT (this repo, Actions:write) stored in **all three** cron-job.org jobs; it **EXPIRES 2026-08-31** and was **EXPOSED IN A SCREENSHOT 2026-07-01** — rotate before expiry and update all three jobs' Authorization header. If all pipelines go silent at once, the PAT is the first suspect.
- **Dead boards: single-strike permanent.** One 404/410 → `boards_dead.add(board_id)` → skipped forever. No retry logic.
- **Dead boards: 921 of 937 are orphaned stale entries.** Only **16 boards** in the current 1,200-row CSV are actually dead. The other 921 are from boards removed in earlier CSV versions — they don't slow down batches.
- **Oracle was broken since day one.** `fetch_oracle` was returning the search container (`items` list, each a dict with `SearchId`, `Keyword`, etc.) instead of `items[0].get("requisitionList")`. This produced `oracle:url:` junk keys and 0 Oracle jobs ever entering `seen.json`. Fixed in commit `804f627b`.
- **GS/IBM/Oracle now paginate.** GS uses `pageNumber` increment; IBM uses `from` offset (Elasticsearch); Oracle uses `limit=50,offset=N` embedded in the finder query string. All three short-circuit when a full page is already in `seen_keys`.
- **New board bootstrap suppresses first-run alerts.** When a board is seen for the first time, all current jobs are added to `seen` silently — no email. Alert lag until second sweep.
- **Cursor persists in `state/boards_cursor.json`.** Wraps to 0 after reaching end of CSV. Full cycle = `ceil(n_boards / batch_size)` runs.
- **Workday URL normalization is complex.** `workday_normalize_external_job_url` handles 5 path shapes. Bugs here produce unclickable links in emails.
- **US location filtering** uses state abbreviation regex + ISO 3166 country-code blocklist. International cities with US-state-like abbreviations were fixed Apr 1 2026.
- **Concurrency:** ThreadPoolExecutor with per-platform semaphores (GH=8, Lever=8, SR=6, WD=4, Ashby=6). Workday is most restrictive.
- **Email:** Gmail SMTP SSL on port 465. Secrets: `EMAIL_USER`, `EMAIL_APP_PASSWORD`, `ALERT_TO_EMAIL`. Subject prefixes per pipeline:
  | Pipeline | Subject prefix | Gmail search |
  |---|---|---|
  | watcher (main) | `[Job Alerts]` | `subject:[Job Alerts]` |
  | boards (boards.yml) | `[Boards Alerts]` | `subject:[Boards Alerts]` |
  | boards2 (boards2.yml) | `[Boards2 Alerts]` | `subject:[Boards2 Alerts]` |
  Override via `SUBJECT_PREFIX` env var in the workflow — absent = falls back to `[Boards Alerts]`.
- **Recall-first philosophy:** a missed job (false negative) is expensive; a junk alert (false positive) is cheap. When in doubt, err toward alerting.
- **Security:** Full git-history scan was clean (no secrets ever committed). `.gitleaks.toml` added with `[extend] useDefault = true` + allowlist for `state/*.json`. The local-only `data/boards/workday_debug/` directory contains HAR files with expired AWS STS credentials — never committed, optional cleanup: `rm -rf data/boards/workday_debug/`.
- **Full architecture reference:** see `docs/ARCHITECTURE.md` — repo map, full function index, runtime traces, external API surface, and ranked risk findings.

## Backlog (later — not urgent)

Evidence gathered 2026-06-02 from ~10 hrs of live dispatch runs (29 main runs, 14 boards runs).
The boards lane is healthy (~250 emails over the window). All items below are main-mode curated-lane gaps.
Note: 100% location pass on Microsoft/Amazon/NVIDIA is expected — those queries are US-filtered upstream, not a bug.

- **Oracle — 0 fetched in every run despite the `804f627b` fix.** Zero Oracle coverage in production. Diagnosis when revisited: run `fetch_oracle` in isolation, inspect the raw API response + extraction key, confirm the fix actually landed in the deployed function.

- **Goldman Sachs — under-fetch + loc_ok=0 always.** Only ~2 jobs fetched per run (single page — pagination likely still broken), and the 1 title-passing job fails `is_us_location` every run. GS is a US company (NYC HQ); suspected location-string format the regex doesn't match. Also threw 403 errors in 2 of 29 runs. Fix plan: fix pagination first, then eyeball real location strings on the larger corpus to diagnose the regex miss.

- **NVIDIA fetches exactly 20, Amazon exactly 300 every run.** Round, stable counts that smell like un-paginated single-page results or hard caps silently truncating the full listing. Confirm both sources paginate to completion (or document why the count is correct).

- **[low] Boards recall spot-check.** Title pass rates (23–37%) and location drop-offs after title (Ashby 24%, Workday 26%) look like normal filtering of all-department global boards, but that's unverified. Someday: eyeball a sample of title-rejected and location-rejected jobs on one high-volume source to confirm the filters aren't dropping real US engineering roles.

## Board expansion — DONE (GH/Lever shard live 2026-07-01)

### Architecture: multi-pipeline sharding (implemented)
The existing 1,200-board pipeline (`boards.yml`) remains **untouched** as the fast lane. New coverage was added as a **separate parallel pipeline** (`boards2.yml`) with its own disjoint CSV, cursor, seen-file, and cron-job.org trigger. This keeps the 1,200 truly untouched and fault-isolated.

**Current system: 3 pipelines, ~7,366 boards total**
| Pipeline | Workflow | Boards | Cadence | Batch size |
|---|---|---|---|---|
| watcher | `watcher.yml` | 6 hardcoded companies | 10 min | — |
| boards | `boards.yml` | 1,200 (GH/Lever/SR/WD/Ashby) | 30 min | 200 |
| boards2 | `boards2.yml` | 6,166 (GH+Lever net-new) | 30 min | 2,000 |

### First move: Greenhouse + Lever — COMPLETE
Lead with GH + Lever: cheapest platforms (1 GET/board, ~18 boards/sec on no-WD batches). Liveness-verified 6,407 candidates → 6,166 alive (96.2%), bootstrapped silently, deployed 2026-07-01. First live run 16:39Z: 2,000 boards, 4 new jobs emailed.

Workday = cost driver (4–26 API calls/board, no cheap change-detection): sized and ready to wire. See Workday sizing section below.

### Measurement findings (2026-06-02, from run_log.json + watcher.py inspection)
- **Huge headroom:** 200-board runs finish ~95s avg / 126s max of the 900s timeout (~14% used). Batch 200 is very conservative — adding boards need not hurt per-run latency if batch size scales up.
- **Per-board API cost:** GH = 1 GET; Lever = 1 GET; Ashby = 1 POST; SmartRecruiters = 1–5 GETs; Workday = 1 GET (boot) + 4–25 POSTs. Workday is "the clock."
- **Observed throughput:** ~2.1 boards/sec average; ~18 boards/sec on no-Workday batches (cursor 1000–1200 slice, 0 WD boards, ran in 11.2s).
- **Rate limits:** No throttling evidence from any boards platform at current load; 429s auto-retried transparently; only Goldman Sachs (main mode) threw 403s.
- **Change-detection:** GH & Lever = easy (ETag/304 conditional GET); SmartRecruiters = read `totalFound` on first page and bail early; Ashby & Workday = no HTTP path (both POST), would need app-level count/ID caching.

### Inventory — RESOLVED (2026-07-01)
Verified lists carry **only `company`, `platform`, `board_url`** — no industry, size, or location metadata. Sector/size targeting is NOT possible from these lists alone; needs external enrichment or job-text-level filtering.

**Root cause of prior "zero overlap" false alarm:** Greenhouse hostname differs between the two sources — live CSV uses `boards.greenhouse.io`, verified list uses `job-boards.greenhouse.io`. Comparing raw URLs gave zero overlap even for real matches. Fix: canonical dedup key is `urlparse(board_url).path.split('/')[1].lower()` applied to both sides (strips hostname, takes first path segment, lowercases). Lever URLs are identical on both sides (`jobs.lever.co`); no fix needed there.

**Net-new counts (canonical-slug dedup, confirmed 2026-07-01):**
| Platform | Verified total | Already in live 1,200 | Net-new |
|---|---|---|---|
| Greenhouse | 4,659 | 0 | **4,659** |
| Lever | 1,806 | 0 | **1,806** |
| Combined | 6,465 | 0 | **6,465** |

Zero overlap is genuine — the two pools were seeded from different candidate sources, so all net-new boards represent real new coverage. Zero internal duplicates in either list.

**Data-quality caveat:** ~58 GH slugs (~1.2%) are noise — purely numeric IDs (e.g. `103644278`, `123456789101010`) or >30-char garbage strings. Filter before ingestion.

### GH/Lever shard — liveness-verified, ready to wire up (2026-07-01)

**Liveness probe results (run 2026-07-01, 73s wall time, 24 workers):**
| | Total rows | Net-new vs live 1,200 | Junk dropped | Probed | Alive | Dead |
|---|---|---|---|---|---|---|
| Greenhouse | 4,659 | 4,659 | 58 (numeric/long slugs) | 4,601 | **4,417 (96.0%)** | 184 |
| Lever | 1,806 | 1,806 | 0 | 1,806 | **1,749 (96.8%)** | 57 |
| **Combined** | **6,465** | **6,465** | **58** | **6,407** | **6,166 (96.2%)** | **241** |

All 241 dead boards were clean 404s — zero timeouts, zero retries needed. Output CSV: **`data/boards/greenhouse_lever_verified_live.csv`** (6,166 rows, same column format as live boards CSV: `company_name, platform, board_url, country_focus, notes`).

**Pipeline built and seeded (2026-07-01):** `boards2.yml` created, all state files bootstrapped locally and committed.

| File | Role | Value at commit |
|---|---|---|
| `data/boards/greenhouse_lever_verified_live.csv` | boards2 CSV | 6,166 boards (GH 4,417 + Lever 1,749) |
| `state/seen_boards2.json` | job dedup (seen-file) | 9,767 job IDs seeded |
| `state/boards2_seen.json` | bootstrap tracking | 6,166 board IDs |
| `state/boards2_cursor.json` | batch cursor | 0 (fully cycled in bootstrap) |
| `state/boards2_dead.json` | dead boards | 0 (clean CSV going in) |
| `.github/workflows/boards2.yml` | workflow | batch_size=2000, cron fallback `43 */3 * * *`, concurrency=`job-watcher-boards2` |

All five env vars are disjoint — `STATE_PATH`, `BOARDS_CURSOR_PATH`, `BOARDS_SEEN_PATH`, `BOARDS_DEAD_PATH`, `BOARDS_DEAD_DETAILS_PATH` — so boards2 cannot collide with the live 1,200 pipeline.

**LIVE as of 2026-07-01T16:39Z.** First `workflow_dispatch` run confirmed success: processed 2,000 GH boards (cursor 0→2000), fetched 57,985 jobs, 3,524 loc_ok, 4 new/emailed, 19s. `seen_boards2.json` grew 9,767→9,771; `boards2_cursor.json`=2000; `seen_boards.json` (live 1,200) untouched. 30-min cadence confirmed. All three cron-job.org jobs share PAT expiring **2026-08-31** — **exposed in screenshot 2026-07-01, rotate soon**.

### Open questions (deferred)
- Job-text eligibility filter across ALL pipelines: drop roles requiring security clearance / "US citizen or PR required" / ITAR (ineligible on OPT); optionally flag "no sponsorship" (H-1B needed later). High value, situation-specific.

## Workday boards3 — sized, NOT built (2026-07-01)

### Sizing findings (`workday_us_verified.csv` → `data/boards/workday_verified_live.csv`)

| | Count | Notes |
|---|---|---|
| Source rows | 4,770 | `workday_us_verified.csv` |
| Junk/dupes removed | 2 | 1 junk slug, 1 internal dupe |
| Already in live 1200 | 77 | excluded, not net-new |
| Net-new probed | 4,768 | — |
| **Alive (has openings)** | **4,497 (94.3%)** | in output CSV |
| Alive (no current openings) | 154 (3.2%) | in output CSV — can get jobs later |
| Dead (4xx/WAF) | 59+16 | 16 are WAF-blocked (403), not rate-limited |
| **Total live CSV** | **4,651 rows** | `data/boards/workday_verified_live.csv` |

**Cost sample** — 200 boards, full `fetch_workday_jobs()` with `max_positions=500`:
| Metric | avg | p90 | p95 |
|---|---|---|---|
| api_calls / board | 21.5 | 26 | 26 |
| response_time / board | 40.5s | 107.7s | — |
| jobs / board | 404 | 500 | — |

p90 api_calls = 26 (= 25 POSTs + 1 GET) means the majority of large boards hit the 500-job cap. p90 wall-time = 107.7s/board (sequential per thread). Zero 429s observed at concurrency=20.

**Rate-limit signal: NONE.** 16 boards returned 403 — those are per-tenant WAF blocks, not throughput limits. Workday CXS has no observed rate-limiting at reasonable concurrency.

### Sizing math & recommendation

| | Value |
|---|---|
| Effective budget / run | 720s (15 min × 80%) |
| WD concurrency in watcher | 4 threads (WD_SEM=4) |
| Boards / run @ p90 latency | 26 (conservative cap) |
| Boards / run @ avg latency | 71 |
| **Recommended batch_size** | **50** (floor-capped for safety) |
| Runs to full cycle (4,497 alive) | 90 |
| Cycle time @ 30 min cadence | **45h** |

**45h cycle is too slow for a single shard.** Recommendation: **two shards** — `boards3a.yml` + `boards3b.yml`, ~2,325 boards each, staggered 15 min apart. Each shard cycles in ~22.5h, acceptable for a supplementary Workday lane (new Workday jobs are generally posted less frequently than GH/Lever).

### Design notes for when boards3 is built
- CSV is drop-in ready (`company_name, platform, board_url, country_focus, notes` columns, same as live)
- All 5 state env vars must be unique per shard (same pattern as boards2)
- `batch_size=50` with `timeout=15` keeps each run well within 15-min Actions limit
- Stagger the two cron-job.org jobs by 15 min to avoid simultaneous Workday load
- No overlap with boards2 (GH/Lever only) — 0 shared boards
- Consider Workday `--boards-batch-size 50` and revisit after measuring first live run

## Recent changes

- **2026-07-07** — `classify_title` now excludes numbered/roman-numeral levels above I ("Software Engineer II", "Data Scientist 2", "Data Engineer III"), returning `"senior"` (same excluded-but-tracked bucket as `SENIORITY_EXCLUDE_TOKENS`, not emailed). New `LEVEL_SUFFIX_REGEX` matches `(engineer|developer|analyst|scientist|specialist|programmer|architect)\s*(ii|iii|iv|v|2|3|4|5)`, case-insensitive, with the same `\bintern(ship)?\b` exception as the token loop. "Level I"/"1" titles (e.g. "Software Engineer I", the PlayStation SWE1 precedent) are left unblocked — this was the known gap flagged 2026-07-07 alongside the `SENIORITY_EXCLUDE_TOKENS` fix below and deferred pending sign-off; now fixed. Verified: `Software Engineer II`/`Machine Learning Engineer II`/`Data Scientist 2`/`Data Engineer III` → `senior`; `Software Engineer I`/`Data Analyst New Grad` → unaffected; `Software Engineer II Intern` → not blocked by this rule (falls through to normal classification).
- **2026-07-07** — `SENIORITY_MAYBE_TOKENS` renamed to `SENIORITY_EXCLUDE_TOKENS`; `classify_title` now returns a new `"senior"` classification (excluded from `title_matches()`/the email, but counted separately) instead of downgrading these titles to `"maybe"` (which still got emailed). Root cause: a Gmail application-pattern audit (2026-07-07, see `Job_Application_Pattern_Tracker.xlsx` in the Resume Build-up project) found every application to a senior/mismatched-level title (Google SWE II/III, Riot "Senior Data Scientist", Vanguard rotational ADP) got auto-rejected in 0-7 days regardless of resume quality, while correctly-leveled entry/new-grad titles that were rejected took 26-71 days (i.e. a human actually looked) — so emailing "maybe" for senior titles was pure noise. `run_boards_sweep()`'s per-platform funnel and `main()`'s per-source funnel both gained a `senior_excluded` count; `_fmt_run_summary()` prints `sr_excl=N` when nonzero. Verified against 16 real titles pulled from actual Gmail rejections.
- **2026-07-06** — Per-pipeline email subject prefixes added. `SUBJECT_PREFIX` env var in boards2.yml sets `[Boards2 Alerts]`; boards1 keeps `[Boards Alerts]`; main keeps `[Job Alerts]`. Gmail searches: `subject:[Job Alerts]` (main), `subject:[Boards Alerts]` (boards1), `subject:[Boards2 Alerts]` (boards2/GH+Lever shard).
- **2026-07-06 — FALSE ALARM: boards2 was never broken.** Earlier diagnostic (reading run_log.json) concluded boards2 ran only ~34 times over 5 days (3.4h cadence). **This was wrong.** `gh run list` confirms boards2 ran **273 times Jul 1–6** with a **perfect 30-min median gap and zero gaps > 60 min** (242 workflow_dispatch + 31 schedule, all success). The "34 runs" figure was a run_log.json artifact: run_log is a bounded 1000-entry JSON array rewritten in full by every pipeline. With main firing 6× more often than boards2, main's concurrent writes win the merge conflict race almost every time — run_log currently shows **0 boards2 entries** despite 273 actual runs. boards2_cursor.json advancing (0→2000→4000→6000) is the reliable signal that boards2 is sweeping correctly. **Do not re-investigate boards2 cadence based on run_log.json counts alone — use `gh run list --workflow=boards2.yml` instead.**

- **2026-07-01** — Workday boards3 sizing complete. Probed 4,768 net-new WD boards: 4,651 alive (97.5%), 77 already in live 1200. Cost sample (200 boards): avg 21.5 API calls/board, p90 40.5s/board wall time. Recommendation: batch_size=50, two shards (boards3a+b ~2,325 each), 30-min cadence → ~22.5h cycle per shard. Live CSV: `data/boards/workday_verified_live.csv` (4,651 rows). Report: `data/boards/workday_sizing_report.txt`. No pipeline built yet.
- **2026-07-01** — `classify_title` widened for data-engineering family (additive only, commit `595355dc`). Added to `STRONG_INCLUDE_PHRASES`: `"dataops"`, `"data ops"`, `"data operations engineer"`, `"data architect"`, `"data quality engineer"`. Added `has_dqe` carve-out in the hard-exclude loop so "Data Quality Engineer" is not blocked by the existing `"quality engineer"` hard-exclude (parallel to SDET exception). No existing terms removed or narrowed. Previously missed: DataOps Engineer, Data Operations Engineer (both dropped by "ops"/"operations" soft-exclude with no STRONG override); Data Architect (no weak/strong match); Data Quality Engineer (hard-excluded). All now pass. QA/DevOps exclusions unaffected.

- **2026-07-01** — boards2 LIVE. First workflow_dispatch at 16:39Z: success, 2,000 GH boards, 4 new jobs emailed, cursor→2000, boards2 state files updated, live-1200 state untouched. 30-min cadence confirmed. PAT shared by all 3 cron-job.org jobs expires 2026-08-31 — **exposed in screenshot 2026-07-01, rotate soon**. System now sweeps ~7,366 total boards across 3 pipelines.
- **2026-07-01** — boards2 pipeline built and seeded. `boards2.yml` created (batch_size=2000, cron fallback `43 */3 * * *`, concurrency=`job-watcher-boards2`). Bootstrap run seeded `seen_boards2.json` with 9,767 job IDs across 6,166 boards (181s). All state paths disjoint. Awaiting cron-job.org trigger (manual browser step).
- **2026-07-01** — Liveness probe complete. Probed 6,407 net-new GH+Lever boards in 73s; 6,166 alive (96.2%), 241 dead (all clean 404s). Output: `data/boards/greenhouse_lever_verified_live.csv`. Shard pipeline is the only remaining step.
- **2026-07-01** — Inventory unblocked. Root cause of zero-overlap false alarm: GH hostname mismatch (`boards.greenhouse.io` vs `job-boards.greenhouse.io`). Canonical dedup key: `urlparse(url).path.split('/')[1].lower()`. Net-new: GH 4,659 + Lever 1,806 = 6,465 (all genuinely new, 0 overlap with live 1,200). GH/Lever shard marked ready to build.
- **2026-06-02** — Expansion plan designed + budget measured. Multi-pipeline shard architecture decided; GH/Lever first move agreed. Verified-list inventory started (URL-format mismatch blocked net-new count — resume next session). See "Future roadmap" section above.
- **2026-06-02** — External triggering verified in production. `gh run list` confirms `event=workflow_dispatch` runs at 20:40 and 20:50 UTC (exactly 10 min apart, all success); boards dispatch also confirmed. Multi-hour latency fully resolved.
- **2026-06-02** — `ci: switch to external dispatch trigger — downgrade schedule to sparse fallback`. cron-job.org now drives both workflows (watcher 10 min, boards 30 min) via `workflow_dispatch` API (HTTP 204 verified). GitHub `schedule:` downgraded to `13 */3 * * *` (sparse fallback). PAT expires 2026-08-31.
- **2026-06-02** — Cadence audit: measured 10 watcher + 9 boards gaps post-Jun-1 cron change. Watcher median 268 min (target 10 min), boards median 273 min (target 30 min) — both worse than pre-change baseline. GitHub cron deprioritization confirmed; must move off Actions cron entirely.
- **2026-06-02** — `feat: add per-run funnel observability to state/run_log.json` (`d0d51894`). Both modes now record `{ts, mode, per_source: {src: {fetched, title_ok, loc_ok, new, emailed, error/errors}}, duration_s, cursor}` to `state/run_log.json` (bounded 1,000 records, picked up by existing `git add state/*.json`). Also prints a one-line summary to Actions log each run.
- **2026-06-01** — `fix: paginate Goldman Sachs, IBM, Oracle — fix Oracle requisitionList extraction` (`804f627b`). Oracle was broken since day one; now fixed. All three sources paginate fully.
- **2026-06-01** — `ci: improve cron cadence — watcher 10min offset, boards 30min offset` (`f7a5c236`). Moved off congested `:00/:15/:30/:45` slots.
- **2026-06-01** — `config: add .gitleaks.toml` (`1e06172d`). Suppresses `state/*.json` false positives while keeping default secret detectors active.
- **2026-06-01** — Full architecture audit: created `docs/ARCHITECTURE.md`; corrected dead-board count (16 active, 921 orphaned); confirmed Actions throttling as primary latency risk; identified GS/IBM/Oracle pagination gaps (now fixed).
- **2026-06-01** — Added `CLAUDE.md` and `docs/STATE.md` for persistent project memory. Repo made **public** (unlimited Actions minutes).
