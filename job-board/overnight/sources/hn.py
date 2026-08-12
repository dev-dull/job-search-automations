"""Hacker News "Ask HN: Who is hiring?" via the official Algolia API.

Why this source: it is the highest signal-per-request available. One monthly
thread carries ~400-500 postings, skewed hard toward remote senior infrastructure
work at companies that do not advertise on job boards. No scraping, no robots
concerns — hn.algolia.com is a public API meant to be queried.

Two conventions of the thread are load-bearing:

  * Only TOP-LEVEL comments are job postings. Replies are discussion. Algolia
    exposes `parent_id`, so `parent_id == story_id` is the filter. Without it
    roughly half of what you collect is people arguing about compensation.

  * The de-facto format is `Company | Role | Location | REMOTE | ...` on the
    first line, then prose. It is a convention, not a schema — plenty of posts
    ignore it — so parsing is best-effort and the model does the real work.
"""

from __future__ import annotations

import json
import re
import urllib.parse

from . import polite_get, posting, strip_html

ALGOLIA = "https://hn.algolia.com/api/v1"
_THREAD_RE = re.compile(r"^Ask HN: Who is hiring\?", re.I)
_URL_RE = re.compile(r'https?://[^\s<>"\')]+')

# Links that are never the job posting itself.
_SKIP_HOSTS = (
    "news.ycombinator.com", "twitter.com", "x.com", "linkedin.com",
    "youtube.com", "github.com/orgs", "calendly.com",
)
# Prefer a real ATS link when the comment offers several.
_ATS_HINTS = (
    "greenhouse.io", "ashbyhq.com", "lever.co", "myworkdayjobs.com",
    "taleo.net", "rippling.com", "icims.com", "jobs.", "careers.",
    "/careers", "/jobs",
)


def _get(url: str, timeout: int = 30):
    return json.loads(polite_get(url, timeout=timeout))


def latest_thread() -> dict | None:
    """Most recent monthly hiring thread. Excludes the freelancer/wants-to-be-hired variants."""
    q = urllib.parse.quote('"Ask HN: Who is hiring"')
    data = _get(f"{ALGOLIA}/search_by_date?query={q}&tags=story&hitsPerPage=20")
    for hit in data.get("hits", []):
        title = hit.get("title") or ""
        if _THREAD_RE.match(title) and "freelance" not in title.lower():
            return hit
    return None


def _best_url(text: str) -> str | None:
    urls = [u.rstrip(".,);") for u in _URL_RE.findall(text)]
    urls = [u for u in urls if not any(h in u for h in _SKIP_HOSTS)]
    if not urls:
        return None
    for u in urls:
        if any(h in u.lower() for h in _ATS_HINTS):
            return u
    return urls[0]


def _company_and_title(first_line: str) -> tuple[str, str]:
    """Parse the `Company | Role | Location` convention, tolerantly."""
    parts = [p.strip() for p in re.split(r"\s*[|•·]\s*|\s+-\s+", first_line) if p.strip()]
    if not parts:
        return "", ""
    company = parts[0]
    # The role is the next part that looks like a job title rather than a
    # location or a REMOTE tag.
    noise = re.compile(
        r"^(remote|onsite|hybrid|full[- ]?time|part[- ]?time|contract|visa|"
        r"\$|us|usa|eu|uk|anywhere|worldwide)\b", re.I)
    title = next((p for p in parts[1:] if not noise.match(p)), "")
    return company[:80], title[:120]


# Not every top-level comment is a job. The thread also collects book
# recommendations, meta-discussion, and posts whose first line is a location or
# a salary, which parse into nonsense like title="70k" or title="New York, NY".
# Sending those to the LLM stages wastes the expensive model on noise.
_HIRING_HINT = re.compile(
    r"\b(hiring|we[''`]?re looking|join us|apply|role|position|engineer|"
    r"developer|opening|full[- ]?time|remote)\b", re.I)
_NOT_A_TITLE = re.compile(
    r"^(\$?\d[\d,.kK+\s-]*|remote|onsite|hybrid|anywhere|worldwide|"
    r"[a-z ]+,\s*[a-z]{2})$", re.I)


def _looks_like_posting(company: str, title: str, text: str) -> bool:
    if len(text) < 80:                      # too short to be a real posting
        return False
    if not _HIRING_HINT.search(text):       # reads like discussion, not a job
        return False
    if not title or len(title) < 3 or _NOT_A_TITLE.match(title.strip()):
        return False
    # A company name that is really a sentence fragment ("This book was one of…")
    if len(company.split()) > 6:
        return False
    return True


def collect(limit: int = 500) -> list[dict]:
    thread = latest_thread()
    if not thread:
        return []
    story_id = thread["objectID"]

    out: list[dict] = []
    page = 0
    while len(out) < limit:
        data = _get(
            f"{ALGOLIA}/search?tags=comment,story_{story_id}"
            f"&hitsPerPage=100&page={page}"
        )
        hits = data.get("hits", [])
        if not hits:
            break

        for h in hits:
            # Top-level comments only — replies are discussion, not postings.
            if str(h.get("parent_id")) != str(story_id):
                continue
            text = strip_html(h.get("comment_text"))
            if not text:
                continue
            lines = [ln for ln in text.splitlines() if ln.strip()]
            if not lines:
                continue

            company, title = _company_and_title(lines[0])
            url = _best_url(text)
            if not url or not company:
                continue
            if not _looks_like_posting(company, title, text):
                continue

            out.append(posting(
                url=url,
                title=title or lines[0][:120],
                company=company,
                description=text,
                source=f"hn:{thread.get('title', 'who-is-hiring')}",
                posted_at=(h.get("created_at") or "")[:10] or None,
            ))

        if page >= data.get("nbPages", 1) - 1:
            break
        page += 1

    return out[:limit]
