"""Greenhouse list-jobs adapter.

Calls the public job-board API (`boards-api.greenhouse.io`) which is the
canonical source companies expose for embed-based career pages. No auth.

`identifier` shape: `{"board": "<board-token>"}`.
The board token is the slug from `boards.greenhouse.io/<token>` or the `?for=`
query param on an embed URL.
"""

from __future__ import annotations

import html
import json
import re
import urllib.request
from typing import Any


BOARDS_API = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true"
TIMEOUT_SEC = 20

# Strip HTML tags from the Greenhouse `content` field. Greenhouse hosts JDs as
# HTML; we want plain text for the score prompt. This is intentionally crude —
# the score model handles whitespace noise fine.
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _strip_html(s: str) -> str:
    if not s:
        return ""
    no_tags = _TAG.sub(" ", s)
    decoded = html.unescape(no_tags)
    return _WS.sub(" ", decoded).strip()


def list_jobs(identifier: dict[str, Any]) -> list[dict[str, Any]]:
    """Return all current postings for a Greenhouse board, newest-first.

    Sorted on `first_published` (the original publish date) rather than
    `updated_at` so that a re-edited stale posting doesn't bubble to the top
    of the list and confuse the poller's stop-when-seen logic.
    """
    board = (identifier or {}).get("board")
    if not board:
        raise ValueError("greenhouse identifier missing required 'board' key")

    url = BOARDS_API.format(board=urllib.parse.quote(board, safe=""))
    req = urllib.request.Request(url, headers={"User-Agent": "job-store-poller/0.1"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
        data = json.load(resp)

    raw = data.get("jobs") or []
    raw.sort(key=lambda j: j.get("first_published") or "", reverse=True)

    out: list[dict[str, Any]] = []
    for job in raw:
        out.append({
            "url": job.get("absolute_url"),
            "title": job.get("title"),
            "description": _strip_html(job.get("content", "")),
            "posted_at": (job.get("first_published") or job.get("updated_at") or "")[:10] or None,
            "location": (job.get("location") or {}).get("name") or "",
        })
    return out


def fetch_description(job: dict[str, Any]) -> str:
    """Greenhouse returns descriptions inline; no extra fetch needed."""
    return job.get("description") or ""
