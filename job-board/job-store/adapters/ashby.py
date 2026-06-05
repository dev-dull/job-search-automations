"""Ashby list-jobs adapter.

Calls `api.ashbyhq.com/posting-api/job-board/{org}`. The response contains
plain-text descriptions inline (`descriptionPlain`) so no HTML stripping is
needed. Per-posting URL is in `jobUrl`.

`identifier` shape: `{"org": "<org-slug>"}`.
The org slug is the path segment in `jobs.ashbyhq.com/<slug>` URLs.
"""

from __future__ import annotations

import html
import json
import re
import urllib.parse
import urllib.request
from typing import Any


API_URL = "https://api.ashbyhq.com/posting-api/job-board/{org}"
TIMEOUT_SEC = 20

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _strip_html(s: str) -> str:
    if not s:
        return ""
    no_tags = _TAG.sub(" ", s)
    decoded = html.unescape(no_tags)
    return _WS.sub(" ", decoded).strip()


def list_jobs(identifier: dict[str, Any]) -> list[dict[str, Any]]:
    """Return listed postings, newest-first by `publishedAt`."""
    org = (identifier or {}).get("org")
    if not org:
        raise ValueError("ashby identifier missing required 'org' key")

    url = API_URL.format(org=urllib.parse.quote(org, safe=""))
    req = urllib.request.Request(url, headers={"User-Agent": "job-store-poller/0.1"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
        data = json.load(resp)

    raw = [j for j in (data.get("jobs") or []) if j.get("isListed") is not False]
    raw.sort(key=lambda j: j.get("publishedAt") or "", reverse=True)

    out: list[dict[str, Any]] = []
    for job in raw:
        description = job.get("descriptionPlain") or _strip_html(job.get("descriptionHtml", ""))
        # Ashby exposes `location` for the primary, plus `secondaryLocations`
        # (list of strings). Join them so a multi-location posting like
        # "US-Remote, Dublin, Bengaluru" matches a US-only allowlist.
        parts = [job.get("location")] + (job.get("secondaryLocations") or [])
        location = ", ".join([str(p) for p in parts if p])
        out.append({
            "url": job.get("jobUrl"),
            "title": job.get("title"),
            "description": description,
            "posted_at": (job.get("publishedAt") or "")[:10] or None,
            "location": location,
        })
    return out


def fetch_description(job: dict[str, Any]) -> str:
    """Ashby returns descriptions inline; no extra fetch needed."""
    return job.get("description") or ""
