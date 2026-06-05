"""Workday list-jobs adapter.

Workday hosts each tenant's career site on `<tenant>.<region>.myworkdayjobs.com`
and exposes a JSON API under `/wday/cxs/<tenant>/<site>/`. The list endpoint
is a POST (not GET), pagination is mandatory, and the JD body lives in a
per-posting detail endpoint, not the list response.

This is qualitatively more involved than the other adapters — three reasons:

1. List endpoint is POST with a JSON facets body.
2. Pagination: typical page size is 20; need to walk until total is reached.
3. Detail fetch per posting to get the JD body.

`identifier` shape (built by `app.py:detect_ats()`):
    {"host": "<tenant>.<region>.myworkdayjobs.com",
     "lang": "<lang>",
     "site": "<site-id>"}

The tenant is the leftmost subdomain of `host`.
"""

from __future__ import annotations

import html
import json
import re
import urllib.parse
import urllib.request
from typing import Any


TIMEOUT_SEC = 30
PAGE_SIZE = 20
# Safety cap. Workday tenants can have hundreds of openings; a runaway scrape
# would slow each poll cycle significantly. Each page is one HTTP call; each
# posting we keep is one detail call. Cap roughly aligns with a 200-posting
# tenant — past that, the deny list isn't doing its job.
MAX_PAGES = 10

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _strip_html(s: str) -> str:
    if not s:
        return ""
    no_tags = _TAG.sub(" ", s)
    decoded = html.unescape(no_tags)
    return _WS.sub(" ", decoded).strip()


def _http_json(url: str, *, method: str = "GET", body: dict | None = None) -> dict:
    headers = {
        "User-Agent": "job-store-poller/0.1",
        "Accept": "application/json",
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
        return json.load(resp)


def _tenant(host: str) -> str:
    """Leftmost subdomain of the Workday host."""
    return host.split(".", 1)[0]


def _public_job_url(host: str, lang: str | None, site: str, external_path: str) -> str:
    """Reconstruct the user-facing URL from list-response fragments."""
    # external_path begins with "/job/..." per Workday's response shape.
    segments = [lang, site] if lang else [site]
    return f"https://{host}/{'/'.join(segments)}{external_path}"


def _detail_url(host: str, site: str, external_path: str) -> str:
    """The detail GET endpoint matching a list-response externalPath."""
    tenant = _tenant(host)
    # externalPath is `/job/<loc>/<slug>_<req>`; strip the leading "/job"
    # because the route already includes `.../job`.
    if external_path.startswith("/job"):
        external_path = external_path[len("/job"):]
    return f"https://{host}/wday/cxs/{tenant}/{urllib.parse.quote(site, safe='')}/job{external_path}"


def list_jobs(identifier: dict[str, Any]) -> list[dict[str, Any]]:
    """Return postings newest-first as lightweight stubs (no description).

    The CXS jobs endpoint defaults to "Most Recent" ordering, so we just
    preserve the API's order. The JD body lives in the per-posting detail
    endpoint and is fetched on demand by `fetch_description()` — this lets
    the poller skip detail fetches for deny-list misses and known URLs.
    """
    host = (identifier or {}).get("host")
    site = (identifier or {}).get("site")
    lang = (identifier or {}).get("lang")
    if not host or not site:
        raise ValueError("workday identifier missing required 'host' / 'site' keys")

    tenant = _tenant(host)
    list_url = f"https://{host}/wday/cxs/{tenant}/{urllib.parse.quote(site, safe='')}/jobs"

    out: list[dict[str, Any]] = []
    offset = 0
    pages = 0
    while pages < MAX_PAGES:
        page = _http_json(list_url, method="POST", body={
            "appliedFacets": {},
            "limit": PAGE_SIZE,
            "offset": offset,
            "searchText": "",
        })
        items = page.get("jobPostings") or []
        if not items:
            break
        for p in items:
            external_path = p.get("externalPath") or ""
            if not external_path:
                continue
            posted = p.get("startDate") or None
            if isinstance(posted, str) and len(posted) >= 10 and posted[4] == "-":
                posted = posted[:10]
            else:
                # `postedOn` is a human string ("Posted 30+ Days Ago") — not a
                # date. Drop it; the row's discovered_at takes over downstream.
                posted = None
            out.append({
                "url": _public_job_url(host, lang, site, external_path),
                "title": p.get("title"),
                # `description` deliberately omitted — caller pulls via fetch_description.
                "posted_at": posted,
                "location": p.get("locationsText") or "",
                # Private fields used by fetch_description below. Underscore-
                # prefixed so callers know they're adapter-private.
                "_host": host,
                "_site": site,
                "_external_path": external_path,
            })
        total = page.get("total") or 0
        offset += PAGE_SIZE
        pages += 1
        if offset >= total:
            break
    return out


def fetch_description(job: dict[str, Any]) -> str:
    """Fetch the JD body for a posting returned by `list_jobs`. Returns
    empty string on transient failure (the poller skips scoring if the
    description is too short)."""
    host = job.get("_host")
    site = job.get("_site")
    ext = job.get("_external_path")
    if not (host and site and ext):
        return ""
    try:
        detail = _http_json(_detail_url(host, site, ext), method="GET")
    except Exception:
        return ""
    info = detail.get("jobPostingInfo") or {}
    return _strip_html(info.get("jobDescription") or "")
