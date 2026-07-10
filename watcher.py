import json
import os
import tempfile
import argparse
import ssl
import smtplib
import csv
import re
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse
from email.mime.text import MIMEText
from datetime import datetime, timezone
from typing import Any, Dict, List, Set, Tuple, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Path to this script's directory (for resolving relative files)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# -----------------------------
# Workday URL parsing helpers (CXS)
# -----------------------------
LOCALE_RE = re.compile(r"^[a-z]{2}[-_][a-zA-Z]{2}$")  # en-US, en-us, en_US, fr-CA, etc.

def _canon_locale(seg: str) -> str:
    """Canonicalize locale segments like en-us / en_US -> en-US."""
    s = (seg or "").strip()
    if not s:
        return ""
    s = s.replace("_", "-")
    parts = s.split("-")
    if len(parts) != 2:
        return s
    return f"{parts[0].lower()}-{parts[1].upper()}"


def _parse_workday_board(board_url: str) -> Tuple[str, str, str]:
    u = urlparse(board_url)
    host = u.netloc
    tenant = host.split(".")[0]
    origin = f"{u.scheme}://{host}"

    segs = [s for s in (u.path or "").split("/") if s]
    if not segs:
        raise ValueError(f"Workday board_url has no path: {board_url}")

    # If /en-US/<site>, drop the locale segment
    if len(segs) >= 2 and LOCALE_RE.match(segs[0]):
        site = segs[1]
    else:
        site = segs[0]

    return origin, tenant, site


def _workday_cxs_endpoints(board_url: str) -> Tuple[str, str]:
    origin, tenant, site = _parse_workday_board(board_url)
    approot = f"{origin}/wday/cxs/{tenant}/{site}/approot"
    jobs = f"{origin}/wday/cxs/{tenant}/{site}/jobs"
    return approot, jobs


def _is_workday_app_error(text: str) -> bool:
    return "<wml:Application_Error" in (text or "")


# -----------------------------
# State file paths (overrideable via env vars)
# -----------------------------
STATE_PATH = os.getenv("STATE_PATH", "state/seen.json")
BOARDS_CURSOR_PATH = os.getenv("BOARDS_CURSOR_PATH", "state/boards_cursor.json")
BOARDS_SEEN_PATH = os.getenv("BOARDS_SEEN_PATH", "state/boards_seen.json")
BOARDS_DEAD_PATH = os.getenv("BOARDS_DEAD_PATH", "state/boards_dead.json")
BOARDS_DEAD_DETAILS_PATH = os.getenv("BOARDS_DEAD_DETAILS_PATH", "state/boards_dead_details.json")
RUN_LOG_PATH = os.getenv("RUN_LOG_PATH", "state/run_log.json")
RUN_LOG_MAX = 1000


# -----------------------------
# Default boards CSV resolution
# -----------------------------
def resolve_default_boards_csv() -> str:
    candidates = [
        (os.getenv("BOARDS_CSV") or "").strip(),
        "data/boards/JOB_BOARDS_PURE_WORKING_SUPPORTED_round2.csv",
        "data/boards/JOB_BOARDS_PURE_WORKING_round2.csv",
        "data/boards/JOB_BOARDS_OK_PRODUCTION_MINUS_DEAD_round2.csv",
        "data/boards/JOB_BOARDS_OK_PRODUCTION.csv",
    ]

    def _try_resolve(raw: str) -> Optional[str]:
        if not raw:
            return None
        p = os.path.expanduser(raw)

        # If absolute, just check it.
        if os.path.isabs(p):
            ap = os.path.abspath(p)
            return ap if os.path.exists(ap) else None

        # If relative, try CWD-relative first, then repo/script-dir relative.
        ap_cwd = os.path.abspath(p)
        if os.path.exists(ap_cwd):
            return ap_cwd

        ap_script = os.path.abspath(os.path.join(SCRIPT_DIR, p))
        if os.path.exists(ap_script):
            return ap_script

        return None

    for raw in candidates:
        resolved = _try_resolve(raw)
        if resolved:
            return resolved

    # Final fallback
    return os.path.abspath(os.path.join(SCRIPT_DIR, "data/boards/JOB_BOARDS_OK_PRODUCTION.csv"))


# -----------------------------
# Boards supported platforms
# -----------------------------
BOARDS_SUPPORTED_PLATFORMS = ("greenhouse", "lever", "smartrecruiters", "workday", "ashby")
SMARTRECRUITERS_API_BASE = "https://api.smartrecruiters.com/v1/companies"

# -----------------------------
# Email env vars
# -----------------------------
EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_APP_PASSWORD = os.getenv("EMAIL_APP_PASSWORD")
ALERT_TO_EMAIL = os.getenv("ALERT_TO_EMAIL")


# -----------------------------
# Title filtering
# -----------------------------
STRONG_INCLUDE_PHRASES = [
    "software engineer",
    "software developer",
    "software development engineer",
    "sde",
    "backend engineer",
    "backend developer",
    "full stack",
    "fullstack",
    "platform engineer",
    "application developer",
    "applications engineer",
    "product developer",
    "product development engineer",
    "machine learning engineer",
    "ml engineer",
    "ai engineer",
    "applied scientist",
    "research engineer",
    "data scientist",
    "data engineer",
    "analytics engineer",
    "dataops",
    "data ops",
    "data operations engineer",
    "data architect",
    "data quality engineer",
    "data analyst",
    "analytics analyst",
    "product analyst",
    "sdet",
    "software development engineer in test",
]

WEAK_INCLUDE_PHRASES = [
    "developer",
    "software",
    "engineer",
    "analytics",
]

# Excluded from yes/maybe (and therefore from email) when found in a title that otherwise
# passed the strong/weak content match. Gmail audit (2026-07-07) showed every application to
# a title carrying one of these tokens got auto-rejected in 0-7 days regardless of resume
# quality (Google SWE II/III, Riot "Senior Data Scientist", Vanguard rotational ADP), while
# correctly-leveled titles that were rejected took 26-71 days (i.e. a human actually looked).
# classify_title() returns "senior" for these (not "no") so callers can still count/log them
# separately in run_log.json for audit without them ever reaching title_matches()/the inbox.
SENIORITY_EXCLUDE_TOKENS = [
    "senior",
    "sr",
    "staff",
    "principal",
    "lead",
    "architect",
    "distinguished",
    "fellow",
    "director",
]

# Numbered/roman-numeral levels (e.g. "Software Engineer II", "Data Scientist 2")
# above I/1. "I"/"1" is left unblocked since it's the standard entry-level tag
# (e.g. the PlayStation "SWE I" role that was correctly entry-leveled). Same
# Gmail-audit finding as SENIORITY_EXCLUDE_TOKENS above (Google SWE II/III both
# auto-rejected within hours) — known gap called out 2026-07-07, fixed here.
LEVEL_SUFFIX_REGEX = re.compile(
    r"\b(?:engineer|developer|analyst|scientist|specialist|programmer|architect)\s*(?:ii|iii|iv|v|2|3|4|5)\b",
    re.IGNORECASE,
)

HARD_EXCLUDE_PHRASES = [
    "quality assurance",
    "qa ",
    " qa",
    "tester",
    "test engineer",
    "quality engineer",
    "validation engineer",
    "site reliability",
    "sre",
    "reliability engineer",
    "reporting",
    "product manager",
    "program manager",
    "project manager",
    "scrum master",
    "business analyst",
    "sales",
    "marketing",
    "recruiter",
    "talent acquisition",
    "customer support",
    "technical support",
    "support engineer",
]

HARD_EXCLUDE_REGEXES = [
    r"\bintern\b",
    r"\binternship\b",
    r"\bco[- ]?op\b",
    r"\bcoop\b",
    r"\bapprentice\b",
]

SOFT_EXCLUDE_PHRASES = [
    "devops",
    "operations",
    "ops",
    "automation",
]


def _norm_title(title: str) -> str:
    t = (title or "").strip().lower()
    t = re.sub(r"\s+", " ", t)
    return t


def classify_title(title: str) -> str:
    t = _norm_title(title)
    if not t:
        return "no"

    has_sdet = ("sdet" in t) or ("software development engineer in test" in t)
    has_dqe = "data quality engineer" in t

    for pat in HARD_EXCLUDE_REGEXES:
        if re.search(pat, t):
            return "no"

    for bad in HARD_EXCLUDE_PHRASES:
        if bad in t:
            if has_sdet and bad in {
                "quality assurance", "qa ", " qa", "tester", "test engineer", "quality engineer", "validation engineer"
            }:
                break
            if has_dqe and bad == "quality engineer":
                continue  # "data quality engineer" is DE, not QA — skip this phrase only
            return "no"

    has_soft_excl = any(bad in t for bad in SOFT_EXCLUDE_PHRASES)

    strong = any(p in t for p in STRONG_INCLUDE_PHRASES)
    weak = any(p in t for p in WEAK_INCLUDE_PHRASES)

    if not (strong or weak):
        return "no"

    if has_soft_excl:
        return "maybe" if strong else "no"

    for tok in SENIORITY_EXCLUDE_TOKENS:
        if re.search(rf"\b{re.escape(tok)}\b", t):
            if not re.search(r"\bintern(ship)?\b", t):
                return "senior"

    if LEVEL_SUFFIX_REGEX.search(t) and not re.search(r"\bintern(ship)?\b", t):
        return "senior"

    if not strong:
        return "maybe"

    return "yes"


def title_matches(title: str) -> bool:
    return classify_title(title) in ("yes", "maybe")


# -----------------------------
# Polling caps
# -----------------------------
MAX_EIGHTFOLD_JOBS_PER_RUN = int(os.getenv("MAX_EIGHTFOLD_JOBS_PER_RUN", "300"))
MAX_AMZ_JOBS_PER_RUN = int(os.getenv("MAX_AMZ_JOBS_PER_RUN", "300"))
MAX_ORACLE_JOBS_PER_RUN = int(os.getenv("MAX_ORACLE_JOBS_PER_RUN", "200"))
MAX_IBM_JOBS_PER_RUN = int(os.getenv("MAX_IBM_JOBS_PER_RUN", "200"))
MAX_GS_JOBS_PER_RUN = int(os.getenv("MAX_GS_JOBS_PER_RUN", "200"))
AMZ_RESULT_LIMIT = int(os.getenv("AMZ_RESULT_LIMIT", "50"))


# -----------------------------
# Requests session pooling + retry (SPEED/ROBUSTNESS)
# -----------------------------
DEFAULT_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "30"))

# Concurrency knobs (boards)
BOARDS_WORKERS = int(os.getenv("BOARDS_WORKERS", "12"))
GH_MAX_INFLIGHT = int(os.getenv("GH_MAX_INFLIGHT", "8"))
LEVER_MAX_INFLIGHT = int(os.getenv("LEVER_MAX_INFLIGHT", "8"))
SR_MAX_INFLIGHT = int(os.getenv("SR_MAX_INFLIGHT", "6"))
WD_MAX_INFLIGHT = int(os.getenv("WD_MAX_INFLIGHT", "4"))
ASHBY_MAX_INFLIGHT = int(os.getenv("ASHBY_MAX_INFLIGHT", "6"))

_PLATFORM_SEMAPHORES: Dict[str, threading.Semaphore] = {
    "greenhouse": threading.Semaphore(GH_MAX_INFLIGHT),
    "lever": threading.Semaphore(LEVER_MAX_INFLIGHT),
    "smartrecruiters": threading.Semaphore(SR_MAX_INFLIGHT),
    "workday": threading.Semaphore(WD_MAX_INFLIGHT),
    "ashby": threading.Semaphore(ASHBY_MAX_INFLIGHT),
}

_thread_local = threading.local()


def _make_session() -> requests.Session:
    s = requests.Session()

    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=0.4,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
        respect_retry_after_header=True,
    )

    adapter = HTTPAdapter(pool_connections=100, pool_maxsize=100, max_retries=retry)

    s.mount("http://", adapter)
    s.mount("https://", adapter)

    # Small headers; keep UA stable
    s.headers.update({"user-agent": "Mozilla/5.0", "accept": "application/json,*/*"})
    return s


def _get_session(bucket: str = "default") -> requests.Session:
    # one session per thread per bucket (platform)
    sessions = getattr(_thread_local, "sessions", None)
    if sessions is None:
        sessions = {}
        _thread_local.sessions = sessions

    if bucket not in sessions:
        sessions[bucket] = _make_session()
    return sessions[bucket]


# -----------------------------
# Source configuration (main mode)
# -----------------------------
EIGHTFOLD_SOURCES: Dict[str, Dict[str, Any]] = {
    "microsoft": {
        "company": "Microsoft",
        "endpoint": "https://apply.careers.microsoft.com/api/pcsx/search",
        "base_url": "https://apply.careers.microsoft.com",
        "default_search_url": "https://apply.careers.microsoft.com/careers",
        "params": {
            "domain": "microsoft.com",
            "query": "",
            "location": "United States, Multiple Locations, Multiple Locations",
            "start": 0,
            "sort_by": "timestamp",
            "filter_include_remote": 1,
            "filter_seniority": ["Entry", "Mid-Level"],
        },
    },
    "nvidia": {
        "company": "NVIDIA",
        "endpoint": "https://nvidia.eightfold.ai/api/pcsx/search",
        "base_url": "https://nvidia.eightfold.ai",
        "default_search_url": "https://nvidia.eightfold.ai/careers",
        "params": {
            "domain": "nvidia.com",
            "query": "",
            "location": "united states",
            "start": 0,
            "sort_by": "timestamp",
            "filter_include_remote": 1,
            "filter_job_category": "engineering",
            "filter_job_type": "regular employee",
            "filter_time_type": "full time",
            "filter_hiring_title": [
                "Software Engineer",
                "Machine Learning Engineer",
                "Artificial Intelligence",
                "machine learning",
                "software development engineer",
                "data",
            ],
        },
    },
}

AMZ_ENDPOINT = "https://www.amazon.jobs/en/search.json"
AMZ_PARAMS = {
    "category[]": ["machine-learning-science", "software-development"],
    "schedule_type_id[]": ["Full-Time"],
    "normalized_country_code[]": ["USA"],
    "radius": "100000km",
    "offset": 0,
    "result_limit": 10,
    "sort": "recent",
    "latitude": 38.89036,
    "longitude": -77.03196,
    "loc_query": "united states",
    "base_query": "",
}

GS_ENDPOINT = "https://api-higher.gs.com/gateway/api/v1/graphql"
GS_HEADERS_MIN = {
    "accept": "application/json",
    "content-type": "application/json",
    "origin": "https://higher.gs.com",
    "referer": "https://higher.gs.com/",
    "user-agent": "Mozilla/5.0",
}
GS_PAYLOAD: Dict[str, Any] = {
    "operationName": "GetRoles",
    "variables": {
        "searchQueryInput": {
            "page": {"pageSize": 20, "pageNumber": 0},
            "sort": {"sortStrategy": "POSTED_DATE", "sortOrder": "DESC"},
            "filters": [
                {
                    "filterCategoryType": "EXPERIENCE_LEVEL",
                    "filters": [
                        {"filter": "Support", "subFilters": []},
                        {"filter": "Seasonal", "subFilters": []},
                        {"filter": "Associate", "subFilters": []},
                    ],
                },
                {
                    "filterCategoryType": "JOB_FUNCTION",
                    "filters": [{"filter": "Software Engineering", "subFilters": []}],
                },
                {
                    "filterCategoryType": "LOCATION",
                    "filters": [
                        {
                            "filter": "United States",
                            "subFilters": [
                                {"filter": "District of Columbia", "subFilters": [{"filter": "Washington", "subFilters": []}]},
                                {"filter": "Virginia", "subFilters": [{"filter": "McLean", "subFilters": []}]},
                                {"filter": "New York", "subFilters": [{"filter": "New York", "subFilters": []}]},
                                {"filter": "Massachusetts", "subFilters": [{"filter": "Boston", "subFilters": []}]},
                                {"filter": "California", "subFilters": [{"filter": "San Francisco", "subFilters": []}]},
                            ],
                        }
                    ],
                },
            ],
            "experiences": ["EARLY_CAREER", "PROFESSIONAL"],
            "searchTerm": "",
        }
    },
    "query": "query GetRoles($searchQueryInput: RoleSearchQueryInput!) {\n  roleSearch(searchQueryInput: $searchQueryInput) {\n    totalCount\n    items {\n      roleId\n      corporateTitle\n      jobTitle\n      jobFunction\n      locations {\n        primary\n        state\n        country\n        city\n        __typename\n      }\n      status\n      division\n      skills\n      jobType {\n        code\n        description\n        __typename\n      }\n      externalSource {\n        sourceId\n        __typename\n      }\n      __typename\n    }\n    __typename\n  }\n}",
}

IBM_ENDPOINT = "https://www-api.ibm.com/search/api/v2"
IBM_HEADERS_MIN = {
    "accept": "application/json",
    "content-type": "application/json",
    "origin": "https://www.ibm.com",
    "referer": "https://www.ibm.com/",
    "user-agent": "Mozilla/5.0",
}
IBM_PAYLOAD: Dict[str, Any] = {
    "appId": "careers",
    "scopes": ["careers2"],
    "query": {"bool": {"must": []}},
    "post_filter": {
        "bool": {
            "must": [
                {
                    "bool": {
                        "should": [
                            {"term": {"field_keyword_08": "Software Engineering"}},
                            {"term": {"field_keyword_08": "Data & Analytics"}},
                        ]
                    }
                },
                {"term": {"field_keyword_18": "Entry Level"}},
                {"term": {"field_keyword_05": "United States"}},
            ]
        }
    },
    "aggs": {},
    "size": 30,
    "sort": [{"dcdate": "desc"}, {"_score": "desc"}],
    "lang": "zz",
    "localeSelector": {},
    "sm": {"query": "", "lang": "zz"},
    "_source": [
        "_id",
        "title",
        "url",
        "description",
        "language",
        "entitled",
        "field_keyword_17",
        "field_keyword_08",
        "field_keyword_18",
        "field_keyword_19",
    ],
}

_ORACLE_URL_PREFIX = (
    "https://eeho.fa.us2.oraclecloud.com/hcmRestApi/resources/latest/"
    "recruitingCEJobRequisitions"
    "?onlyData=true"
    "&expand=requisitionList.workLocation,requisitionList.otherWorkLocations,"
    "requisitionList.secondaryLocations,flexFieldsFacet.values,"
    "requisitionList.requisitionFlexFields"
    "&finder=findReqs;siteNumber=CX_45001,"
    "facetsList=LOCATIONS%3BWORK_LOCATIONS%3BWORKPLACE_TYPES%3BTITLES%3BCATEGORIES%3B"
    "ORGANIZATIONS%3BPOSTING_DATES%3BFLEX_FIELDS,"
)
_ORACLE_URL_SUFFIX = (
    "lastSelectedFacet=AttributeChar13,"
    "locationId=300000000149325,"
    "selectedCategoriesFacet=300000001559315%3B300000001917356,"
    "selectedFlexFieldsFacets=%22AttributeChar6%7C0%20to%202%2B%20years%22,"
    "selectedLocationsFacet=300000000149325,"
    "selectedPostingDatesFacet=7,sortBy=POSTING_DATES_DESC"
)
ORACLE_PAGE_SIZE = 50
ORACLE_HEADERS_MIN = {
    "accept": "application/json",
    "content-type": "application/json",
    "origin": "https://careers.oracle.com",
    "referer": "https://careers.oracle.com/",
    "user-agent": "Mozilla/5.0",
}

SUPPORTED_SOURCES = ["microsoft", "amazon", "nvidia", "goldman_sachs", "ibm", "oracle"]


# -----------------------------
# State helpers
# -----------------------------
def _atomic_write_json(path: str, payload: Dict[str, Any], indent: int = 2) -> None:
    dirpath = os.path.dirname(path) or "."
    os.makedirs(dirpath, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=dirpath)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass


def _append_run_log(record: Dict[str, Any]) -> None:
    existing: List[Any] = []
    if os.path.exists(RUN_LOG_PATH):
        try:
            with open(RUN_LOG_PATH, "r", encoding="utf-8") as f:
                existing = json.load(f)
            if not isinstance(existing, list):
                existing = []
        except Exception:
            existing = []
    existing.append(record)
    if len(existing) > RUN_LOG_MAX:
        existing = existing[-RUN_LOG_MAX:]
    _atomic_write_json(RUN_LOG_PATH, existing)


def _fmt_run_summary(mode: str, ts: str, per_source: Dict[str, Any], cursor: Optional[int], dur: float) -> str:
    parts = []
    for src, s in per_source.items():
        if s.get("error"):
            parts.append(f"{src} ERROR")
        else:
            seg = f"{src} f={s.get('fetched', 0)} t={s.get('title_ok', 0)} loc={s.get('loc_ok', 0)} new={s.get('new', 0)}"
            if s.get("senior_excluded"):
                seg += f" sr_excl={s['senior_excluded']}"
            if s.get("emailed"):
                seg += f" em={s['emailed']}"
            if s.get("errors"):
                seg += f" err={s['errors']}"
            parts.append(seg)
    cur_str = f" cur={cursor}" if cursor is not None else ""
    return f"{mode} {ts} | {' | '.join(parts)} | dur={dur}s{cur_str}"


def load_seen_ids(path: str) -> Set[str]:
    if not os.path.exists(path):
        return set()
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return set(data.get("seen_ids", []))


def save_seen_ids(path: str, seen_ids: Set[str]) -> None:
    payload = {"updated_utc": datetime.now(timezone.utc).isoformat(), "seen_ids": sorted(seen_ids)}
    _atomic_write_json(path, payload, indent=2)


# -----------------------------
# Boards cursor helpers
# -----------------------------
def load_boards_cursor(path: str) -> int:
    if not os.path.exists(path):
        return 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        cur = int(data.get("cursor", 0))
        return max(cur, 0)
    except Exception:
        return 0


def save_boards_cursor(path: str, cursor: int) -> None:
    payload = {"updated_utc": datetime.now(timezone.utc).isoformat(), "cursor": int(max(cursor, 0))}
    _atomic_write_json(path, payload, indent=2)


def load_boards_seen(path: str) -> Set[str]:
    if not os.path.exists(path):
        return set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data.get("boards_seen", []))
    except Exception:
        return set()


def save_boards_seen(path: str, boards_seen: Set[str]) -> None:
    payload = {"updated_utc": datetime.now(timezone.utc).isoformat(), "boards_seen": sorted(boards_seen)}
    _atomic_write_json(path, payload, indent=2)


def load_boards_dead(path: str) -> Set[str]:
    if not os.path.exists(path):
        return set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data.get("boards_dead", []))
    except Exception:
        return set()


def save_boards_dead(path: str, boards_dead: Set[str]) -> None:
    payload = {"updated_utc": datetime.now(timezone.utc).isoformat(), "boards_dead": sorted(boards_dead)}
    _atomic_write_json(path, payload, indent=2)


def load_dead_details(path: str) -> Dict[str, Dict[str, Any]]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        details = data.get("dead_details") or {}
        return details if isinstance(details, dict) else {}
    except Exception:
        return {}


def save_dead_details(path: str, dead_details: Dict[str, Dict[str, Any]]) -> None:
    payload = {"updated_utc": datetime.now(timezone.utc).isoformat(), "dead_details": dead_details}
    _atomic_write_json(path, payload, indent=2)


def upsert_dead_detail(
    dead_details: Dict[str, Dict[str, Any]],
    *,
    board_id: str,
    platform: str,
    company: str,
    board_url: str,
    status: Optional[int],
    error: str,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    rec = dead_details.get(board_id) or {}
    if not rec.get("first_seen_utc"):
        rec["first_seen_utc"] = now
    rec["last_seen_utc"] = now
    rec["platform"] = platform
    rec["company"] = company
    rec["board_url"] = board_url
    if status is not None:
        rec["last_status"] = int(status)
    rec["last_error"] = str(error)
    dead_details[board_id] = rec


def export_dead_boards_csv(dead_details: Dict[str, Dict[str, Any]], out_path: str) -> None:
    if not out_path:
        return
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    fieldnames = [
        "board_id",
        "platform",
        "company",
        "board_url",
        "last_status",
        "last_error",
        "first_seen_utc",
        "last_seen_utc",
    ]
    rows: List[Dict[str, Any]] = []
    for board_id, rec in sorted(dead_details.items(), key=lambda x: x[0]):
        row = {"board_id": board_id}
        for k in fieldnames[1:]:
            row[k] = rec.get(k, "")
        rows.append(row)

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


# -----------------------------
# Boards CSV loader
# -----------------------------
def load_boards_csv(path: str) -> List[Dict[str, str]]:
    raw = (path or "").strip()
    p = os.path.expanduser(raw)

    # If a relative path is provided, allow running from any CWD by also resolving relative to this script.
    if not os.path.isabs(p):
        p_cwd = os.path.abspath(p)
        p_script = os.path.abspath(os.path.join(SCRIPT_DIR, p))
        if os.path.exists(p_cwd):
            p = p_cwd
        elif os.path.exists(p_script):
            p = p_script

    if not os.path.exists(p):
        raise FileNotFoundError(f"Boards CSV not found: {path}")

    rows: List[Dict[str, str]] = []
    with open(p, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            company = (r.get("company_name") or r.get("company") or "").strip()
            platform = (r.get("platform") or "").strip().lower()
            url = (r.get("board_url") or r.get("url") or "").strip()
            ok_val = (r.get("ok") or "").strip().lower()

            if ok_val and ok_val not in ("true", "1", "yes"):
                continue
            if not company or not platform or not url:
                continue
            rows.append({"company": company, "platform": platform, "board_url": url})

    seen: Set[Tuple[str, str]] = set()
    deduped: List[Dict[str, str]] = []
    for r in rows:
        k = (r["platform"], r["board_url"].rstrip("/"))
        if k in seen:
            continue
        seen.add(k)
        r["board_url"] = r["board_url"].rstrip("/")
        deduped.append(r)
    return deduped


def make_location(parts: List[str]) -> str:
    clean = [p.strip() for p in parts if p and str(p).strip()]
    return ", ".join(clean) if clean else "Unknown Location"


# -----------------------------
# Location helpers (Boards mode)
# -----------------------------
US_STATE_ABBRS = {
    "al","ak","az","ar","ca","co","ct","de","fl","ga","hi","id","il","in","ia","ks","ky","la",
    "me","md","ma","mi","mn","ms","mo","mt","ne","nv","nh","nj","nm","ny","nc","nd","oh","ok",
    "or","pa","ri","sc","sd","tn","tx","ut","vt","va","wa","wv","wi","wy","dc",
}

# ISO 3166-1 alpha-2 country codes that are NOT the US.
# Used to reject false-positive state-abbreviation matches in international location strings
# like "Budapest, OR, hu" where "or" is an Hungarian region code, not Oregon.
NON_US_COUNTRY_CODES = {
    "af","al","dz","ad","ao","ag","ar","am","au","at","az","bs","bh","bd","bb","by","be",
    "bz","bj","bt","bo","ba","bw","br","bn","bg","bf","bi","kh","cm","ca","cv","cf","td",
    "cl","cn","co","km","cg","cd","cr","ci","hr","cu","cy","cz","dk","dj","dm","do","ec",
    "eg","sv","gq","er","ee","et","fj","fi","fr","ga","gm","ge","de","gh","gr","gd","gt",
    "gn","gw","gy","ht","hn","hu","is","in","id","ir","iq","ie","il","it","jm","jp","jo",
    "kz","ke","ki","kp","kr","kw","kg","la","lv","lb","ls","lr","ly","li","lt","lu","mk",
    "mg","mw","my","mv","ml","mt","mh","mr","mu","mx","fm","md","mc","mn","me","ma","mz",
    "mm","na","nr","np","nl","nz","ni","ne","ng","no","om","pk","pw","pa","pg","py","pe",
    "ph","pl","pt","qa","ro","ru","rw","kn","lc","vc","ws","sm","st","sa","sn","rs","sc",
    "sl","sg","sk","si","sb","so","za","ss","es","lk","sd","sr","sz","se","ch","sy","tw",
    "tj","tz","th","tl","tg","to","tt","tn","tr","tm","tv","ug","ua","ae","gb","uy","uz",
    "vu","ve","vn","ye","zm","zw",
}

# Full country names that appear in job location strings and are NOT the US.
# Guards against cases like "Bogota, DC, Colombia" where the last part is a full
# country name (> 2 chars) that bypasses the 2-char ISO code check above.
NON_US_COUNTRY_NAMES = {
    "afghanistan","albania","algeria","andorra","angola","argentina","armenia","australia",
    "austria","azerbaijan","bahrain","bangladesh","belarus","belgium","belize","benin",
    "bhutan","bolivia","bosnia","botswana","brazil","brunei","bulgaria","burkina faso",
    "burundi","cambodia","cameroon","canada","cape verde","chad","chile","china","colombia",
    "comoros","congo","costa rica","croatia","cuba","cyprus","czechia","czech republic",
    "denmark","djibouti","dominican republic","ecuador","egypt","el salvador","eritrea",
    "estonia","ethiopia","fiji","finland","france","gabon","gambia","georgia","germany",
    "ghana","greece","guatemala","guinea","guyana","haiti","honduras","hungary","iceland",
    "india","indonesia","iran","iraq","ireland","israel","italy","jamaica","japan","jordan",
    "kazakhstan","kenya","kuwait","kyrgyzstan","laos","latvia","lebanon","lesotho","liberia",
    "libya","liechtenstein","lithuania","luxembourg","madagascar","malawi","malaysia",
    "maldives","mali","malta","mauritania","mauritius","mexico","moldova","monaco",
    "mongolia","montenegro","morocco","mozambique","myanmar","namibia","nepal",
    "netherlands","new zealand","nicaragua","niger","nigeria","north korea","norway","oman",
    "pakistan","panama","papua new guinea","paraguay","peru","philippines","poland",
    "portugal","qatar","romania","russia","rwanda","saudi arabia","senegal","serbia",
    "sierra leone","singapore","slovakia","slovenia","somalia","south africa","south korea",
    "south sudan","spain","sri lanka","sudan","suriname","sweden","switzerland","syria",
    "taiwan","tajikistan","tanzania","thailand","togo","trinidad and tobago","tunisia",
    "turkey","turkmenistan","uganda","ukraine","united arab emirates","united kingdom",
    "uruguay","uzbekistan","venezuela","vietnam","yemen","zambia","zimbabwe",
}


def is_us_location(location: str) -> bool:
    loc = (location or "").strip().lower()
    if not loc or loc == "unknown location":
        return False
    if "united states" in loc or "u.s." in loc:
        return True
    if re.search(r"\busa\b", loc):
        return True
    if re.search(r"\bus\b", loc):
        return True
    if "remote" in loc and (re.search(r"\bus\b", loc) or "united states" in loc or re.search(r"\busa\b", loc)):
        return True
    if "washington, dc" in loc or "district of columbia" in loc:
        return True
    # Reject if any comma-separated part is a known non-US country indicator.
    # Two separate guards:
    #   2-char ISO code ("hu", "co"…): only safe when 3+ parts — "ca" alone is ambiguous
    #     (California vs Canada), but "City, ST, ca" clearly flags Canada.
    #   Full country name ("Colombia", "Germany"…): unambiguous at any part count.
    parts = [p.strip() for p in loc.split(",")]
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-1] in NON_US_COUNTRY_CODES:
        return False
    if any(p in NON_US_COUNTRY_NAMES for p in parts):
        return False
    m = re.search(r",\s*([a-z]{2})(\b|[^a-z])", loc)
    if m and m.group(1) in US_STATE_ABBRS:
        return True
    return False


# -----------------------------
# Eightfold (Microsoft + NVIDIA)
# -----------------------------
def eightfold_key_from_pos(source: str, pos: Dict[str, Any]) -> str:
    job_id = str(pos.get("id", ""))
    if job_id:
        return f"{source}:{job_id}"
    url = pos.get("applyUrl") or pos.get("positionUrl") or ""
    return f"{source}:url:{url}"


def fetch_eightfold_positions(
    source: str,
    seen_keys: Optional[Set[str]] = None,
    max_positions: int = MAX_EIGHTFOLD_JOBS_PER_RUN,
) -> List[Dict[str, Any]]:
    cfg = EIGHTFOLD_SOURCES[source]
    endpoint = cfg["endpoint"]
    params0 = cfg["params"]

    sess = _get_session("main")
    headers = {"accept": "application/json", "user-agent": "Mozilla/5.0"}

    all_positions: List[Dict[str, Any]] = []
    start = 0
    safety_cap = 5000

    while True:
        params = dict(params0)
        params["start"] = start

        r = sess.get(endpoint, params=params, headers=headers, timeout=DEFAULT_TIMEOUT)
        r.raise_for_status()
        data = r.json()

        positions = (data.get("data", {}) or {}).get("positions", []) or []
        if not positions:
            break

        all_positions.extend(positions)

        if len(all_positions) >= max_positions:
            all_positions = all_positions[:max_positions]
            break

        if seen_keys is not None and start > 0:
            page_keys = {eightfold_key_from_pos(source, p) for p in positions}
            if page_keys and page_keys.issubset(seen_keys):
                break

        start += len(positions)
        if start >= safety_cap:
            break

    return all_positions


def normalize_eightfold_position(source: str, pos: Dict[str, Any]) -> Dict[str, str]:
    cfg = EIGHTFOLD_SOURCES[source]
    key = eightfold_key_from_pos(source, pos)
    title = pos.get("name") or pos.get("title") or "Unknown Title"

    if isinstance(pos.get("standardizedLocations"), list) and pos["standardizedLocations"]:
        loc = pos["standardizedLocations"][0]
    elif isinstance(pos.get("locations"), list) and pos["locations"]:
        loc = pos["locations"][0]
    else:
        loc = "Unknown Location"

    posted_ts = pos.get("postedTs")
    posted_str = ""
    if isinstance(posted_ts, (int, float)):
        posted_str = datetime.fromtimestamp(posted_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    url = pos.get("positionUrl") or pos.get("applyUrl") or cfg["default_search_url"]
    if isinstance(url, str) and url.startswith("/"):
        url = cfg["base_url"] + url

    return {
        "key": str(key),
        "company": cfg["company"],
        "title": str(title),
        "location": str(loc),
        "posted": str(posted_str),
        "url": str(url),
    }


# -----------------------------
# Amazon
# -----------------------------
def amazon_key_from_job(job: Dict[str, Any]) -> str:
    job_id = (
        job.get("id")
        or job.get("job_id")
        or job.get("jobId")
        or job.get("id_icims")
        or job.get("icims_id")
        or job.get("requisition_id")
        or ""
    )
    job_id = str(job_id)
    if job_id:
        return f"amazon:{job_id}"
    url = job.get("url") or job.get("job_path") or ""
    return f"amazon:url:{url}"


def fetch_amazon_positions(
    seen_keys: Optional[Set[str]] = None,
    max_positions: int = MAX_AMZ_JOBS_PER_RUN,
) -> List[Dict[str, Any]]:
    sess = _get_session("main")
    headers = {"accept": "application/json", "user-agent": "Mozilla/5.0"}

    all_jobs: List[Dict[str, Any]] = []
    offset = 0
    limit = AMZ_RESULT_LIMIT
    safety_cap = 5000

    while True:
        params = dict(AMZ_PARAMS)
        params["offset"] = offset
        params["result_limit"] = limit

        r = sess.get(AMZ_ENDPOINT, params=params, headers=headers, timeout=DEFAULT_TIMEOUT)
        r.raise_for_status()
        data = r.json()

        jobs = data.get("jobs") or data.get("results") or data.get("job_results") or []
        if not jobs:
            break

        all_jobs.extend(jobs)

        if len(all_jobs) >= max_positions:
            all_jobs = all_jobs[:max_positions]
            break

        if seen_keys is not None and offset > 0:
            page_keys = {amazon_key_from_job(j) for j in jobs}
            if page_keys and page_keys.issubset(seen_keys):
                break

        offset += len(jobs)
        if offset >= safety_cap:
            break

    return all_jobs


def normalize_amazon_job(job: Dict[str, Any]) -> Dict[str, str]:
    key = amazon_key_from_job(job)
    title = job.get("title") or job.get("job_title") or job.get("name") or "Unknown Title"
    loc = job.get("location") or job.get("normalized_location") or job.get("city") or job.get("primary_location") or "Unknown Location"
    posted_str = job.get("posted_date") or job.get("postedDate") or job.get("posted") or ""

    url = job.get("url") or job.get("job_path") or job.get("jobPath") or ""
    if isinstance(url, str) and url.startswith("/"):
        url = "https://www.amazon.jobs" + url
    if not url:
        url = "https://www.amazon.jobs/en/search"

    return {"key": str(key), "company": "Amazon", "title": str(title), "location": str(loc), "posted": str(posted_str), "url": str(url)}


# -----------------------------
# Goldman Sachs (GraphQL)
# -----------------------------
def gs_key_from_item(item: Dict[str, Any]) -> str:
    role_id = str(item.get("roleId", ""))
    if role_id:
        return f"goldman_sachs:{role_id}"
    return f"goldman_sachs:url:{(item.get('externalSource', {}) or {}).get('sourceId', '')}"


def fetch_goldman_sachs(
    seen_keys: Optional[Set[str]] = None,
    max_positions: int = MAX_GS_JOBS_PER_RUN,
) -> List[Dict[str, Any]]:
    sess = _get_session("main")
    all_items: List[Dict[str, Any]] = []
    page_number = 0
    page_size = GS_PAYLOAD["variables"]["searchQueryInput"]["page"]["pageSize"]
    while True:
        payload = json.loads(json.dumps(GS_PAYLOAD))
        payload["variables"]["searchQueryInput"]["page"]["pageNumber"] = page_number
        r = sess.post(GS_ENDPOINT, headers=GS_HEADERS_MIN, json=payload, timeout=DEFAULT_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        page_items = (((data.get("data", {}) or {}).get("roleSearch", {}) or {}).get("items", []) or [])
        if not page_items:
            break
        all_items.extend(page_items)
        if max_positions and len(all_items) >= max_positions:
            break
        if seen_keys and all(gs_key_from_item(i) in seen_keys for i in page_items):
            break
        if len(page_items) < page_size:
            break
        page_number += 1
    if max_positions and len(all_items) > max_positions:
        all_items = all_items[:max_positions]
    return all_items


def normalize_goldman_item(item: Dict[str, Any]) -> Dict[str, str]:
    key = gs_key_from_item(item)
    title = item.get("jobTitle") or item.get("corporateTitle") or "Unknown Title"
    locs = item.get("locations") or []
    loc = "Unknown Location"
    if isinstance(locs, list) and locs:
        first = locs[0] if isinstance(locs[0], dict) else {}
        loc = first.get("primary") or make_location([first.get("city"), first.get("state"), first.get("country")])
    role_id = str(item.get("roleId", ""))
    url = f"https://higher.gs.com/roles/{role_id}" if role_id else "https://higher.gs.com/results"
    return {"key": str(key), "company": "Goldman Sachs", "title": str(title), "location": str(loc), "posted": "", "url": str(url)}


# -----------------------------
# IBM (POST JSON)
# -----------------------------
def ibm_key_from_hit(hit: Dict[str, Any]) -> str:
    _id = hit.get("_id") or hit.get("id") or ""
    _id = str(_id)
    if _id:
        return f"ibm:{_id}"
    url = (hit.get("_source") or {}).get("url") or hit.get("url") or ""
    return f"ibm:url:{url}"


def fetch_ibm(
    seen_keys: Optional[Set[str]] = None,
    max_positions: int = MAX_IBM_JOBS_PER_RUN,
) -> List[Dict[str, Any]]:
    sess = _get_session("main")
    all_hits: List[Dict[str, Any]] = []
    page_size = IBM_PAYLOAD["size"]
    offset = 0
    while True:
        payload = dict(IBM_PAYLOAD)
        payload["from"] = offset
        r = sess.post(IBM_ENDPOINT, headers=IBM_HEADERS_MIN, json=payload, timeout=DEFAULT_TIMEOUT)

        # IBM endpoint occasionally rejects certain fields (e.g., `aggs`) with a 400.
        # If we see that, retry once with a minimal payload.
        if r.status_code == 400 and "aggs" in (r.text or ""):
            payload.pop("aggs", None)
            r = sess.post(IBM_ENDPOINT, headers=IBM_HEADERS_MIN, json=payload, timeout=DEFAULT_TIMEOUT)

        if r.status_code >= 400:
            print(f"[DEBUG] IBM HTTP {r.status_code}: {r.text[:500]}")
        r.raise_for_status()
        data = r.json()

        results = data.get("results")
        if isinstance(results, list):
            page_hits = results
        else:
            page_hits = ((data.get("hits", {}) or {}).get("hits", []) or [])

        if not page_hits:
            break
        all_hits.extend(page_hits)
        if max_positions and len(all_hits) >= max_positions:
            break
        if seen_keys and all(ibm_key_from_hit(h) in seen_keys for h in page_hits):
            break
        if len(page_hits) < page_size:
            break
        offset += page_size
    if max_positions and len(all_hits) > max_positions:
        all_hits = all_hits[:max_positions]
    return all_hits


def normalize_ibm_hit(hit: Dict[str, Any]) -> Dict[str, str]:
    key = ibm_key_from_hit(hit)
    src = hit.get("_source") if isinstance(hit.get("_source"), dict) else hit
    title = src.get("title") or "Unknown Title"
    url = src.get("url") or ""
    if url and isinstance(url, str) and url.startswith("/"):
        url = "https://www.ibm.com" + url
    if not url:
        url = "https://www.ibm.com/careers/search"
    posted_str = src.get("dcdate") or ""
    loc = src.get("field_keyword_17")
    if isinstance(loc, list) and loc:
        loc_str = str(loc[0])
    elif isinstance(loc, str) and loc:
        loc_str = loc
    else:
        loc_str = "United States"
    return {"key": str(key), "company": "IBM", "title": str(title), "location": str(loc_str), "posted": str(posted_str), "url": str(url)}


# -----------------------------
# Oracle (GET)
# -----------------------------
def oracle_key_from_req(req: Dict[str, Any]) -> str:
    rid = req.get("requisitionId") or req.get("RequisitionId") or req.get("id") or req.get("Id") or ""
    rid = str(rid)
    if rid:
        return f"oracle:{rid}"
    url = req.get("ExternalApplyLink") or req.get("applyUrl") or req.get("externalApplyUrl") or ""
    return f"oracle:url:{url}"


def fetch_oracle(
    seen_keys: Optional[Set[str]] = None,
    max_positions: int = MAX_ORACLE_JOBS_PER_RUN,
) -> List[Dict[str, Any]]:
    sess = _get_session("main")
    all_reqs: List[Dict[str, Any]] = []
    offset = 0
    while True:
        url = _ORACLE_URL_PREFIX + f"limit={ORACLE_PAGE_SIZE},offset={offset}," + _ORACLE_URL_SUFFIX
        r = sess.get(url, headers=ORACLE_HEADERS_MIN, timeout=DEFAULT_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        items = data.get("items") or []
        page_reqs = items[0].get("requisitionList", []) if items else []
        if not page_reqs:
            break
        all_reqs.extend(page_reqs)
        if max_positions and len(all_reqs) >= max_positions:
            break
        if seen_keys and all(oracle_key_from_req(req) in seen_keys for req in page_reqs):
            break
        if len(page_reqs) < ORACLE_PAGE_SIZE:
            break
        offset += ORACLE_PAGE_SIZE
    if max_positions and len(all_reqs) > max_positions:
        all_reqs = all_reqs[:max_positions]
    return all_reqs


def normalize_oracle_req(req: Dict[str, Any]) -> Dict[str, str]:
    key = oracle_key_from_req(req)
    title = req.get("Title") or req.get("title") or req.get("requisitionTitle") or req.get("requisitionName") or "Unknown Title"
    loc_parts: List[str] = []
    wl = req.get("workLocation")
    if isinstance(wl, dict):
        loc_parts.extend([wl.get("city"), wl.get("state"), wl.get("country")])
    loc = make_location(loc_parts) if loc_parts else "United States"
    posted_str = req.get("PostedDate") or req.get("postedDate") or req.get("postingDate") or ""
    url = req.get("ExternalApplyLink") or req.get("externalApplyUrl") or req.get("applyUrl") or ""
    if not url:
        url = "https://careers.oracle.com/jobs/#en/sites/jobsearch"
    return {"key": str(key), "company": "Oracle", "title": str(title), "location": str(loc), "posted": str(posted_str), "url": str(url)}


# -----------------------------
# Workday (Boards mode)
# -----------------------------
WORKDAY_HEADERS_MIN = {"accept": "application/json", "content-type": "application/json", "user-agent": "Mozilla/5.0"}


def workday_tenant_from_host(host: str) -> str:
    host = (host or "").strip().lower()
    if not host:
        return ""
    return host.split(".")[0]




def workday_site_from_board_url(board_url: str) -> str:
    u = urlparse((board_url or "").strip())
    parts = [p for p in (u.path or "").split("/") if p]
    if not parts:
        return ""
    if parts and LOCALE_RE.match(parts[0]):
        parts = parts[1:]
    return parts[0] if parts else ""

# Best-effort locale segment for external Workday links.
def workday_locale_from_board_url(board_url: str) -> str:
    """Best-effort locale segment for external Workday links.

    Many Workday external links require the locale prefix (commonly `en-US`).
    If the board URL already includes a locale (e.g., `/en-US/<site>`), reuse it.
    Otherwise default to `en-US`.
    """
    u = urlparse((board_url or "").strip())
    parts = [p for p in (u.path or "").split("/") if p]
    if parts and LOCALE_RE.match(parts[0]):
        canon = _canon_locale(parts[0])
        return canon or "en-US"
    return "en-US"


def workday_normalize_external_job_url(board_url: str, external: str) -> str:
    """Normalize Workday external job URLs/paths for external users.

    Fixes shapes like:
      - https://<host>/job/...    (missing /en-US/<site>/)
      - /job/...                 (missing /en-US/<site>/)
      - /<site>/job/...          (missing /en-US/)

    Returns a fully-qualified URL if possible.
    """
    ext = (external or "").strip()
    if not ext:
        return ""

    locale = workday_locale_from_board_url(board_url)
    site = workday_site_from_board_url(board_url)

    bu = urlparse((board_url or "").strip())
    base_host = (bu.netloc or "").strip()

    if ext.startswith("http"):
        eu = urlparse(ext)
        host = (eu.netloc or "").strip() or base_host
        path = eu.path or ""
    else:
        host = base_host
        path = ext

    if not host or not path.startswith("/"):
        return ext

    segs = [s for s in path.split("/") if s]

    # Already /<locale>/<site>/...
    if len(segs) >= 2 and LOCALE_RE.match(segs[0]) and site and segs[1] == site:
        canon0 = _canon_locale(segs[0])
        if canon0 and canon0 != segs[0]:
            segs[0] = canon0
            new_path = "/" + "/".join(segs)
        else:
            new_path = path

    # /<locale>/job/... (locale present, missing site)
    elif len(segs) >= 2 and LOCALE_RE.match(segs[0]) and site and segs[1] in {"job", "jobs"}:
        canon0 = _canon_locale(segs[0]) or segs[0]
        rest = "/".join(segs[1:])
        new_path = f"/{canon0}/{site}/{rest}"

    # /<site>/... (missing locale)
    elif len(segs) >= 1 and site and segs[0] == site:
        new_path = f"/{locale}{path}"

    # /job/... or /jobs/... (missing locale + site)
    elif len(segs) >= 1 and segs[0] in {"job", "jobs"} and site:
        new_path = f"/{locale}/{site}{path}"

    else:
        new_path = path

    return f"https://{host}{new_path}"

def workday_board_id(board_url: str) -> str:
    u = urlparse((board_url or "").strip())
    tenant = workday_tenant_from_host(u.netloc)
    site = workday_site_from_board_url(board_url)
    if tenant and site:
        return f"workday:{tenant}:{site}"
    if tenant:
        return f"workday:{tenant}:"
    return "workday:"


# Matches the requisition/job number embedded in a Workday externalPath, e.g.:
#   /job/Software-Engineer_R1234567        → R1234567
#   /en-US/site/job/Engineer_R1234567-1   → R1234567  (variant suffix stripped)
#   /job/Engineer_12345                    → 12345
#   /job/Engineer_R-1234567               → R-1234567
_WD_REQ_RE = re.compile(r"[/_]([A-Za-z]{0,3}-?\d{3,})(?:-\d+)?(?:[/?#_]|$)")


def workday_key_from_post(tenant: str, site: str, post: Dict[str, Any], url: str) -> str:
    pid = str(post.get("jobPostingId") or post.get("id") or "")
    if pid:
        return f"workday:{tenant}:{site}:{pid}"
    # jobPostingId absent — try to pull a stable req ID from the externalPath rather than
    # using the full normalized URL, which can vary across calls for the same job.
    ext_path = str(post.get("externalPath") or post.get("externalUrl") or "")
    if ext_path:
        m = _WD_REQ_RE.search(ext_path)
        if m:
            return f"workday:{tenant}:{site}:req:{m.group(1)}"
    return f"workday:{tenant}:{site}:url:{url}"


def fetch_workday_jobs(board_url: str, max_positions: int = 500, timeout: int = DEFAULT_TIMEOUT) -> List[Dict[str, Any]]:
    approot_url, jobs_url = _workday_cxs_endpoints(board_url)

    all_posts: List[Dict[str, Any]] = []
    offset = 0
    limit = 20
    safety_cap = 5000

    sess = _get_session("workday")
    headers = dict(WORKDAY_HEADERS_MIN)

    boot = sess.get(approot_url, timeout=timeout)
    boot.raise_for_status()

    while True:
        payload = {"limit": limit, "offset": offset, "searchText": "", "appliedFacets": {}}
        resp = sess.post(jobs_url, headers=headers, json=payload, timeout=timeout)
        resp.raise_for_status()

        if _is_workday_app_error(resp.text):
            err_snip = (resp.text or "")[:200].replace("\n", " ")
            raise RuntimeError(f"Workday application error: {err_snip}")

        ct = (resp.headers.get("content-type") or "").lower()
        if "json" not in ct:
            body_snip = (resp.text or "")[:200].replace("\n", " ")
            raise RuntimeError(f"Workday non-JSON response (ct={ct}): {body_snip}")

        data = resp.json() if resp.content else {}
        posts = data.get("jobPostings") or data.get("items") or []
        if not isinstance(posts, list) or not posts:
            break

        all_posts.extend(posts)

        if max_positions and len(all_posts) >= max_positions:
            all_posts = all_posts[:max_positions]
            break

        offset += len(posts)
        if offset >= safety_cap:
            break

    return all_posts


def normalize_workday_post(company_name: str, board_url: str, post: Dict[str, Any]) -> Dict[str, str]:
    u = urlparse((board_url or "").strip())
    host = (u.netloc or "").strip()
    tenant = workday_tenant_from_host(host)
    site = workday_site_from_board_url(board_url)

    title = post.get("title") or post.get("jobTitle") or "Unknown Title"
    loc = post.get("locationsText") or post.get("location") or "Unknown Location"
    if isinstance(loc, list):
        loc = make_location([str(x) for x in loc])
    else:
        loc = str(loc)

    posted = post.get("postedOn") or post.get("postedDate") or post.get("timePosted") or ""

    # Workday CXS often returns external paths like `/job/...` which need `/en-US/<site>/...`.
    # Use one normalizer for all shapes (absolute URL or path).
    ext = post.get("externalPath") or post.get("externalUrl") or ""
    url = ""

    if isinstance(ext, str) and ext:
        if ext.startswith("http") or ext.startswith("/"):
            url = workday_normalize_external_job_url(board_url, ext)
        else:
            # rare: path without leading slash
            url = workday_normalize_external_job_url(board_url, "/" + ext)

    if not url:
        url = board_url

    key = workday_key_from_post(tenant, site, post, url)

    return {"key": str(key), "company": str(company_name), "title": str(title), "location": str(loc), "posted": str(posted), "url": str(url)}


# -----------------------------
# SmartRecruiters (Boards mode)
# -----------------------------
def smartrecruiters_company_from_board_url(board_url: str) -> str:
    u = urlparse((board_url or "").strip())
    parts = [p for p in (u.path or "").split("/") if p]
    if len(parts) >= 1:
        return parts[0].strip()
    return ""


def smartrecruiters_board_id(board_url: str) -> str:
    slug = smartrecruiters_company_from_board_url(board_url).lower()
    return f"smartrecruiters:{slug}" if slug else "smartrecruiters:"


def smartrecruiters_key_from_post(company_slug: str, post: Dict[str, Any]) -> str:
    pid = str(post.get("id") or post.get("ref") or "")
    if pid:
        return f"smartrecruiters:{company_slug}:{pid}"
    url = post.get("referrer") or post.get("applyUrl") or post.get("url") or ""
    return f"smartrecruiters:{company_slug}:url:{url}"


def fetch_smartrecruiters_jobs(board_url: str, max_positions: int = 500, timeout: int = DEFAULT_TIMEOUT) -> List[Dict[str, Any]]:
    company = smartrecruiters_company_from_board_url(board_url)
    if not company:
        return []

    sess = _get_session("smartrecruiters")
    headers = {"accept": "application/json", "user-agent": "Mozilla/5.0"}

    all_posts: List[Dict[str, Any]] = []
    offset = 0
    limit = 100
    safety_cap = 5000

    while True:
        url = f"{SMARTRECRUITERS_API_BASE}/{company}/postings"
        params = {"offset": offset, "limit": limit}
        r = sess.get(url, params=params, headers=headers, timeout=timeout)
        r.raise_for_status()

        data = r.json() if r.content else {}
        posts = data.get("content") or data.get("postings") or []
        if not isinstance(posts, list) or not posts:
            break

        all_posts.extend(posts)

        if max_positions and len(all_posts) >= max_positions:
            all_posts = all_posts[:max_positions]
            break

        offset += len(posts)
        if offset >= safety_cap:
            break

    return all_posts


def normalize_smartrecruiters_post(company_name: str, board_url: str, post: Dict[str, Any]) -> Dict[str, str]:
    company_slug = smartrecruiters_company_from_board_url(board_url) or company_name.lower().replace(" ", "")
    key = smartrecruiters_key_from_post(company_slug, post)
    title = post.get("name") or post.get("jobTitle") or "Unknown Title"

    loc_obj = post.get("location") or {}
    if isinstance(loc_obj, dict):
        city = loc_obj.get("city")
        region = loc_obj.get("region") or loc_obj.get("state")
        country = loc_obj.get("country")
        loc_str = make_location([city, region, country])
    else:
        loc_str = str(loc_obj) if loc_obj else "Unknown Location"

    posted = post.get("releasedDate") or post.get("publicationDate") or post.get("createdOn") or ""
    url = post.get("referrer") or post.get("applyUrl") or post.get("url") or ""
    if not url:
        url = board_url

    return {"key": str(key), "company": str(company_name), "title": str(title), "location": str(loc_str), "posted": str(posted), "url": str(url)}


# -----------------------------
# Greenhouse + Lever (Boards mode)
# -----------------------------
def greenhouse_slug_from_board_url(board_url: str) -> str:
    u = urlparse(board_url or "")
    parts = [p for p in (u.path or "").split("/") if p]
    return parts[0] if parts else ""


def lever_slug_from_board_url(board_url: str) -> str:
    u = urlparse(board_url or "")
    parts = [p for p in (u.path or "").split("/") if p]
    return parts[0] if parts else ""


def gh_key(company_slug: str, job_id: str) -> str:
    return f"greenhouse:{company_slug}:{job_id}"


def lever_key(company_slug: str, job_id: str) -> str:
    return f"lever:{company_slug}:{job_id}"


def fetch_greenhouse_jobs(company_slug: str, timeout: int = DEFAULT_TIMEOUT) -> List[Dict[str, Any]]:
    sess = _get_session("greenhouse")
    url = f"https://boards-api.greenhouse.io/v1/boards/{company_slug}/jobs"
    r = sess.get(url, params={"content": "true"}, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    jobs = data.get("jobs") or []
    return jobs if isinstance(jobs, list) else []


def fetch_lever_jobs(company_slug: str, timeout: int = DEFAULT_TIMEOUT) -> List[Dict[str, Any]]:
    sess = _get_session("lever")
    url = f"https://jobs.lever.co/v0/postings/{company_slug}"
    r = sess.get(url, params={"mode": "json"}, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


def normalize_greenhouse_job(company_name: str, company_slug: str, job: Dict[str, Any]) -> Dict[str, str]:
    job_id = str(job.get("id") or "")
    key = gh_key(company_slug, job_id) if job_id else f"greenhouse:{company_slug}:url:{job.get('absolute_url','')}"
    title = job.get("title") or "Unknown Title"

    loc = "Unknown Location"
    loc_obj = job.get("location")
    if isinstance(loc_obj, dict) and loc_obj.get("name"):
        loc = str(loc_obj.get("name"))

    posted_str = job.get("updated_at") or job.get("created_at") or ""
    url = job.get("absolute_url") or job.get("url") or ""
    return {"key": str(key), "company": str(company_name), "title": str(title), "location": str(loc), "posted": str(posted_str), "url": str(url)}


def normalize_lever_job(company_name: str, company_slug: str, job: Dict[str, Any]) -> Dict[str, str]:
    job_id = str(job.get("id") or "")
    key = lever_key(company_slug, job_id) if job_id else f"lever:{company_slug}:url:{job.get('hostedUrl','')}"
    title = job.get("text") or job.get("title") or "Unknown Title"

    loc = "Unknown Location"
    categories = job.get("categories")
    if isinstance(categories, dict) and categories.get("location"):
        loc = str(categories.get("location"))

    posted_str = job.get("createdAt") or ""
    if isinstance(posted_str, (int, float)):
        posted_str = datetime.fromtimestamp(float(posted_str) / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    url = job.get("hostedUrl") or job.get("applyUrl") or ""
    return {"key": str(key), "company": str(company_name), "title": str(title), "location": str(loc), "posted": str(posted_str), "url": str(url)}


# -----------------------------
# Ashby (Boards mode)
# -----------------------------
ASHBY_API_URL = "https://jobs.ashbyhq.com/api/non-user-graphql"
ASHBY_JOBS_QUERY = (
    "query ApiJobBoardWithTeams($organizationHostedJobsPageName: String!) {"
    "  jobBoard: jobBoardWithTeams(organizationHostedJobsPageName: $organizationHostedJobsPageName) {"
    "    jobPostings { id title locationName workplaceType employmentType }"
    "  }"
    "}"
)


def ashby_slug_from_board_url(board_url: str) -> str:
    u = urlparse(board_url or "")
    parts = [p for p in (u.path or "").split("/") if p]
    return parts[0] if parts else ""


def ashby_key(company_slug: str, job_id: str) -> str:
    return f"ashby:{company_slug}:{job_id}"


def fetch_ashby_jobs(company_slug: str, timeout: int = DEFAULT_TIMEOUT) -> List[Dict[str, Any]]:
    sess = _get_session("ashby")
    r = sess.post(
        ASHBY_API_URL,
        headers={"content-type": "application/json"},
        json={
            "operationName": "ApiJobBoardWithTeams",
            "variables": {"organizationHostedJobsPageName": company_slug},
            "query": ASHBY_JOBS_QUERY,
        },
        timeout=timeout,
    )
    r.raise_for_status()
    data = r.json()
    board = (data.get("data") or {}).get("jobBoard")
    if board is None:
        # Slug not found — synthesize a 404 so dead-board tracking fires the same way as Greenhouse
        import requests as _req
        fake = _req.models.Response()
        fake.status_code = 404
        raise _req.HTTPError(response=fake)
    return board.get("jobPostings") or []


def normalize_ashby_job(company_name: str, company_slug: str, job: Dict[str, Any]) -> Dict[str, str]:
    job_id = str(job.get("id") or "")
    key = ashby_key(company_slug, job_id) if job_id else f"ashby:{company_slug}:url:"
    title = job.get("title") or "Unknown Title"
    loc = job.get("locationName") or "Unknown Location"
    url = (
        f"https://jobs.ashbyhq.com/{company_slug}/{job_id}"
        if job_id
        else f"https://jobs.ashbyhq.com/{company_slug}"
    )
    return {"key": str(key), "company": str(company_name), "title": str(title), "location": str(loc), "posted": "", "url": str(url)}


# -----------------------------
# Boards sweep (CONCURRENT + PERF)
# -----------------------------
def _board_id_for(platform: str, board_url: str) -> Tuple[str, str]:
    platform = (platform or "").strip().lower()

    if platform == "greenhouse":
        slug = greenhouse_slug_from_board_url(board_url)
        return f"greenhouse:{slug}", slug
    if platform == "lever":
        slug = lever_slug_from_board_url(board_url)
        return f"lever:{slug}", slug
    if platform == "smartrecruiters":
        slug = smartrecruiters_company_from_board_url(board_url)
        return smartrecruiters_board_id(board_url), slug
    if platform == "ashby":
        slug = ashby_slug_from_board_url(board_url)
        return f"ashby:{slug}", slug
    # workday
    return workday_board_id(board_url), ""


def _is_dead_http_status(status: Optional[int]) -> bool:
    # For boards: 404 is clearly dead. Some platforms may return 410 too.
    return status in (404, 410)


def _process_single_board(
    b: Dict[str, str],
    boards_seen: Set[str],
    dead_boards: Set[str],
    timeout: int,
    suppress_new_boards: bool = True,
) -> Tuple[
    str,                 # platform
    str,                 # board_id
    float,               # elapsed_s
    List[Dict[str, str]],# norm_jobs
    Set[str],            # bootstrap_keys
    bool,                # bootstrap_board
    Optional[int],       # dead_status
    Optional[str],       # dead_error
    Optional[str],       # err_line
]:
    company = b["company"]
    platform = b["platform"]
    board_url = b["board_url"]

    t0 = time.time()
    norm_jobs: List[Dict[str, str]] = []
    bootstrap_keys: Set[str] = set()
    bootstrap_board = False
    dead_status: Optional[int] = None
    dead_error: Optional[str] = None
    err_line: Optional[str] = None

    board_id, slug = _board_id_for(platform, board_url)

    if board_id in dead_boards:
        return platform, board_id, 0.0, [], set(), False, None, None, None

    sem = _PLATFORM_SEMAPHORES.get(platform)
    if sem is None:
        sem = threading.Semaphore(1)

    try:
        with sem:
            if platform == "greenhouse":
                jobs = fetch_greenhouse_jobs(slug, timeout=timeout)
                norm_jobs = [normalize_greenhouse_job(company, slug, j) for j in jobs]
            elif platform == "lever":
                jobs = fetch_lever_jobs(slug, timeout=timeout)
                norm_jobs = [normalize_lever_job(company, slug, j) for j in jobs]
            elif platform == "smartrecruiters":
                jobs = fetch_smartrecruiters_jobs(board_url, timeout=timeout)
                norm_jobs = [normalize_smartrecruiters_post(company, board_url, j) for j in jobs]
            elif platform == "ashby":
                jobs = fetch_ashby_jobs(slug, timeout=timeout)
                norm_jobs = [normalize_ashby_job(company, slug, j) for j in jobs]
            else:
                jobs = fetch_workday_jobs(board_url, timeout=timeout)
                norm_jobs = [normalize_workday_post(company, board_url, j) for j in jobs]

        # Per-board bootstrap to avoid huge first-time alerts.
        # IMPORTANT: when running --test-email, we WANT to emit jobs even for brand-new boards,
        # otherwise matched will be empty and test-email will fail.
        if suppress_new_boards and (board_id not in boards_seen):
            matched_on_board = [
                j for j in norm_jobs
                if title_matches(j.get("title", "")) and is_us_location(j.get("location", ""))
            ]
            bootstrap_keys = {j["key"] for j in matched_on_board if j.get("key")}
            bootstrap_board = True
            norm_jobs = []  # do not emit jobs from brand-new board

    except requests.HTTPError as e:
        status: Optional[int] = None
        try:
            status = int(e.response.status_code) if e.response is not None else None
        except Exception:
            status = None

        if _is_dead_http_status(status):
            dead_status = status
            if platform == "smartrecruiters":
                dead_error = "HTTP 404/410 (company/board not found)"
            elif platform == "workday":
                dead_error = "HTTP 404/410 (Workday API not found)"
            else:
                dead_error = "HTTP 404/410 (board not found)"
        else:
            err_line = f"{platform} {company}: HTTPError status={status}: {e}"

    except Exception as e:
        err_line = f"{platform} {company}: {type(e).__name__}: {e}"

    elapsed = time.time() - t0
    return platform, board_id, elapsed, norm_jobs, bootstrap_keys, bootstrap_board, dead_status, dead_error, err_line


def run_boards_sweep(
    seen: Set[str],
    boards_seen: Set[str],
    dead_boards: Set[str],
    dead_details: Dict[str, Dict[str, Any]],
    boards_csv: str,
    batch_size: int,
    timeout: int = DEFAULT_TIMEOUT,
    workers: int = BOARDS_WORKERS,
    suppress_new_boards: bool = True,
) -> Tuple[List[Dict[str, str]], Set[str], List[str], int, Set[str], Set[str], Dict[str, Dict[str, Any]]]:
    boards = load_boards_csv(boards_csv)
    boards = [b for b in boards if (b.get("platform") or "") in BOARDS_SUPPORTED_PLATFORMS]
    if not boards:
        return [], set(), ["No supported boards found (need greenhouse/lever/smartrecruiters/workday rows)."], 0, set(), set()

    cursor = load_boards_cursor(BOARDS_CURSOR_PATH)
    n = len(boards)

    start = cursor % n
    end = min(start + max(batch_size, 1), n)
    batch = boards[start:end]

    # PERF metrics
    perf_platform_counts: Dict[str, int] = {}
    perf_platform_time: Dict[str, float] = {}
    perf_boards_total = 0

    normalized: List[Dict[str, str]] = []
    errors: List[str] = []
    bootstrap_keys: Set[str] = set()
    bootstrap_boards: Set[str] = set()

    t_sweep0 = time.time()

    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as ex:
        futures = [
            ex.submit(_process_single_board, b, boards_seen, dead_boards, timeout, suppress_new_boards)
            for b in batch
        ]
        for fut in as_completed(futures):
            platform, board_id, elapsed, norm_jobs, bkeys, booted, dead_status, dead_err, err_line = fut.result()

            if elapsed > 0:
                perf_boards_total += 1
                perf_platform_counts[platform] = perf_platform_counts.get(platform, 0) + 1
                perf_platform_time[platform] = perf_platform_time.get(platform, 0.0) + float(elapsed)

            if dead_status is not None and dead_err is not None:
                dead_boards.add(board_id)
                # best-effort to find original row
                # (we do not have company/url here; include what we can)
                upsert_dead_detail(
                    dead_details,
                    board_id=board_id,
                    platform=platform,
                    company="",
                    board_url="",
                    status=dead_status,
                    error=dead_err,
                )
                errors.append(f"DEAD {platform} {board_id}: {dead_err}")
                continue

            if err_line:
                errors.append(err_line)

            if bkeys:
                bootstrap_keys |= bkeys
            if booted:
                bootstrap_boards.add(board_id)

            if norm_jobs:
                normalized.extend(norm_jobs)

    matched = [j for j in normalized if title_matches(j.get("title", "")) and is_us_location(j.get("location", ""))]
    latest_keys = {j["key"] for j in matched if j.get("key")}

    per_platform: Dict[str, Dict[str, Any]] = {}
    for _j in normalized:
        _plat = (_j.get("key") or "unknown").split(":")[0]
        _s = per_platform.setdefault(
            _plat, {"fetched": 0, "title_ok": 0, "loc_ok": 0, "new": 0, "emailed": 0, "senior_excluded": 0}
        )
        _s["fetched"] += 1
        _cls = classify_title(_j.get("title", ""))
        if _cls == "senior":
            _s["senior_excluded"] += 1
        if title_matches(_j.get("title", "")):
            _s["title_ok"] += 1
            if is_us_location(_j.get("location", "")):
                _s["loc_ok"] += 1

    new_cursor = end if end < n else 0

    sweep_elapsed = time.time() - t_sweep0
    # Print perf summary
    if perf_boards_total > 0:
        parts = []
        for p in sorted(perf_platform_counts.keys()):
            c = perf_platform_counts[p]
            tt = perf_platform_time.get(p, 0.0)
            avg = (tt / c) if c else 0.0
            parts.append(f"{p} c={c} avg={avg:.2f}s")
        print(f"[PERF] boards_batch={len(batch)} processed={perf_boards_total} elapsed={sweep_elapsed:.2f}s | " + " | ".join(parts))

    return matched, latest_keys, errors, new_cursor, bootstrap_keys, bootstrap_boards, per_platform


# -----------------------------
# Email
# -----------------------------
def send_email_digest(
    yes_jobs: List[Dict[str, str]],
    maybe_jobs: List[Dict[str, str]],
    subject_prefix: str = "[Job Alerts]",
) -> None:
    if not (EMAIL_USER and EMAIL_APP_PASSWORD and ALERT_TO_EMAIL):
        raise RuntimeError("Missing EMAIL_USER / EMAIL_APP_PASSWORD / ALERT_TO_EMAIL env vars.")

    all_jobs = (yes_jobs or []) + (maybe_jobs or [])
    companies = sorted({j.get("company", "") for j in all_jobs if j.get("company")})
    company_str = ", ".join(companies) if companies else "Jobs"

    subject = f"{subject_prefix} {len(yes_jobs)} yes + {len(maybe_jobs)} maybe ({company_str})"

    lines: List[str] = []
    lines.append(f"YES bucket: {len(yes_jobs)} job(s)\n")
    for j in yes_jobs:
        posted = f" | {j['posted']}" if j.get("posted") else ""
        lines.append(f"- [{j['company']}] {j['title']} | {j['location']}{posted}")
        lines.append(f"  {j['url']}")
        lines.append("")

    if maybe_jobs:
        lines.append("\nMAYBE bucket (review manually): " + str(len(maybe_jobs)) + " job(s)\n")
        for j in maybe_jobs:
            posted = f" | {j['posted']}" if j.get("posted") else ""
            lines.append(f"- [{j['company']}] {j['title']} | {j['location']}{posted}")
            lines.append(f"  {j['url']}")
            lines.append("")

    body = "\n".join(lines)

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = EMAIL_USER
    msg["To"] = ALERT_TO_EMAIL

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        app_pw = (EMAIL_APP_PASSWORD or "").replace(" ", "").replace("\xa0", "").strip()
        server.login(EMAIL_USER, app_pw)
        server.send_message(msg)


# -----------------------------
# Main orchestration (main mode)
# -----------------------------
def safe_call(label: str, fn):
    try:
        return fn(), None
    except Exception as e:
        return None, f"{label}: {type(e).__name__}: {e}"


def main(test_email: bool = False, no_email: bool = False, dry_run: bool = False) -> None:
    t0 = time.time()
    seen = load_seen_ids(STATE_PATH)

    normalized: List[Dict[str, str]] = []
    errors: List[str] = []
    src_norm: Dict[str, List[Dict[str, str]]] = {}
    src_errors: Dict[str, str] = {}

    ms_positions, err = safe_call("Microsoft fetch", lambda: fetch_eightfold_positions("microsoft", seen_keys=seen))
    if err:
        errors.append(err)
        src_errors["microsoft"] = err
    else:
        print(f"[DEBUG] Fetched {len(ms_positions)} positions from Microsoft endpoint.")
        _ms_norm = [normalize_eightfold_position("microsoft", p) for p in (ms_positions or [])]
        normalized.extend(_ms_norm)
        src_norm["microsoft"] = _ms_norm

    nv_positions, err = safe_call("NVIDIA fetch", lambda: fetch_eightfold_positions("nvidia", seen_keys=seen))
    if err:
        errors.append(err)
        src_errors["nvidia"] = err
    else:
        print(f"[DEBUG] Fetched {len(nv_positions)} positions from NVIDIA endpoint.")
        _nv_norm = [normalize_eightfold_position("nvidia", p) for p in (nv_positions or [])]
        normalized.extend(_nv_norm)
        src_norm["nvidia"] = _nv_norm

    amz_positions, err = safe_call("Amazon fetch", lambda: fetch_amazon_positions(seen_keys=seen))
    if err:
        errors.append(err)
        src_errors["amazon"] = err
    else:
        print(f"[DEBUG] Fetched {len(amz_positions)} jobs from Amazon endpoint.")
        _amz_norm = [normalize_amazon_job(j) for j in (amz_positions or [])]
        normalized.extend(_amz_norm)
        src_norm["amazon"] = _amz_norm

    gs_items, err = safe_call("Goldman Sachs fetch", lambda: fetch_goldman_sachs(seen_keys=seen))
    if err:
        errors.append(err)
        src_errors["goldman_sachs"] = err
    else:
        print(f"[DEBUG] Fetched {len(gs_items)} roles from Goldman Sachs endpoint.")
        _gs_norm = [normalize_goldman_item(i) for i in (gs_items or [])]
        normalized.extend(_gs_norm)
        src_norm["goldman_sachs"] = _gs_norm

    ibm_hits, err = safe_call("IBM fetch", lambda: fetch_ibm(seen_keys=seen))
    if err:
        errors.append(err)
        src_errors["ibm"] = err
    else:
        print(f"[DEBUG] Fetched {len(ibm_hits)} roles from IBM endpoint.")
        _ibm_norm = [normalize_ibm_hit(h) for h in (ibm_hits or [])]
        normalized.extend(_ibm_norm)
        src_norm["ibm"] = _ibm_norm

    oracle_reqs, err = safe_call("Oracle fetch", lambda: fetch_oracle(seen_keys=seen))
    if err:
        errors.append(err)
        src_errors["oracle"] = err
    else:
        print(f"[DEBUG] Fetched {len(oracle_reqs)} requisitions from Oracle endpoint.")
        _oracle_norm = [normalize_oracle_req(rq) for rq in (oracle_reqs or [])]
        normalized.extend(_oracle_norm)
        src_norm["oracle"] = _oracle_norm

    yes_matched = [j for j in normalized if classify_title(j.get("title", "")) == "yes"]
    maybe_matched = [j for j in normalized if classify_title(j.get("title", "")) == "maybe"]
    matched = yes_matched + maybe_matched

    _per_source: Dict[str, Dict[str, Any]] = {}
    for _src in SUPPORTED_SOURCES:
        _jobs = src_norm.get(_src, [])
        _t_ok = [j for j in _jobs if title_matches(j.get("title", ""))]
        _l_ok = [j for j in _t_ok if is_us_location(j.get("location", ""))]
        _sr_excl = [j for j in _jobs if classify_title(j.get("title", "")) == "senior"]
        _entry: Dict[str, Any] = {
            "fetched": len(_jobs), "title_ok": len(_t_ok), "loc_ok": len(_l_ok),
            "new": 0, "emailed": 0, "senior_excluded": len(_sr_excl),
        }
        if _src in src_errors:
            _entry["error"] = src_errors[_src]
        _per_source[_src] = _entry

    if test_email:
        sample_yes = yes_matched[:2]
        sample_maybe = maybe_matched[:1]
        if not (sample_yes or sample_maybe):
            raise RuntimeError("No matching jobs found to send in test email.")
        if no_email:
            print(f"[TEST] no-email enabled; would have sent {len(sample_yes) + len(sample_maybe)} job(s) to {ALERT_TO_EMAIL}.")
        else:
            send_email_digest(sample_yes, sample_maybe, subject_prefix="[TEST Job Alerts]")
            print(f"[TEST] Sent a test email with {len(sample_yes) + len(sample_maybe)} job(s) to {ALERT_TO_EMAIL}.")
        if errors:
            print("[WARN] Some sources failed:")
            for e in errors:
                print("  -", e)
        return

    latest_keys = {j["key"] for j in matched if j.get("key")}

    bootstrap_sources: Set[str] = set()
    for src in SUPPORTED_SOURCES:
        if not any(k.startswith(f"{src}:") for k in seen):
            bootstrap_sources.add(src)

    if bootstrap_sources:
        for src in bootstrap_sources:
            src_keys = {k for k in latest_keys if k.startswith(f"{src}:")}
            seen |= src_keys
        if not dry_run:
            save_seen_ids(STATE_PATH, seen)
        print(f"[BOOTSTRAP] Initialized sources: {', '.join(sorted(bootstrap_sources))}. No email for these sources this run.")

    if not os.path.exists(STATE_PATH):
        if dry_run:
            print(f"[BOOTSTRAP] (dry-run) Would save {len(latest_keys)} seen_ids. No email sent.")
            return
        save_seen_ids(STATE_PATH, latest_keys)
        print(f"[BOOTSTRAP] Saved {len(latest_keys)} seen_ids. No email sent.")
        _ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
        _append_run_log({"ts": _ts, "mode": "main", "per_source": _per_source, "duration_s": round(time.time() - t0, 1), "cursor": None})
        print(_fmt_run_summary("main", _ts, _per_source, None, round(time.time() - t0, 1)))
        return

    new_keys = latest_keys - seen
    for _src in SUPPORTED_SOURCES:
        _per_source[_src]["new"] = sum(1 for k in new_keys if k.startswith(_src + ":"))

    new_yes = [j for j in yes_matched if j.get("key") in new_keys]
    new_maybe = [j for j in maybe_matched if j.get("key") in new_keys]

    if new_yes or new_maybe:
        if no_email:
            print(f"[ALERT] no-email enabled; {len(new_yes)} yes + {len(new_maybe)} maybe new job(s) detected (not emailed).")
        else:
            send_email_digest(new_yes, new_maybe, subject_prefix="[Job Alerts]")
            print(f"[ALERT] Sent digest for {len(new_yes)} yes + {len(new_maybe)} maybe new job(s).")
            for _j in new_yes + new_maybe:
                _src = (_j.get("key") or "").split(":")[0]
                if _src in _per_source:
                    _per_source[_src]["emailed"] += 1
    else:
        print("[OK] No new jobs.")

    if not dry_run:
        seen |= latest_keys
        save_seen_ids(STATE_PATH, seen)

    if errors:
        print("[WARN] Some sources failed (watcher still ran):")
        for e in errors:
            print("  -", e)

    _ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    _dur = round(time.time() - t0, 1)
    if not dry_run:
        _append_run_log({"ts": _ts, "mode": "main", "per_source": _per_source, "duration_s": _dur, "cursor": None})
    print(_fmt_run_summary("main", _ts, _per_source, None, _dur))


# -----------------------------
# CLI
# -----------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Job watcher")
    parser.add_argument("--dry-run", action="store_true", help="Run without saving any state/cursor/dead-board files (safe for testing).")
    parser.add_argument("--no-email", action="store_true", help="Do everything except sending email (still updates state/cursor).")
    parser.add_argument("--test-email", action="store_true", help="Send a test email using the latest 1-3 matching jobs (does not change seen_ids).")
    parser.add_argument("--mode", default="main", choices=["main", "boards"], help="Run mode: main (existing adapters) or boards (ATS board sweep).")
    parser.add_argument(
        "--boards-csv",
        default=resolve_default_boards_csv(),
        help="Boards CSV path (must include company_name, platform, board_url). Default resolves via BOARDS_CSV env var or best-available CSV.",
    )
    parser.add_argument("--boards-batch-size", type=int, default=50, help="How many boards to process per boards run (default: 50).")
    parser.add_argument("--export-dead-csv", default="", help="Optional: write a CSV report of dead boards (404/410) discovered so far.")
    parser.add_argument("--boards-timeout", type=int, default=DEFAULT_TIMEOUT, help=f"HTTP timeout in seconds for boards adapters (default: {DEFAULT_TIMEOUT}).")
    parser.add_argument("--boards-run-until-wrap", action="store_true", help="In boards mode, keep running batches until cursor wraps to 0 (full sweep).")
    parser.add_argument("--boards-max-iterations", type=int, default=2000, help="Safety cap for --boards-run-until-wrap (default: 2000 iterations).")

    args = parser.parse_args()

    if args.mode == "boards":
        seen = load_seen_ids(STATE_PATH)
        boards_seen = load_boards_seen(BOARDS_SEEN_PATH)
        dead_boards = load_boards_dead(BOARDS_DEAD_PATH)
        dead_details = load_dead_details(BOARDS_DEAD_DETAILS_PATH)

        # CSV summary
        try:
            from collections import Counter
            _all_rows = load_boards_csv(args.boards_csv)
            _all_counts = Counter((r.get("platform") or "") for r in _all_rows)
            _supported_rows = [r for r in _all_rows if (r.get("platform") or "") in BOARDS_SUPPORTED_PLATFORMS]
            _skipped = len(_all_rows) - len(_supported_rows)
            print(
                f"[INFO] Boards CSV: {args.boards_csv} | total_rows={len(_all_rows)} | "
                f"supported_rows={len(_supported_rows)} | skipped_unsupported={_skipped}"
            )
            if _skipped:
                _unsupported_counts = Counter(
                    (r.get("platform") or "") for r in _all_rows
                    if (r.get("platform") or "") not in BOARDS_SUPPORTED_PLATFORMS
                )
                top = ", ".join(f"{k}:{v}" for k, v in _unsupported_counts.most_common(10))
                if top:
                    print(f"[INFO] Unsupported platforms in CSV (top): {top}")
        except Exception as e:
            print(f"[WARN] Could not summarize boards CSV: {type(e).__name__}: {e}")

        def run_one_boards_batch() -> int:
            t0_batch = time.time()
            matched, latest_keys, errors, new_cursor, bootstrap_keys, bootstrap_boards, per_platform = run_boards_sweep(
                seen=seen,
                boards_seen=boards_seen,
                dead_boards=dead_boards,
                dead_details=dead_details,
                boards_csv=args.boards_csv,
                batch_size=args.boards_batch_size,
                timeout=args.boards_timeout,
                workers=BOARDS_WORKERS,
                suppress_new_boards=not args.test_email,
            )

            if bootstrap_keys:
                seen.update(bootstrap_keys)
            if bootstrap_boards:
                boards_seen.update(bootstrap_boards)

            _subject_prefix = os.getenv("SUBJECT_PREFIX") or "[Boards Alerts]"
            if args.test_email:
                sample_yes = [j for j in matched if classify_title(j.get("title", "")) == "yes"][:2]
                sample_maybe = [j for j in matched if classify_title(j.get("title", "")) == "maybe"][:1]
                if not (sample_yes or sample_maybe):
                    raise RuntimeError("No matching jobs found to send in test email.")
                if args.no_email:
                    print(f"[TEST] no-email enabled; would have sent {len(sample_yes) + len(sample_maybe)} job(s) to {ALERT_TO_EMAIL}.")
                else:
                    send_email_digest(sample_yes, sample_maybe, subject_prefix="[TEST " + _subject_prefix.lstrip("["))
                    print(f"[TEST] Sent a test boards email with {len(sample_yes) + len(sample_maybe)} job(s) to {ALERT_TO_EMAIL}.")

                if not args.dry_run:
                    save_boards_cursor(BOARDS_CURSOR_PATH, new_cursor)
                    save_boards_seen(BOARDS_SEEN_PATH, boards_seen)
                    save_boards_dead(BOARDS_DEAD_PATH, dead_boards)
                    save_dead_details(BOARDS_DEAD_DETAILS_PATH, dead_details)
                    export_dead_boards_csv(dead_details, args.export_dead_csv)

                if errors:
                    print("[WARN] Some boards failed:")
                    for e in errors:
                        print("  -", e)
                raise SystemExit(0)

            if not os.path.exists(STATE_PATH):
                initial_count = len(seen | latest_keys)
                if args.dry_run:
                    print(f"[BOOTSTRAP] (dry-run) Would save {initial_count} seen_ids (boards). No email sent.")
                    raise SystemExit(0)

                seen.update(latest_keys)
                save_seen_ids(STATE_PATH, seen)
                save_boards_cursor(BOARDS_CURSOR_PATH, new_cursor)
                save_boards_seen(BOARDS_SEEN_PATH, boards_seen)
                save_boards_dead(BOARDS_DEAD_PATH, dead_boards)
                save_dead_details(BOARDS_DEAD_DETAILS_PATH, dead_details)
                export_dead_boards_csv(dead_details, args.export_dead_csv)
                print(f"[BOOTSTRAP] Saved {initial_count} seen_ids (boards). No email sent.")
                _ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
                _dur = round(time.time() - t0_batch, 1)
                _append_run_log({"ts": _ts, "mode": "boards", "per_source": per_platform, "duration_s": _dur, "cursor": new_cursor})
                print(_fmt_run_summary("boards", _ts, per_platform, new_cursor, _dur))
                raise SystemExit(0)

            new_keys = latest_keys - seen
            for _k in new_keys:
                _plat = _k.split(":")[0]
                if _plat in per_platform:
                    per_platform[_plat]["new"] = per_platform[_plat].get("new", 0) + 1

            new_yes = [j for j in matched if classify_title(j.get("title", "")) == "yes" and j.get("key") in new_keys]
            new_maybe = [j for j in matched if classify_title(j.get("title", "")) == "maybe" and j.get("key") in new_keys]

            if new_yes or new_maybe:
                if args.no_email:
                    print(f"[ALERT] no-email enabled; {len(new_yes)} yes + {len(new_maybe)} maybe new job(s) detected (not emailed).")
                else:
                    send_email_digest(new_yes, new_maybe, subject_prefix=_subject_prefix)
                    print(f"[ALERT] Sent boards digest for {len(new_yes)} yes + {len(new_maybe)} maybe new job(s).")
                    for _j in new_yes + new_maybe:
                        _plat = (_j.get("key") or "").split(":")[0]
                        if _plat in per_platform:
                            per_platform[_plat]["emailed"] = per_platform[_plat].get("emailed", 0) + 1
            else:
                print("[OK] No new boards jobs.")

            if not args.dry_run:
                seen.update(latest_keys)
                save_seen_ids(STATE_PATH, seen)
                save_boards_cursor(BOARDS_CURSOR_PATH, new_cursor)
                save_boards_seen(BOARDS_SEEN_PATH, boards_seen)
                save_boards_dead(BOARDS_DEAD_PATH, dead_boards)
                save_dead_details(BOARDS_DEAD_DETAILS_PATH, dead_details)
                export_dead_boards_csv(dead_details, args.export_dead_csv)

            if errors:
                print("[WARN] Some boards failed (sweep still ran):")
                for e in errors:
                    print("  -", e)

            for _err_str in errors:
                _words = _err_str.split(" ")
                _plat = _words[1] if _words and _words[0] == "DEAD" else (_words[0] if _words else "unknown")
                _ps = per_platform.setdefault(_plat, {"fetched": 0, "title_ok": 0, "loc_ok": 0, "new": 0, "emailed": 0})
                _ps["errors"] = _ps.get("errors", 0) + 1

            _ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
            _dur = round(time.time() - t0_batch, 1)
            if not args.dry_run:
                _append_run_log({"ts": _ts, "mode": "boards", "per_source": per_platform, "duration_s": _dur, "cursor": new_cursor})
            print(_fmt_run_summary("boards", _ts, per_platform, new_cursor, _dur))

            return new_cursor

        if args.boards_run_until_wrap:
            it = 0
            try:
                while True:
                    it += 1
                    cur = run_one_boards_batch()
                    print(f"[PROGRESS] cursor_now: {cur}")
                    if cur == 0:
                        print("[DONE] cursor wrapped to 0 (full sweep completed).")
                        break
                    if it >= args.boards_max_iterations:
                        print(f"[STOP] Reached boards_max_iterations={args.boards_max_iterations} without wrap; stopping for safety.")
                        break
            except KeyboardInterrupt:
                print("\n[STOP] Interrupted by user (state saved up to last completed batch).")
        else:
            _ = run_one_boards_batch()

    else:
        main(test_email=args.test_email, no_email=args.no_email, dry_run=args.dry_run)