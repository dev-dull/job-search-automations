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

from . import MIN_DESCRIPTION_CHARS, polite_get, posting, strip_html

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


def collect(limit: int = 300, feeds: dict[str, str] | None = None,
            problems: list[str] | None = None) -> list[dict]:
    out: list[dict] = []
    for name, url in (feeds or FEEDS).items():
        try:
            root = ET.fromstring(_fetch(url))
        except Exception as err:
            # A dead feed must not take the whole night down — but it must
            # not be INVISIBLE either, or the funnel silently narrows to
            # HN-only. Record it where the morning report will show it.
            if problems is not None:
                problems.append(f"wwr feed {name!r}: {type(err).__name__}: {err}")
            continue

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
            jd = strip_html(_text(item, "description"))
            # Drop teasers on the JD ALONE, before the synthesized Region line
            # is added: 36 chars of metadata must not lift a thin JD over the
            # backend's scoring floor (which means "enough JD to be worth
            # paying for"). Measuring here rather than trusting the prescreen's
            # stage-1 length check is what keeps the padding from rescuing it.
            if len(jd) < MIN_DESCRIPTION_CHARS:
                continue
            body = f"Region: {region}\n\n{jd}" if region else jd

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
