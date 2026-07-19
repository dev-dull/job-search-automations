"""iCIMS list-jobs adapter (classic-iframe tenants).

iCIMS has no public JSON API and some tenants bot-shell every request (see
docs/icims-adapter-notes.md — the original deferral). But many tenants serve
the classic iframe rendering path fully server-side:

- Listing: `https://<tenant>.icims.com/jobs/search?ss=1&in_iframe=1&pr=<page>`
  — `iCIMS_JobCardItem` rows with job links, titles, and "Job Locations",
  ordered newest-first, 50 per page.
- Detail: the job page with `?in_iframe=1` embeds a complete schema.org
  JobPosting ld+json (title, datePosted, full description).

Whether a given tenant is crawlable is probed at create time (`verify_tenant`,
wired into app.py's probe-at-create): crawlable tenants (e.g. HealthEdge)
verify and poll; locked-down ones (e.g. Rivian) fail closed with a clear error
instead of becoming targets that never work.

`identifier` shape: {"tenant": "<subdomain>"} (e.g. "careers-healthedge" from
`careers-healthedge.icims.com`).
"""

from __future__ import annotations

import html as html_mod
import json
import re
import urllib.parse
import urllib.request
from typing import Any


SEARCH_URL = "https://{tenant}.icims.com/jobs/search?ss=1&in_iframe=1&pr={page}"
TIMEOUT_SEC = 20
# Safety cap; 50 rows/page. Same rationale as the Workday cap.
MAX_PAGES = 10

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_ROW_SPLIT = re.compile(r'class="iCIMS_JobCardItem"')
_JOB_LINK = re.compile(r'href="[^"]*?/jobs/(\d+)/([^/"?]+)/job[^"]*"')
_ANCHOR_TITLE = re.compile(r'title="\d+\s*-\s*([^"]+)"')
_LOCATION = re.compile(r'Job Locations</span>\s*<span[^>]*>\s*([^<]*)<', re.S)
_LD_JSON = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S | re.I)


def _strip_html(s: str) -> str:
    if not s:
        return ""
    return _WS.sub(" ", html_mod.unescape(_TAG.sub(" ", s))).strip()


def _http_get(url: str) -> str:
    req = urllib.request.Request(url, headers={
        # iCIMS serves the classic path to browser-ish agents.
        "User-Agent": "Mozilla/5.0 (compatible; job-store-poller/0.1)",
        "Accept": "text/html",
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
        return resp.read().decode("utf-8", errors="replace")


def parse_listing(page_html: str, tenant: str) -> list[dict[str, Any]]:
    """Job stubs from one search page. Pure; tested against captured HTML."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for block in _ROW_SPLIT.split(page_html)[1:]:
        link = _JOB_LINK.search(block)
        if not link:
            continue
        job_id, slug = link.group(1), link.group(2)
        if job_id in seen:
            continue
        seen.add(job_id)
        t = _ANCHOR_TITLE.search(block)
        loc = _LOCATION.search(block)
        out.append({
            "url": f"https://{tenant}.icims.com/jobs/{job_id}/{slug}/job",
            "title": html_mod.unescape(t.group(1)).strip() if t else slug.replace("-", " "),
            # description + posted_at come from the detail page (ld+json).
            "posted_at": None,
            "location": html_mod.unescape(loc.group(1)).strip() if loc else "",
        })
    return out


def job_posting_ld(page_html: str) -> dict[str, Any]:
    """The schema.org JobPosting object from a page's ld+json blocks, or {}."""
    for block in _LD_JSON.findall(page_html or ""):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        for item in data if isinstance(data, list) else [data]:
            t = item.get("@type")
            if (isinstance(t, list) and "JobPosting" in t) or t == "JobPosting":
                return item
    return {}


def verify_tenant(tenant: str) -> bool:
    """True if the tenant serves a crawlable classic-iframe listing —
    probe-at-create's gate for the per-tenant variance iCIMS is known for."""
    if not tenant:
        return False
    url = SEARCH_URL.format(tenant=urllib.parse.quote(tenant, safe=""), page=0)
    try:
        return bool(parse_listing(_http_get(url), tenant))
    except Exception:
        return False


def list_jobs(identifier: dict[str, Any]) -> list[dict[str, Any]]:
    """Walk the paginated search newest-first; stop on an empty page or the
    page cap."""
    tenant = (identifier or {}).get("tenant")
    if not tenant:
        raise ValueError("icims identifier missing required 'tenant' key")

    out: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for page in range(MAX_PAGES):
        url = SEARCH_URL.format(tenant=urllib.parse.quote(tenant, safe=""), page=page)
        stubs = parse_listing(_http_get(url), tenant)
        fresh = [s for s in stubs if s["url"] not in seen_urls]
        if not fresh:
            break
        seen_urls.update(s["url"] for s in fresh)
        out.extend(fresh)
        if len(stubs) < 50:      # short page = last page
            break
    return out


def fetch_description(job: dict[str, Any]) -> str:
    """JD from the job page's `?in_iframe=1` ld+json. Returns "" on transient
    failure. Side effect: back-fills job["posted_at"] from datePosted (the
    listing carries no dates), mirroring the Rippling adapter."""
    url = job.get("url")
    if not url:
        return ""
    sep = "&" if "?" in url else "?"
    try:
        page = _http_get(f"{url}{sep}in_iframe=1")
    except Exception:
        return ""
    ld = job_posting_ld(page)
    posted = str(ld.get("datePosted") or "")[:10]
    if posted and not job.get("posted_at"):
        job["posted_at"] = posted
    return _strip_html(ld.get("description") or "")
