"""
One-time script to populate jobs_db.json for the dashboard.
Fetches jobs from Greenhouse and Lever boards WITHOUT sending emails
and WITHOUT updating seen state.

Run: python backfill_dashboard.py
"""
import json
import re
import time
import requests
from pathlib import Path
from datetime import datetime, timezone

STATE_DIR = Path("state")
JOBS_DB_PATH = STATE_DIR / "jobs_db.json"
BOARDS_CSV = Path("data/boards/JOB_BOARDS_PURE_WORKING_SUPPORTED_round2.csv")

# ── Classifier (ported from jboard_zm) ───────────────────────────────────────

_DASH_STRONG = [
    "data analyst", "data analytics", "data scientist", "data science",
    "data engineer", "data engineering", "analytics engineer", "analytics analyst",
    "business intelligence", "bi analyst", "bi engineer", "bi developer",
    "intelligence analyst", "machine learning engineer", "ml engineer",
    "applied scientist", "research scientist", "decision scientist", "ai data",
    "quantitative analyst", "quant analyst", "statistical analyst",
    "statistical modeler", "forecasting analyst",
    "data platform engineer", "data infrastructure engineer",
    "data reliability engineer", "data quality engineer", "data quality analyst",
    "data governance", "data management analyst", "data operations analyst",
    "data architect", "analytics architect",
    "etl engineer", "etl developer", "elt engineer", "data warehouse engineer",
    "data warehousing", "dwh engineer", "data modeler", "data modeling",
    "insights analyst", "insights engineer", "reporting analyst",
    "product analyst", "growth analyst", "marketing analyst",
    "financial analyst", "operations analyst", "clinical data analyst",
    "research analyst", "feature engineer", "mlops engineer",
    "ml platform engineer", "ai engineer",
    "llm engineer", "llm data", "prompt engineer",
    "generative ai engineer", "gen ai engineer", "nlp engineer",
    "natural language processing engineer", "natural language processing scientist",
    "computer vision engineer", "computer vision scientist",
    "multimodal", "foundation model",
    "analytics consultant", "data consultant", "data advisor",
]

_DASH_WEAK = [
    "analytics", "intelligence", "insights", "tableau", "power bi",
    "snowflake", "spark", "databricks", "warehouse", "pipeline",
    "etl", "elt", "dbt", "airflow", "kafka", "flink", "hadoop",
    "generative ai", "gen ai", "large language model", "llm", "nlp",
    "ai analyst", "ai scientist", "business analyst",
    "business intelligence analyst", "operations research",
]

_DASH_HARD_EXCLUDES = [
    "software engineer", "software developer", "software development engineer",
    "frontend engineer", "front-end engineer", "front end engineer",
    "backend engineer", "back-end engineer", "back end engineer",
    "full stack engineer", "fullstack engineer", "full-stack engineer",
    "mobile engineer", "ios engineer", "android engineer",
    "embedded engineer", "embedded software", "systems engineer",
    "site reliability", "sre", "devops", "platform engineer",
    "cloud engineer", "infrastructure engineer", "network engineer",
    "security engineer", "cybersecurity", "penetration tester",
    "quality assurance", "qa engineer", "qa analyst",
    "test engineer", "quality engineer", "validation engineer",
    "product manager", "program manager", "project manager",
    "engineering manager", "scrum master", "agile coach",
    "sales", "account executive", "account manager",
    "solutions engineer", "pre-sales", "recruiter",
    "talent acquisition", "human resources",
    "customer support", "technical support", "support engineer",
    "help desk", "it support", "it administrator",
    "systems administrator", "sysadmin", "database administrator",
    "data entry", "data center", "accounts payable", "billing analyst",
    "claims analyst", "procurement analyst", "inventory analyst",
    "legal analyst", "compliance analyst",
    "hardware engineer", "electrical engineer", "mechanical engineer",
    "manufacturing engineer", "supply chain",
]

_DASH_HARD_EXCLUDE_RE = [
    r"\bintern(ship)?\b", r"\bco[- ]?op\b", r"\bcoop\b",
    r"\bapprentice\b", r"\bpart[- ]time\b",
]

_DASH_CLEARANCE_PHRASES = [
    "security clearance", "clearance required", "clearance preferred",
    "clearance eligible", "active clearance", "active secret", "secret clearance",
    "top secret", "ts/sci", "ts sci", "sci clearance", "dod clearance",
    "dod secret", "public trust", "polygraph",
    "us citizen", "u.s. citizen", "must be a citizen",
    "citizenship required", "citizenship eligibility", "must hold clearance",
]

_DASH_CLEARANCE_RE = [
    r"\bts[/\s\-]?sci\b", r"\btop\s+secret\b", r"\bpolygraph\b",
    r"\bpublic\s+trust\b", r"\bclearance\b", r"\bus\s+citizen",
    r"\bcitizenship\b", r"\bsci\b",
]

_DASH_VERY_SENIOR = frozenset(["director", "vp", "vice president", "head of", "fellow", "distinguished"])
_DASH_SENIORITY_TOKENS = [
    "senior", "sr", "staff", "principal", "lead", "architect",
    "distinguished", "fellow", "director", "manager", "head of",
    "vp", "vice president",
]

_DASH_SAFETY_NETS = frozenset([
    "data security analyst", "data quality engineer", "data governance",
    "data management", "data operations", "data steward", "data catalog",
    "data platform engineer", "data platform", "data infrastructure",
    "data reliability engineer", "data product manager",
    "data program manager", "analytics program manager",
    "generative ai", "gen ai", "llm engineer", "prompt engineer",
    "ai data engineer",
])

_DASH_GOOD_TECH = [
    "python", "pytorch", "tensorflow", "keras", "scikit-learn", "sklearn",
    "xgboost", "lightgbm", "mlflow", "rag", "openai", "huggingface",
    "langchain", "faiss", "transformers", "nlp", "computer vision",
    "deep learning", "machine learning", "pyspark", "apache spark",
    "databricks", "airflow", "dbt", "kafka", "etl", "elt",
    "aws", "gcp", "azure", "docker", "kubernetes",
    "postgresql", "postgres", "mysql", "mongodb", "snowflake",
    "bigquery", "duckdb", "redshift", "fastapi", "flask", "sql",
]
_DASH_BAD_TECH = [
    "java", "spring boot", "spring framework", "j2ee", "hibernate",
    "c++", "c/c++", "golang", "rust", "c#", ".net",
    "ruby", "rails", "php", "swift", "kotlin", "android",
    "fpga", "vhdl", "verilog", "embedded", "firmware",
]
_DASH_H1B_POS = ["visa sponsorship", "sponsor h1b", "h1b sponsorship", "will sponsor", "open to sponsorship"]
_DASH_H1B_NEG = ["no sponsorship", "no visa", "not sponsor", "cannot sponsor",
                  "must be authorized", "us citizen only", "citizenship required"]
_DASH_ROLE_MAP = {
    "ml_engineer": ["machine learning engineer", "ml engineer", "ai engineer", "applied scientist",
                    "research engineer", "mlops", "model engineer", "llm engineer", "nlp engineer",
                    "computer vision engineer", "generative ai", "gen ai"],
    "data_scientist": ["data scientist", "data science", "applied researcher",
                       "quantitative analyst", "statistical analyst", "decision scientist"],
    "data_engineer": ["data engineer", "analytics engineer", "dataops", "data pipeline",
                      "data platform", "data architect", "etl engineer", "elt engineer",
                      "data warehouse", "data modeler"],
    "data_analyst": ["data analyst", "analytics analyst", "product analyst", "bi analyst",
                     "business intelligence", "insights analyst", "reporting analyst",
                     "growth analyst", "marketing analyst", "financial analyst"],
    "swe": ["software engineer", "software developer", "backend engineer", "full stack", "sde"],
}

# Location filtering (unchanged, with word-boundary fix for state codes)
_FULL_STATES = [
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana",
    "maine", "maryland", "massachusetts", "michigan", "minnesota",
    "mississippi", "missouri", "montana", "nebraska", "nevada",
    "new hampshire", "new jersey", "new mexico", "new york",
    "north carolina", "north dakota", "ohio", "oklahoma", "oregon",
    "pennsylvania", "rhode island", "south carolina", "south dakota",
    "tennessee", "texas", "utah", "vermont", "virginia", "washington",
    "west virginia", "wisconsin", "wyoming",
]
_STATE_CODES = [
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi",
    "id", "il", "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi",
    "mn", "ms", "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc",
    "nd", "oh", "ok", "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut",
    "vt", "va", "wa", "wv", "wi", "wy", "dc",
]
_US_CITIES = [
    "new york", "san francisco", "los angeles", "chicago", "boston",
    "seattle", "austin", "denver", "atlanta", "miami", "dallas",
    "houston", "phoenix", "portland", "minneapolis", "philadelphia",
    "san jose", "san diego", "washington dc", "washington, dc",
    "silicon valley", "bay area", "raleigh", "charlotte", "nashville",
    "salt lake city", "las vegas", "pittsburgh", "columbus",
    "fairfax", "mclean", "arlington", "reston", "herndon", "tysons",
    "new england",
]


def loc_ok(loc: str) -> bool:
    l = loc.lower().strip()
    if not l or l == "n/a":
        return True
    if any(x in l for x in ["remote", "united states", "usa", "u.s.", "anywhere"]):
        return True
    if any(s in l for s in _FULL_STATES):
        return True
    if any(c in l for c in _US_CITIES):
        return True
    for code in _STATE_CODES:
        if re.search(r'\b' + code + r'\b', l):
            return True
    return False


def _dash_classify_title(title: str) -> dict:
    """jboard_zm classifier — returns score 0-100 and bucket yes/maybe/no."""
    t = re.sub(r"\s+", " ", (title or "").strip().lower())
    if not t:
        return {"score": 0, "bucket": "no"}

    for phrase in _DASH_CLEARANCE_PHRASES:
        if phrase in t:
            return {"score": 0, "bucket": "no"}
    for pat in _DASH_CLEARANCE_RE:
        if re.search(pat, t):
            return {"score": 0, "bucket": "no"}

    is_safety_net = any(sn in t for sn in _DASH_SAFETY_NETS)

    for pat in _DASH_HARD_EXCLUDE_RE:
        if re.search(pat, t):
            return {"score": 0, "bucket": "no"}

    if not is_safety_net:
        for phrase in _DASH_HARD_EXCLUDES:
            if phrase in t:
                return {"score": 0, "bucket": "no"}

    strong = any(p in t for p in _DASH_STRONG)
    weak = any(p in t for p in _DASH_WEAK)
    if not (strong or weak):
        return {"score": 0, "bucket": "no"}

    score = 90 if strong else 55

    for tok in _DASH_SENIORITY_TOKENS:
        if re.search(rf"\b{re.escape(tok)}\b", t):
            score = min(score, 34 if tok in _DASH_VERY_SENIOR else 65)
            break

    score = max(0, min(score, 100))
    bucket = "yes" if score >= 70 else ("maybe" if score >= 40 else "no")
    return {"score": score, "bucket": bucket}


def title_ok(title: str) -> bool:
    """Return True if the jboard_zm classifier accepts this title."""
    result = _dash_classify_title(title)
    return result["bucket"] in ("yes", "maybe")


def classify(title: str, company: str = "", loc: str = "") -> dict:
    """Full classification dict for a job entry."""
    text = f"{title} {company} {loc}".lower()
    cls = _dash_classify_title(title)

    good_tech = [t for t in _DASH_GOOD_TECH if t in text]
    bad_tech = [t for t in _DASH_BAD_TECH if t in text]

    clearance = any(p in text for p in _DASH_CLEARANCE_PHRASES)

    h1b = "unknown"
    if any(k in text for k in _DASH_H1B_POS):
        h1b = "yes"
    elif any(k in text for k in _DASH_H1B_NEG):
        h1b = "no"

    role_category = "other"
    tl = title.lower()
    for cat, phrases in _DASH_ROLE_MAP.items():
        if any(p in tl for p in phrases):
            role_category = cat
            break

    exp_level = "mid"
    if any(x in tl for x in ["junior", "entry", "associate", "new grad", "recent grad"]):
        exp_level = "entry"

    return {
        "bucket": cls["bucket"] if cls["bucket"] != "no" else "yes",  # already filtered by title_ok
        "score": cls["score"],
        "role_category": role_category,
        "exp_level": exp_level,
        "clearance_required": clearance,
        "h1b_status": h1b,
        "good_tech": good_tech,
        "bad_tech": bad_tech,
    }


# ── Fetchers ──────────────────────────────────────────────────────────────────

def fetch_greenhouse(company: str) -> list:
    try:
        url = f"https://boards-api.greenhouse.io/v1/boards/{company}/jobs?content=false"
        r = requests.get(url, timeout=10)
        if r.status_code != 200:
            return []
        jobs = []
        for j in r.json().get("jobs", []):
            title = j.get("title", "")
            loc = j.get("location", {}).get("name", "")
            if title_ok(title) and loc_ok(loc):
                jobs.append({
                    "key": f"greenhouse:{company}:{j['id']}",
                    "title": title,
                    "company": company.replace("-", " ").title(),
                    "url": j.get("absolute_url", ""),
                    "location": loc,
                    "posted": j.get("updated_at", "")[:10],
                })
        return jobs
    except Exception:
        return []


def fetch_lever(company: str) -> list:
    try:
        url = f"https://api.lever.co/v0/postings/{company}?mode=json"
        r = requests.get(url, timeout=10)
        if r.status_code != 200:
            return []
        jobs = []
        for j in r.json():
            title = j.get("text", "")
            loc = (j.get("categories", {}).get("location", "")
                   or (j.get("categories", {}).get("allLocations") or [""])[0])
            if title_ok(title) and loc_ok(loc):
                jobs.append({
                    "key": f"lever:{company}:{j['id']}",
                    "title": title,
                    "company": company.replace("-", " ").title(),
                    "url": j.get("hostedUrl", ""),
                    "location": loc,
                    "posted": "",
                })
        return jobs
    except Exception:
        return []


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("Loading board list...")
    greenhouse_cos, lever_cos = [], []
    if BOARDS_CSV.exists():
        import csv as _csv
        with open(BOARDS_CSV) as f:
            for row in _csv.DictReader(f):
                src = row.get("platform", "").strip().lower()
                url = row.get("board_url", "").strip()
                m = re.search(r"greenhouse\.io/([^/\s]+)", url)
                if src == "greenhouse" and m:
                    greenhouse_cos.append(m.group(1))
                m2 = re.search(r"lever\.co/([^/\s]+)", url)
                if src == "lever" and m2:
                    lever_cos.append(m2.group(1))

    print(f"  {len(greenhouse_cos)} Greenhouse boards, {len(lever_cos)} Lever boards")

    existing = []
    if JOBS_DB_PATH.exists():
        with open(JOBS_DB_PATH) as f:
            existing = json.load(f)
    existing_keys = {j["key"] for j in existing}

    now = datetime.now(timezone.utc).isoformat()
    all_new = []

    sample_gh = greenhouse_cos[:200]
    sample_lv = lever_cos[:200]

    print(f"Fetching {len(sample_gh)} Greenhouse + {len(sample_lv)} Lever boards...")
    for i, co in enumerate(sample_gh):
        for j in fetch_greenhouse(co):
            if j["key"] not in existing_keys:
                entry = {**j, "date_found": now, **classify(j["title"], j["company"], j["location"])}
                all_new.append(entry)
                existing_keys.add(j["key"])
        if (i + 1) % 20 == 0:
            print(f"  GH {i+1}/{len(sample_gh)} done, {len(all_new)} jobs so far")
        time.sleep(0.05)

    for i, co in enumerate(sample_lv):
        for j in fetch_lever(co):
            if j["key"] not in existing_keys:
                entry = {**j, "date_found": now, **classify(j["title"], j["company"], j["location"])}
                all_new.append(entry)
                existing_keys.add(j["key"])
        if (i + 1) % 20 == 0:
            print(f"  LV {i+1}/{len(sample_lv)} done, {len(all_new)} jobs so far")
        time.sleep(0.05)

    combined = existing + all_new
    combined = combined[-2000:]
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(JOBS_DB_PATH, "w") as f:
        json.dump(combined, f, indent=2)

    yes_count = sum(1 for j in combined if j.get("bucket") == "yes")
    maybe_count = sum(1 for j in combined if j.get("bucket") == "maybe")
    print(f"\nDone! Added {len(all_new)} jobs ({yes_count} yes / {maybe_count} maybe, {len(combined)} total).")
    print("Refresh your dashboard at http://localhost:5050")


if __name__ == "__main__":
    main()
