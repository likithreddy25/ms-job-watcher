#!/usr/bin/env python3
"""One-time migration: back-fill req: keys for Workday url: entries in seen_boards.json.

For every Workday URL key like:
  workday:zoom:Zoom:url:https://zoom.wd5.myworkdayjobs.com/en-US/Zoom/job/..._R18583-1

extract the stable req number (R18583) from the URL using the same regex as
workday_key_from_post(), and add the matching req key:
  workday:zoom:Zoom:req:R18583

This ensures jobs that were first seen with the old URL-based key won't be
re-alerted when they reappear and generate a req-based key.
"""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

STATE_PATH = Path("state/seen_boards.json")

# Same regex as _WD_REQ_RE in watcher.py
_WD_REQ_RE = re.compile(r"[/_]([A-Za-z]{0,3}-?\d{3,})(?:-\d+)?(?:[/?#_]|$)")

_WD_URL_PREFIX = re.compile(
    r"^(workday:[^:]+:[^:]+):url:https?://[^/]+"  # captures "workday:tenant:site"
)


def migrate(path: Path, dry_run: bool = False) -> None:
    data = json.loads(path.read_text())
    seen: list = data.get("seen_ids", [])
    seen_set = set(seen)

    added: list[str] = []
    for key in seen:
        # Only process Workday url: keys
        m_prefix = _WD_URL_PREFIX.match(key)
        if not m_prefix:
            continue
        prefix = m_prefix.group(1)  # e.g. "workday:zoom:Zoom"

        # Extract the URL portion (everything after ":url:")
        url_part = key.split(":url:", 1)[1]

        # Try to find a req number in the URL
        m_req = _WD_REQ_RE.search(url_part)
        if not m_req:
            continue

        req_key = f"{prefix}:req:{m_req.group(1)}"
        if req_key not in seen_set:
            added.append(req_key)
            seen_set.add(req_key)
            print(f"  + {req_key}  (from {key})")

    print(f"\n{'[DRY RUN] ' if dry_run else ''}Added {len(added)} req key(s) from {len(seen)} existing entries.")

    if dry_run or not added:
        return

    data["seen_ids"] = sorted(seen_set)
    data["updated_utc"] = datetime.now(timezone.utc).isoformat()
    path.write_text(json.dumps(data, indent=2) + "\n")
    print(f"Saved {path} ({len(data['seen_ids'])} total seen_ids).")


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    if not STATE_PATH.exists():
        print(f"ERROR: {STATE_PATH} not found. Run from repo root.", file=sys.stderr)
        sys.exit(1)
    migrate(STATE_PATH, dry_run=dry_run)
