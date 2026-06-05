"""URL helpers shared between the Flask app and the storage layer.

Two distinct operations live here:

- `canonicalize_url(url)`: clickable URL, normalized — drops tracking params,
  strips trailing slash, and (for postings with `gh_jid`) keeps only that param.
  This is what gets stored as `jobs.url` and what the user clicks on.

- `compute_dedupe_key(url)`: opaque identifier shared by every URL form that
  points at the same posting. For Greenhouse this is `gh:<jid>` regardless of
  whether the URL points at the embed wrapper (e.g. `voxel51.com/jd?gh_jid=N`),
  the boards-api host (`boards.greenhouse.io/<board>/jobs/N`), or the newer
  `job-boards.greenhouse.io/...` form. Stored as `jobs.dedupe_key` and used as
  the lookup key for the backend's "already scored?" guard and the badge.
"""

from __future__ import annotations

import re
import urllib.parse


# Tracking / referrer params that don't identify the posting itself.
_TRACKING_PARAMS = {
    "gh_src", "utm_source", "utm_medium", "utm_campaign", "utm_term",
    "utm_content", "ref", "source", "lever-origin", "lever-source",
}

# Path patterns that carry a Greenhouse posting id directly in the URL path
# rather than in a `gh_jid` query param.
_GREENHOUSE_PATH = re.compile(r"^/[^/]+/jobs/(\d+)/?$")
# Ashby direct-post URL: jobs.ashbyhq.com/<org>/<uuid>
_ASHBY_PATH = re.compile(
    r"^/[^/]+/([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})/?$",
    re.IGNORECASE,
)


def canonicalize_url(url):
    """Return a deduped, clickable form of `url`. See module docstring."""
    if not url or url.startswith("manual-paste"):
        return url
    try:
        u = urllib.parse.urlparse(url)
    except Exception:
        return url

    params = urllib.parse.parse_qs(u.query, keep_blank_values=False)
    gh_jid = params.get("gh_jid", [None])[0]
    if gh_jid:
        path = u.path.rstrip("/") or "/"
        return f"{u.scheme}://{u.netloc}{path}?gh_jid={gh_jid}"

    kept = [(k, v) for k, v in urllib.parse.parse_qsl(u.query, keep_blank_values=False)
            if k not in _TRACKING_PARAMS]
    new_query = urllib.parse.urlencode(kept)
    path = u.path
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return urllib.parse.urlunparse((u.scheme, u.netloc, path, "", new_query, ""))


def compute_dedupe_key(url):
    """Return an opaque identifier that's the same across every URL form
    pointing at one posting. See module docstring."""
    if not url or url.startswith("manual-paste"):
        return url
    try:
        u = urllib.parse.urlparse(url)
    except Exception:
        return url

    # Greenhouse posting id wins over everything: it identifies the role
    # regardless of which host serves the JD page.
    params = urllib.parse.parse_qs(u.query, keep_blank_values=False)
    gh_jid = params.get("gh_jid", [None])[0]
    if not gh_jid and "greenhouse.io" in (u.netloc or ""):
        m = _GREENHOUSE_PATH.match(u.path or "")
        if m:
            gh_jid = m.group(1)
    if gh_jid:
        return f"gh:{gh_jid}"

    # Ashby: same idea. `ashby_jid` query param (embed wrapper on any host,
    # e.g. www.ashbyhq.com/careers?ashby_jid=<uuid>) or UUID-on-path on
    # jobs.ashbyhq.com.
    ashby_jid = params.get("ashby_jid", [None])[0]
    if not ashby_jid and (u.netloc or "") == "jobs.ashbyhq.com":
        m = _ASHBY_PATH.match(u.path or "")
        if m:
            ashby_jid = m.group(1)
    if ashby_jid:
        # First UUID stanza is unique within Ashby; shorter key.
        return f"ashby:{ashby_jid.split('-')[0]}"

    # Fall through to canonical URL for non-Greenhouse/Ashby postings. Two
    # URLs that differ only in tracking params / trailing slash converge here.
    return canonicalize_url(url)
