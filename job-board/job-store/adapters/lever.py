"""Lever list-jobs adapter.

Calls `api.lever.co/v0/postings/{company}?mode=json`. Returns a top-level
array (no envelope) of posting objects. Posting URL is in `hostedUrl`.

Lever splits the JD across `descriptionPlain` (the lead paragraph) and
`descriptionBodyPlain` (the meat). We concatenate both for the scorer.

`identifier` shape: `{"company": "<lever-slug>"}`.
The slug is the path segment in `jobs.lever.co/<slug>` URLs.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any


API_URL = "https://api.lever.co/v0/postings/{company}?mode=json"
TIMEOUT_SEC = 20


def list_jobs(identifier: dict[str, Any]) -> list[dict[str, Any]]:
    """Return postings newest-first by `createdAt` (epoch ms)."""
    company = (identifier or {}).get("company")
    if not company:
        raise ValueError("lever identifier missing required 'company' key")

    url = API_URL.format(company=urllib.parse.quote(company, safe=""))
    req = urllib.request.Request(url, headers={"User-Agent": "job-store-poller/0.1"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
        data = json.load(resp)

    raw = list(data or [])
    raw.sort(key=lambda j: j.get("createdAt") or 0, reverse=True)

    out: list[dict[str, Any]] = []
    for job in raw:
        parts = [job.get("descriptionPlain", ""), job.get("descriptionBodyPlain", "")]
        description = "\n\n".join(p for p in parts if p).strip()
        # createdAt is milliseconds since epoch; turn it into an ISO date.
        created_ms = job.get("createdAt")
        posted_at = None
        if isinstance(created_ms, (int, float)) and created_ms > 0:
            from datetime import datetime, timezone
            posted_at = datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc).date().isoformat()
        cats = job.get("categories") or {}
        out.append({
            "url": job.get("hostedUrl"),
            "title": job.get("text"),
            "description": description,
            "posted_at": posted_at,
            "location": cats.get("location") or job.get("country") or "",
        })
    return out


def fetch_description(job: dict[str, Any]) -> str:
    """Lever returns descriptions inline; no extra fetch needed."""
    return job.get("description") or ""
