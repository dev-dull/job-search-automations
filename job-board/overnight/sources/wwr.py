"""WeWorkRemotely via its official category RSS feeds.

robots.txt checked 2026-08-02: `User-agent: *` / `Allow: /`, with only admin and
account paths disallowed. The RSS feeds are published for consumption, so we use
those rather than parsing listing HTML.

Feed titles follow `Company: Role`, and the description carries the full JD as
an HTML fragment — enough to prescreen without fetching the posting page, which
keeps us to one request per feed per night.

Note the feeds are noisy: the DevOps/Sysadmin category reliably contains sales
and support roles. That is expected and is what the rules stage is for.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from . import polite_get, posting, strip_html

BASE = "https://weworkremotely.com/categories"

# Categories worth reading for infrastructure work. WWR has no
# platform/infra category, so devops is the primary and programming is the
# wider net that occasionally carries platform roles.
FEEDS = {
    "devops": f"{BASE}/remote-devops-sysadmin-jobs.rss",
    "programming": f"{BASE}/remote-programming-jobs.rss",
}


def _fetch(url: str, timeout: int = 45) -> bytes:
    return polite_get(url, timeout=timeout)


def _text(item: ET.Element, tag: str) -> str:
    el = item.find(tag)
    return (el.text or "").strip() if el is not None and el.text else ""


def collect(limit: int = 300, feeds: dict[str, str] | None = None) -> list[dict]:
    out: list[dict] = []
    for name, url in (feeds or FEEDS).items():
        try:
            root = ET.fromstring(_fetch(url))
        except Exception:
            continue  # a dead feed must not take the whole night down

        for item in root.findall(".//item"):
            raw_title = _text(item, "title")
            link = _text(item, "link")
            if not link:
                continue

            # "Company: Role" — split on the first colon only.
            if ":" in raw_title:
                company, _, title = raw_title.partition(":")
            else:
                company, title = "", raw_title

            region = _text(item, "region")
            body = strip_html(_text(item, "description"))
            if region:
                body = f"Region: {region}\n\n{body}"

            out.append(posting(
                url=link,
                title=title.strip() or raw_title,
                company=company.strip(),
                description=body,
                source=f"wwr:{name}",
                posted_at=None,
            ))
            if len(out) >= limit:
                return out
    return out
