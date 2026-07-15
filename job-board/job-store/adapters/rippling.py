"""Rippling list-jobs adapter.

Rippling hosts company job boards at `ats.rippling.com/<slug>/jobs` and exposes
a public, unauthenticated list API:

    GET https://api.rippling.com/platform/api/ats/v1/board/<slug>/jobs
    -> [{"name", "url", "uuid", "department", "workLocation"}, ...]

The list carries no posted date and no description. The job page itself is a
client-rendered Next.js app (no ld+json script tag, no server h1), but the full
posting is SSR'd into the `__NEXT_DATA__` blob: `props.pageProps.apiData.jobPost`
holds `name`, `companyName`, `createdOn` (a real posted date), and `description`
as {"company": <html>, "role": <html>}. `fetch_description()` parses that blob —
and back-fills `job["posted_at"]` from `createdOn` as a documented side effect,
since the list endpoint can't provide it.

`identifier` shape (built by `app.py:detect_ats()`): {"slug": "<board-slug>"}.
"""

from __future__ import annotations

import html
import json
import re
import urllib.parse
import urllib.request
from typing import Any


BOARD_API = "https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs"
TIMEOUT_SEC = 20

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_NEXT_DATA = re.compile(
    r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


def _strip_html(s: str) -> str:
    if not s:
        return ""
    no_tags = _TAG.sub(" ", s)
    decoded = html.unescape(no_tags)
    return _WS.sub(" ", decoded).strip()


def _http_get(url: str, *, accept: str = "application/json") -> str:
    req = urllib.request.Request(url, headers={
        # The Next.js pages 403 bot-ish agents; a browser UA works for both.
        "User-Agent": "Mozilla/5.0 (compatible; job-store-poller/0.1)",
        "Accept": accept,
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _location_text(work_location: Any) -> str:
    """workLocation shows up as a plain string or a small dict depending on the
    board; accept both."""
    if isinstance(work_location, str):
        return work_location
    if isinstance(work_location, dict):
        return str(work_location.get("label") or work_location.get("name") or "")
    return ""


def job_post_from_page(page_html: str) -> dict[str, Any]:
    """The SSR'd jobPost object from a Rippling job page, or {} if absent."""
    m = _NEXT_DATA.search(page_html or "")
    if not m:
        return {}
    try:
        data = json.loads(m.group(1))
        return (data.get("props", {}).get("pageProps", {})
                .get("apiData", {}).get("jobPost")) or {}
    except (ValueError, AttributeError):
        return {}


def flatten_description(description: Any) -> str:
    """description is {"company": <html>, "role": <html>} — role first (it's
    the JD; the company blurb is boilerplate context)."""
    if isinstance(description, str):
        return _strip_html(description)
    if not isinstance(description, dict):
        return ""
    parts = [description.get("role"), description.get("company")]
    return "\n\n".join(p for p in (_strip_html(x or "") for x in parts) if p)


def verify_board(slug: str) -> bool:
    """True if the slug resolves on the public board API. Probe-at-create
    (see app.py): a mis-parsed slug is rejected instead of saved as a target
    that 404s on every poll."""
    if not slug:
        return False
    url = BOARD_API.format(slug=urllib.parse.quote(slug, safe=""))
    try:
        return isinstance(json.loads(_http_get(url)), list)
    except Exception:
        return False


def list_jobs(identifier: dict[str, Any]) -> list[dict[str, Any]]:
    """Return postings as lightweight stubs (no description, no posted date —
    both live on the job page and are filled in by `fetch_description`).

    NOTE: the board API documents no ordering and items carry no date, so
    newest-first cannot be guaranteed — the poller's stop-when-seen may stop
    early on boards where order shifts. Rippling boards are small (tens of
    postings), which bounds the damage.
    """
    slug = (identifier or {}).get("slug")
    if not slug:
        raise ValueError("rippling identifier missing required 'slug' key")

    url = BOARD_API.format(slug=urllib.parse.quote(slug, safe=""))
    raw = json.loads(_http_get(url))
    if not isinstance(raw, list):
        raise ValueError(f"unexpected rippling board response for {slug!r}")

    out: list[dict[str, Any]] = []
    for p in raw:
        job_url = p.get("url") or ""
        if not job_url:
            continue
        out.append({
            "url": job_url,
            "title": p.get("name"),
            # description + posted_at deliberately omitted — fetch_description
            # fills both from the job page's __NEXT_DATA__.
            "posted_at": None,
            "location": _location_text(p.get("workLocation")),
        })
    return out


def fetch_description(job: dict[str, Any]) -> str:
    """Fetch the JD from the job page's __NEXT_DATA__. Returns "" on transient
    failure (the poller skips scoring when the description is too short).

    Side effect: sets job["posted_at"] from the page's `createdOn` when
    present — the list API has no dates, and a real posted date feeds
    age-decay ranking far better than the discovered_at fallback."""
    url = job.get("url")
    if not url:
        return ""
    try:
        page = _http_get(url, accept="text/html")
    except Exception:
        return ""
    post = job_post_from_page(page)
    created = post.get("createdOn") or ""
    if created and not job.get("posted_at"):
        job["posted_at"] = str(created)[:10]
    return flatten_description(post.get("description"))
