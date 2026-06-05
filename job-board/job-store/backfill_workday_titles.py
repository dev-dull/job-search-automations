#!/usr/bin/env python3
"""One-shot backfill for mis-titled Workday rows (issue #42).

The firefox-plugin used to store the static Workday site banner ("CAREERS AT
NVIDIA") as the job title for every Workday posting it discovered, because its
title-extraction selector chain had no Workday-specific selector and fell
through to `document.title`. The poller was never affected — it reads the title
straight from the Workday CXS API.

This script repairs the already-stored rows by re-fetching the authoritative
title from the same CXS detail endpoint the poller's adapter uses
(`jobPostingInfo.title`). It makes no Anthropic calls — only free Workday API
requests — and is idempotent: it updates a row only when the fetched title
differs from what's stored, so re-running it is a no-op.

Usage:
    python3 backfill_workday_titles.py --dry-run     # preview, no writes
    python3 backfill_workday_titles.py               # repair plugin-discovered rows
    python3 backfill_workday_titles.py --all         # check every Workday row
    python3 backfill_workday_titles.py --db /path/to/jobs.db
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from urllib.parse import urlsplit

import db
from adapters.workday import _detail_url, _http_json

_WS = re.compile(r"\s+")


def _looks_generic(title: str) -> bool:
    """True if a stored title is a Workday site banner rather than a real job
    title — e.g. "CAREERS AT NVIDIA", "Intel Careers", "Careers at Red Hat",
    "Career Site". Used to keep the lossy slug fallback from overwriting rows
    where the plugin happened to capture a genuine title."""
    t = (title or "").strip()
    if not t:
        return True
    if re.search(r"\bcareers?\b", t, re.IGNORECASE):
        return True
    if t == t.upper() and len(t) < 40:  # short all-caps banner
        return True
    return False


def _strip_reqid(slug: str) -> str:
    """Drop the trailing "_<reqid>" Workday appends to slugs. The reqid is the
    final underscore-delimited token and always contains a digit (e.g. JR2017916,
    R-056651, 26WD95712-1). Anchoring on "last underscore + has-a-digit" handles
    hyphenated reqids that a simpler regex misses."""
    head, sep, tail = slug.rpartition("_")
    if sep and any(c.isdigit() for c in tail):
        return head
    return slug


def _parse_public_url(url: str) -> tuple[str, str, str]:
    """Pull (host, site, external_path) out of a stored public Workday URL.

    Public URLs look like:
        https://<host>/<lang?>/<site>/job/<slug>_<reqid>
    The site is the path segment immediately before "job"; the external path is
    "/job/..." onward. `lang` is optional (some tenants omit it), so we anchor
    on the "job" segment rather than assuming a fixed position.
    """
    parts = urlsplit(url)
    segments = [s for s in parts.path.split("/") if s]
    try:
        ji = segments.index("job")
    except ValueError as exc:
        raise ValueError(f"no '/job/' segment in URL: {url}") from exc
    if ji == 0:
        raise ValueError(f"no site segment before '/job/' in URL: {url}")
    site = segments[ji - 1]
    external_path = "/" + "/".join(segments[ji:])
    return parts.netloc, site, external_path


def _fetch_title(url: str) -> str:
    """Authoritative title from the Workday CXS detail endpoint, or "" on failure."""
    host, site, external_path = _parse_public_url(url)
    detail = _http_json(_detail_url(host, site, external_path), method="GET")
    info = detail.get("jobPostingInfo") or {}
    return (info.get("title") or "").strip()


def _slug_title(url: str) -> str:
    """Approximate title from the URL slug, for postings the API can't resolve
    (closed/unpublished reqs return 403/404). Lossy: Workday slugifies every
    run of punctuation to hyphens, so we can only guess the original separators
    ("---" was likely " - ", "--" likely ", "). Readable, not authoritative."""
    _, _, external_path = _parse_public_url(url)
    slug = external_path.rstrip("/").rsplit("/", 1)[-1]   # last path segment
    slug = _strip_reqid(slug)                              # drop trailing "_JR…"
    # Map each maximal hyphen run by length in a single pass (so a separator we
    # insert isn't re-processed by a later rule): "---"+ was likely " - ",
    # "--" likely ", ", a lone "-" a space.
    def _sep(m: "re.Match") -> str:
        n = len(m.group())
        return " - " if n >= 3 else (", " if n == 2 else " ")
    slug = re.sub(r"-+", _sep, slug)
    return _WS.sub(" ", slug).strip()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", help="path to jobs.db (default: db.DB_PATH)")
    ap.add_argument("--all", action="store_true",
                    help="check every Workday row, not just plugin-discovered ones")
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would change without writing")
    ap.add_argument("--slug-fallback", action="store_true",
                    help="for rows the API can't resolve (closed/removed reqs), "
                         "approximate the title from the URL slug (lossy)")
    args = ap.parse_args(argv)

    conn = sqlite3.connect(args.db or db.DB_PATH)
    conn.row_factory = sqlite3.Row

    where = "ats_platform = 'workday'"
    if not args.all:
        where += " AND discovered_by = 'plugin'"
    rows = conn.execute(
        f"SELECT id, url, title FROM jobs WHERE {where} ORDER BY id"
    ).fetchall()

    print(f"Examining {len(rows)} Workday row(s)"
          f"{'' if args.all else ' (plugin-discovered)'}"
          f"{' [dry-run]' if args.dry_run else ''}\n")

    updated = unchanged = failed = approximated = 0
    for r in rows:
        tag = ""
        try:
            new_title = _fetch_title(r["url"])
        except Exception as exc:  # network / parse / closed-req (403/404): try fallback
            if args.slug_fallback and _looks_generic(r["title"]):
                try:
                    new_title = _slug_title(r["url"])
                    tag = " (slug)"
                except Exception:
                    new_title = ""
            else:
                new_title = ""
            if not new_title:
                failed += 1
                print(f"  ! id={r['id']}: fetch failed ({exc})")
                continue
        if not new_title:
            failed += 1
            print(f"  ! id={r['id']}: empty title from API, skipped")
            continue
        if new_title == (r["title"] or ""):
            unchanged += 1
            continue
        updated += 1
        if tag:
            approximated += 1
        print(f"  ~ id={r['id']}: {r['title']!r} -> {new_title!r}{tag}")
        if not args.dry_run:
            conn.execute("UPDATE jobs SET title = ? WHERE id = ?", (new_title, r["id"]))

    if not args.dry_run:
        conn.commit()
    conn.close()

    print(f"\n{'Would update' if args.dry_run else 'Updated'}: {updated}"
          f"   unchanged: {unchanged}   failed: {failed}")
    if approximated:
        print(f"  of those, {approximated} are slug-approximated (lossy), not authoritative")
    elif failed and not args.slug_fallback:
        print(f"  {failed} unresolved (likely closed/removed reqs); "
              f"re-run with --slug-fallback to approximate their titles from the URL")
    return 0


if __name__ == "__main__":
    sys.exit(main())
