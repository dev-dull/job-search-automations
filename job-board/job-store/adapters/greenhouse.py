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
import urllib.parse
import urllib.request
from typing import Any


BOARDS_API = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true"
BOARDS_API_JOB = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{jid}"
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


# ---------------------------------------------------------------------------
# Custom-domain resolution (issue #43): Greenhouse boards served on a vanity
# host (jobs.elastic.co) expose NO board token anywhere in the page — the only
# Greenhouse signal is the `gh_jid` query param. Guess candidate tokens from
# the domain and VERIFY each against the public board API using that gh_jid;
# a wrong guess 404s and is discarded, so resolution fails closed.
# ---------------------------------------------------------------------------

# Two-part public suffixes we're likely to meet on careers domains. Enough to
# get the registrable label right (acme.co.uk -> acme); exotic suffixes just
# yield a candidate that fails verification, which is safe.
_TWO_PART_SUFFIXES = {
    "co.uk", "org.uk", "ac.uk", "co.jp", "co.in", "co.nz", "co.za", "co.kr",
    "com.au", "com.br", "com.mx", "com.sg", "com.cn",
}
# Hostname labels that are site furniture, never the company.
_GENERIC_LABELS = {"www", "jobs", "careers", "career", "apply", "job", "boards", "talent"}


def _registrable_label(hostname: str) -> str | None:
    """The company-ish label of a hostname: jobs.elastic.co -> elastic,
    careers.acme.co.uk -> acme."""
    labels = [p for p in (hostname or "").lower().split(".") if p]
    if len(labels) < 2:
        return None
    suffix_len = 2 if ".".join(labels[-2:]) in _TWO_PART_SUFFIXES else 1
    remaining = labels[:-suffix_len]
    return remaining[-1] if remaining else None


def board_candidates(hostname: str) -> list[str]:
    """Ordered candidate board tokens derived from a vanity careers hostname."""
    label = _registrable_label(hostname)
    if not label or label in _GENERIC_LABELS:
        return []
    out = [label]
    dehyphenated = label.replace("-", "")
    if dehyphenated != label:
        out.append(dehyphenated)
    return out


def verify_board(board: str, gh_jid: str) -> bool:
    """True if this specific posting exists on this board — the strong check
    that makes domain-guessing safe."""
    url = BOARDS_API_JOB.format(board=urllib.parse.quote(board, safe=""),
                                jid=urllib.parse.quote(str(gh_jid), safe=""))
    req = urllib.request.Request(url, headers={"User-Agent": "job-store-poller/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
            return json.load(resp).get("id") is not None
    except Exception:
        return False


def resolve_board_from_url(careers_url: str, *, verify=None) -> str | None:
    """Resolve a verified board token from a Greenhouse custom-domain posting
    URL (one carrying gh_jid). Returns the token, or None when the URL has no
    gh_jid or no candidate verifies. `verify` is injectable for tests; at most
    len(candidates) (≤2) API calls."""
    verify = verify or verify_board
    try:
        u = urllib.parse.urlsplit(careers_url or "")
    except ValueError:
        return None
    gh_jid = urllib.parse.parse_qs(u.query).get("gh_jid", [None])[0]
    if not gh_jid:
        return None
    for cand in board_candidates(u.hostname or ""):
        if verify(cand, gh_jid):
            return cand
    return None


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
