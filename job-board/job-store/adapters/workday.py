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
import urllib.error
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


def parse_public_url(url: str) -> tuple[str, str, str]:
    """(host, site, external_path) from a public Workday posting URL.

    Public URLs look like `https://<host>/<lang?>/<site>/job/<...>`; the site is
    the segment immediately before "job" (lang is optional), so we anchor on the
    "job" segment rather than assuming a fixed position."""
    parts = urllib.parse.urlsplit(url)
    segments = [s for s in parts.path.split("/") if s]
    try:
        ji = segments.index("job")
    except ValueError as exc:
        raise ValueError(f"no '/job/' segment in URL: {url}") from exc
    if ji == 0:
        raise ValueError(f"no site segment before '/job/' in URL: {url}")
    return parts.netloc, segments[ji - 1], "/" + "/".join(segments[ji:])


def posting_dead(public_url: str) -> bool | None:
    """Liveness of a single posting via the CXS detail endpoint.

    The public job page is an SPA shell that returns HTTP 200 even for removed
    postings (issue #65), so dead-link checks must ask the JSON API instead:
    200 with jobPostingInfo = alive, 403 (errorCode S22) or 404 = removed —
    behavior confirmed against live tenants (June + July recon). Returns None
    when undeterminable (unparseable URL, network noise); callers treat None
    as alive."""
    try:
        host, site, ext = parse_public_url(public_url)
    except ValueError:
        return None
    try:
        detail = _http_json(_detail_url(host, site, ext), method="GET")
    except urllib.error.HTTPError as e:
        return True if e.code in (403, 404) else None
    except Exception:
        return None
    if isinstance(detail, dict) and detail.get("jobPostingInfo"):
        return False
    return None


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


# ---------------------------------------------------------------------------
# Probe-at-create: resolve + VERIFY an identifier from any pasted Workday URL.
#
# The naive parse ("first path segment = lang, second = site") is wrong for
# job-detail URLs: /<site>/job parses as lang=<site>, site='job', which the
# backend used to save as-is — producing a target that 404s on every poll
# (the June poller incident). These helpers generate both plausible parses and
# check them against the live CXS jobs endpoint before anything is saved.
# ---------------------------------------------------------------------------

# Locale segments look like "en", "en-US", "fr-CA" — used only to ORDER the
# candidates (which parse to try first), never to hard-reject a shape.
_LOCALE_RE = re.compile(r"^[a-z]{2}(?:-[A-Z]{2})?$")

HOST_RE = re.compile(r"([a-z0-9-]+\.[a-z0-9-]+\.myworkdayjobs\.com)")


def site_candidates(host: str, segments: list[str]) -> list[dict[str, Any]]:
    """Ordered candidate identifiers for a Workday URL path.

    Two site URL shapes exist (see app.py's detector): /<lang>/<site> and
    /<site>. Both are generated from the first one or two path segments; a
    locale-looking first segment just decides which to try first."""
    out: list[dict[str, Any]] = []

    def add(c):
        if c not in out:
            out.append(c)

    if len(segments) >= 2:
        two = {"host": host, "lang": segments[0], "site": segments[1]}
        one = {"host": host, "site": segments[0]}
        if _LOCALE_RE.match(segments[0]):
            add(two)
            add(one)
        else:
            add(one)
            add(two)
    elif len(segments) == 1:
        add({"host": host, "site": segments[0]})
    return out


def verify_site(identifier: dict[str, Any]) -> bool:
    """True if the identifier resolves to a live CXS jobs endpoint (cheap
    limit-1 list call). A misparsed site 404s here instead of after save."""
    host = (identifier or {}).get("host")
    site = (identifier or {}).get("site")
    if not host or not site:
        return False
    tenant = _tenant(host)
    url = f"https://{host}/wday/cxs/{tenant}/{urllib.parse.quote(site, safe='')}/jobs"
    try:
        page = _http_json(url, method="POST", body={
            "appliedFacets": {}, "limit": 1, "offset": 0, "searchText": "",
        })
    except Exception:
        return False
    return isinstance(page.get("jobPostings"), list)


def _root_landing_path(host: str, timeout: int = 8) -> str | None:
    """Path the tenant root redirects to (usually /<lang>/<site>), for bare
    host URLs pasted with no path at all."""
    req = urllib.request.Request(
        f"https://{host}/",
        headers={"User-Agent": "Mozilla/5.0 (compatible; job-store/0.1)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return urllib.parse.urlsplit(resp.geturl()).path
    except Exception:
        return None


def resolve_identifier(careers_url: str, *, verify=None,
                       fetch_landing_path=None) -> dict[str, Any] | None:
    """Resolve a VERIFIED {host, lang?, site} identifier from any pasted
    Workday URL (careers page, job-detail link, or bare tenant host).
    Returns None when the URL isn't Workday or no candidate site verifies.

    `verify` / `fetch_landing_path` are injectable for tests; at most
    len(candidates) (≤2) verification calls plus one optional root fetch."""
    verify = verify or verify_site
    fetch_landing_path = fetch_landing_path or _root_landing_path
    m = HOST_RE.search(careers_url or "")
    if not m:
        return None
    host = m.group(1)
    path = urllib.parse.urlsplit(careers_url).path
    segments = [s for s in path.split("/") if s]
    if not segments:
        landing = fetch_landing_path(host)
        segments = [s for s in (landing or "").split("/") if s]
    for cand in site_candidates(host, segments):
        if verify(cand):
            return cand
    return None


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
    total = None  # latched from page 1; the CXS API only returns `total` on
    # the first page (subsequent pages report total=0), so we must not
    # overwrite it or the loop terminates after offset=20.
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
        if total is None:
            total = page.get("total") or 0
        offset += PAGE_SIZE
        pages += 1
        if total and offset >= total:
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
