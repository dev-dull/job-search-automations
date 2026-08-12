"""Discovery sources: where new postings come from.

A source is a callable returning a list of Posting dicts. Sources go OUT and
find roles at companies the watch list has never heard of — that is the whole
point of this module. Polling companies already on the watch list is the
poller's job and is deliberately not duplicated here.

Every source must:
  * use an official API or feed where one exists, rather than scraping HTML
  * respect robots.txt and any stated content signals
  * identify itself honestly in the User-Agent
  * be cheap enough to run nightly without being a burden on the host

Posting shape (matches what the job-store adapters emit, so the rest of the
pipeline does not care where a posting came from):

    {url, title, company, description, source, posted_at|None}

Sources deliberately NOT implemented
------------------------------------
RemoteOK: robots.txt disallows `/*?action=get_jobs` (the JSON jobs API) for all
user agents and individually blocks every major AI crawler. Checked 2026-08-02.
The site has said no; we take it at its word.
"""

from __future__ import annotations

import html
import os
import re

# Identify YOURSELF to the sites you fetch — an honest UA with a contact path
# is part of being a polite bot. Set it once via env; the default names the
# toolkit but not you.
USER_AGENT = os.environ.get(
    "DISCOVERY_USER_AGENT",
    "overnight-discovery/0.1 (self-hosted job search agent)")

# Minimum spacing between requests to the SAME host, applied by polite_get.
# Sources are called serially, so a module-level table is safe. The adapters
# path has its own politeness (fetch.PoliteFetcher); this covers the raw
# API/feed fetches the current sources make.
_MIN_DELAY_S = float(os.environ.get("SOURCE_MIN_DELAY_S", "2.0"))
_last_hit: dict[str, float] = {}


def polite_get(url: str, timeout: int = 30) -> bytes:
    """GET with an honest UA and a per-host minimum delay. All source fetches
    go through here so the README's rate-limiting guarantee is made by code,
    not by prose."""
    import time
    import urllib.parse
    import urllib.request
    host = urllib.parse.urlparse(url).netloc.lower()
    last = _last_hit.get(host)
    if last is not None:
        wait = _MIN_DELAY_S - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
    _last_hit[host] = time.monotonic()
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")
_BLANKS = re.compile(r"\n{3,}")


def strip_html(raw: str | None) -> str:
    """HTML fragment -> readable text, preserving paragraph breaks."""
    if not raw:
        return ""
    s = re.sub(r"(?i)<br\s*/?>", "\n", raw)
    s = re.sub(r"(?i)</p\s*>", "\n\n", s)
    s = _TAG.sub("", s)
    s = html.unescape(s)
    s = _WS.sub(" ", s)
    return _BLANKS.sub("\n\n", s).strip()


def posting(url, title, company, description, source, posted_at=None) -> dict:
    return {
        "url": url,
        "title": (title or "").strip(),
        "company": (company or "").strip(),
        "description": description or "",
        "source": source,
        "posted_at": posted_at,
    }


from . import hn, wwr  # noqa: E402

SOURCES = {"hn": hn, "wwr": wwr}

__all__ = ["SOURCES", "USER_AGENT", "polite_get", "posting", "strip_html"]
