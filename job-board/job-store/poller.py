"""Targeted-company poller.

For each `company_targets` row whose `ats_platform` has an adapter, fetch the
current list of openings, dedupe against existing `jobs.url`, filter titles
against the target's `deny_list`, and POST the survivors to `/jobs/score`
(without `fit_score`, so the backend's server-side Claude scorer runs).

CLI:

    .venv/bin/python poller.py                # poll all greenhouse targets
    .venv/bin/python poller.py --dry-run      # print actions, no POSTs
    .venv/bin/python poller.py --target 5     # poll only target id=5
    .venv/bin/python poller.py --backend http://127.0.0.1:5000

The poller is a pure HTTP client of job-store — it holds no DB access. It reads
targets (`/companies.json`), existing URLs (`/jobs/urls`), and location settings
(`/settings/locations`) over HTTP, POSTs survivors to `/jobs/score`, and stamps
`last_polled` via `/companies/<id>/polled`. This lets it run anywhere with
network reach to the backend (e.g. an out-of-cluster Kubernetes CronJob). The
backend URL comes from --backend or the JOB_STORE_URL env var.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

from adapters import ADAPTERS


DEFAULT_BACKEND = os.environ.get("JOB_STORE_URL", "http://127.0.0.1:5000")

# Persisted in the `settings` table; user overrides via --set-locations and
# --set-deny-locations. The denylist wins ties so that postings like
# "Sweden | Remote" or "Toronto, Canada" are rejected even though "Remote"
# is in the allowlist (a Grafana-style cross-region listing should only
# survive for the US variant).
DEFAULT_LOCATION_ALLOWLIST = [
    "United States", "USA", "U.S.",
    "US-",   # Workday URL-path format: US-CA-Santa-Clara
    "US,",   # Workday CXS list format: "US, CA, Santa Clara"
    "US |",  # Greenhouse pipe-delimited: "Title | US | Remote"
    "Americas", "North America",
    # Workday's CXS list endpoint collapses multi-region postings to strings
    # like "13 Locations" — we can't tell which of those are US without
    # fetching the detail page, so accept and let scoring sort it out.
    "Locations",
    # Note: "Remote" is intentionally NOT here. A posting like
    # "Sweden | Remote" should reject on Sweden via denylist, and a posting
    # like plain "Remote" falls through to allow via the no-match default.
]
DEFAULT_LOCATION_DENYLIST = [
    # Countries / regions seen in recent polls. Add new ones as they surface.
    "Canada", "Toronto", "Montreal", "Vancouver",
    "United Kingdom", " UK ", "| UK", "UK |", "London",
    "Ireland", "Dublin",
    "Germany", "Berlin", "Munich",
    "Spain", "Barcelona", "Madrid",
    "Sweden", "Stockholm",
    "Netherlands", "Amsterdam",
    "France", "Paris",
    "Israel", "Tel Aviv", "Yokneam",
    "India", "Bengaluru", "Pune", "Hyderabad", "Mumbai",
    "Taiwan", "Taipei", "China", "Japan", "Tokyo",
    "Australia", "Sydney",
    "LATAM", "EMEA", "APAC", "ASEAN", "DACH", "ANZ",
]


def _get_json(backend: str, path: str) -> dict:
    req = urllib.request.Request(f"{backend.rstrip('/')}{path}",
                                 headers={"accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def _post_json(backend: str, path: str, payload: dict) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(f"{backend.rstrip('/')}{path}", data=body,
                                 headers={"content-type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def _existing_urls(backend: str) -> set[str]:
    return set(_get_json(backend, "/jobs/urls").get("urls") or [])


def _is_denied(title: str, deny_list: list[str]) -> str | None:
    """Return the matching deny phrase, or None if the title passes."""
    t = (title or "").lower()
    for phrase in deny_list or []:
        if not phrase:
            continue
        if phrase.lower() in t:
            return phrase
    return None


def _csv_or_default(raw: str | None, default: list[str]) -> list[str]:
    """Split a stored CSV setting, or fall back to the built-in default when
    the backend reports the setting unset (null)."""
    if raw is None:
        return list(default)
    return [p.strip() for p in raw.split(",") if p.strip()]


def _load_location_lists(backend: str) -> tuple[list[str], list[str]]:
    """Allow/deny lists from GET /settings/locations, with defaults applied."""
    data = _get_json(backend, "/settings/locations")
    return (
        _csv_or_default(data.get("allowlist"), DEFAULT_LOCATION_ALLOWLIST),
        _csv_or_default(data.get("denylist"), DEFAULT_LOCATION_DENYLIST),
    )


def _location_allowed(loc: str, allowlist: list[str], denylist: list[str]) -> str | None:
    """Return None if allowed; otherwise return a human-readable reason.

    Semantics:
    - Empty location → allowed (don't reject on missing data).
    - Allowlist match → allowed (positive override beats denylist; lets a
      multi-region posting like "United States, Canada" survive even though
      "Canada" is in the denylist).
    - Otherwise denylist match → rejected.
    - Otherwise → allowed (permissive default: an unknown country slips
      through, which the user can patch via --set-deny-locations).
    """
    if not loc:
        return None
    low = loc.lower()
    for p in allowlist or []:
        if p and p.lower() in low:
            return None
    for p in denylist or []:
        if p and p.lower() in low:
            return f"deny:{p!r}"
    return None


def _post_score(backend: str, job: dict, target_name: str) -> tuple[bool, dict | str]:
    payload = {
        "url": job["url"],
        "title": job.get("title"),
        "description": job.get("description"),
        "posted_at": job.get("posted_at"),
        "ats_platform": job.get("ats_platform"),
        "discovered_by": f"poller:{target_name}",
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{backend.rstrip('/')}/jobs/score",
        data=body,
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        # Server-side scoring takes ~5-10s per JD on cache hits — generous timeout.
        with urllib.request.urlopen(req, timeout=120) as resp:
            return True, json.load(resp)
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:200]}"
    except urllib.error.URLError as e:
        return False, f"URL error: {e.reason}"


def _exit_code(summaries: list[dict]) -> int:
    """Exit 0 unless the run was *systemically* broken.

    A single misconfigured or flaky ATS target (e.g. one that 404s) must NOT
    fail the whole run: a non-zero exit makes Kubernetes mark the Job failed and
    retry the entire ~minute-long poll, and it drowns the CronJob's failure
    signal so a genuinely broken poller is indistinguishable from one bad target.
    Per-posting errors (short JD, a single score POST failing) are logged but
    non-fatal too.

    Fail only when every target that has an adapter failed at the adapter level
    (all ATS unreachable, network down, etc.). A dead backend already fails
    earlier, when the initial /companies.json and /jobs/urls reads raise.
    """
    attempted = sum(1 for s in summaries if not s.get("skipped"))
    adapter_failures = sum(1 for s in summaries if s.get("adapter_error"))
    return 1 if attempted and adapter_failures == attempted else 0


def poll_target(target: dict, *, backend: str, existing_urls: set[str],
                dry_run: bool, max_new: int | None,
                location_allowlist: list[str],
                location_denylist: list[str]) -> dict:
    name = target.get("name") or "unnamed"
    platform = target.get("ats_platform")
    adapter = ADAPTERS.get(platform)

    summary = {
        "id": target["id"],
        "name": name,
        "platform": platform,
        "found": 0,
        "new": 0,
        "denied": 0,
        "out_of_region": 0,
        "scored": 0,
        "errors": 0,
        "adapter_error": False,
        "skipped": 0,
        "stopped_at_seen": False,
        "max_new_reached": False,
        "error_detail": [],
    }

    if not adapter:
        # Not a transient error — the target was registered with an ATS the
        # poller doesn't know how to talk to. Surface it visibly but don't
        # treat it as a failure for exit-code purposes.
        print(f"  {name} ({platform}): no adapter for this ATS — skipping")
        summary["skipped"] = 1
        return summary

    identifier = target.get("ats_identifier_parsed") or {}
    deny_list = target.get("deny_list") or []

    try:
        jobs = adapter.list_jobs(identifier)
    except Exception as e:
        # Adapter-level failure: this whole target couldn't be fetched (bad
        # config, ATS down, 404). Flagged separately from per-posting errors so
        # one broken target doesn't fail the whole run (see _exit_code).
        summary["errors"] = 1
        summary["adapter_error"] = True
        summary["error_detail"].append(f"adapter error: {e}")
        return summary

    summary["found"] = len(jobs)
    print(f"  {name} ({platform}): found {len(jobs)} openings (newest-first)")

    for job in jobs:
        if not job.get("url") or not job.get("title"):
            continue
        # Stop-when-seen: adapters return newest-first, so the first time we
        # hit a URL we already have, every job below it is older and either
        # known or deny-listed — no point continuing.
        if job["url"] in existing_urls:
            summary["stopped_at_seen"] = True
            print(f"    stop [already seen] {job['title']}")
            break

        denied = _is_denied(job["title"], deny_list)
        if denied:
            summary["denied"] += 1
            print(f"    skip [deny: {denied!r}] {job['title']}")
            continue

        # Location check before the description fetch — skip cheaply on
        # geography before we spend any HTTP / Anthropic budget on a role
        # the user wouldn't take.
        loc = job.get("location") or ""
        reason = _location_allowed(loc, location_allowlist, location_denylist)
        if reason is not None:
            summary["out_of_region"] += 1
            print(f"    skip [location {reason} for {loc!r}] {job['title']}")
            continue

        if max_new is not None and summary["new"] >= max_new:
            summary["max_new_reached"] = True
            print(f"    stop [--max-new {max_new} reached] (more openings unreviewed)")
            break

        # Lazy description fetch — only for jobs that survived dedupe+deny.
        # For Greenhouse/Ashby/Lever this is a no-op (descriptions are inline);
        # for Workday it issues the per-posting detail HTTP call.
        if not job.get("description"):
            try:
                job["description"] = adapter.fetch_description(job)
            except Exception as e:
                summary["errors"] += 1
                summary["error_detail"].append(f"detail fetch failed for {job['title']}: {e}")
                print(f"    ERROR fetching detail for {job['title']}: {e}")
                continue
        if not job.get("description") or len(job["description"]) < 100:
            summary["errors"] += 1
            summary["error_detail"].append(f"{job['title']}: description too short to score")
            print(f"    skip [no description]   {job['title']}")
            continue

        summary["new"] += 1
        job["ats_platform"] = platform
        if dry_run:
            print(f"    dry-run [would POST]    {job['title']}  ({len(job['description'])}c)")
            continue
        ok, result = _post_score(backend, job, name)
        if ok:
            summary["scored"] += 1
            existing_urls.add(job["url"])
            fs = result.get("fit_score")
            print(f"    scored fit={fs}  {job['title']}")
        else:
            summary["errors"] += 1
            summary["error_detail"].append(f"{job['title']}: {result}")
            print(f"    ERROR  {job['title']}: {result}")

    if not dry_run:
        try:
            _post_json(backend, f"/companies/{target['id']}/polled",
                       {"last_polled_count": summary["found"]})
        except Exception as e:
            summary["error_detail"].append(f"failed to record last_polled: {e}")

    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Poll company_targets for new openings.")
    parser.add_argument("--backend", default=DEFAULT_BACKEND,
                        help=f"job-store base URL (default: {DEFAULT_BACKEND})")
    parser.add_argument("--target", type=int, default=None,
                        help="poll only this target id")
    parser.add_argument("--dry-run", action="store_true",
                        help="print actions, no POSTs (no scoring, no last_polled)")
    parser.add_argument("--max-new", type=int, default=None,
                        help="cap the number of new jobs scored per target this run "
                             "(useful for first-poll budget on big Workday tenants)")
    parser.add_argument("--locations", default=None,
                        help="comma-separated location allowlist override for this "
                             "run only (e.g. 'United States,Remote,US-')")
    parser.add_argument("--set-locations", default=None,
                        help="persist the comma-separated allowlist to settings and exit")
    parser.add_argument("--deny-locations", default=None,
                        help="comma-separated location denylist override for this run only")
    parser.add_argument("--set-deny-locations", default=None,
                        help="persist the comma-separated denylist to settings and exit")
    parser.add_argument("--show-locations", action="store_true",
                        help="print the currently-configured allow- and deny-lists and exit")
    args = parser.parse_args(argv)
    backend = args.backend

    # One-shot settings management — never polls.
    if args.set_locations is not None:
        _post_json(backend, "/settings/locations", {"allowlist": args.set_locations})
        print(f"location_allowlist set to: {args.set_locations!r}")
        return 0
    if args.set_deny_locations is not None:
        _post_json(backend, "/settings/locations", {"denylist": args.set_deny_locations})
        print(f"location_denylist set to: {args.set_deny_locations!r}")
        return 0
    if args.show_locations:
        allow, deny = _load_location_lists(backend)
        print(f"allowlist: {', '.join(allow)}")
        print(f"denylist:  {', '.join(deny)}")
        return 0

    targets = _get_json(backend, "/companies.json")
    if args.target is not None:
        targets = [t for t in targets if t["id"] == args.target]
        if not targets:
            print(f"no target with id={args.target}", file=sys.stderr)
            return 2

    if not targets:
        print("no company_targets configured.", file=sys.stderr)
        return 1

    existing = _existing_urls(backend)
    backend_allow, backend_deny = _load_location_lists(backend)
    location_allowlist = (
        [p.strip() for p in args.locations.split(",") if p.strip()]
        if args.locations else backend_allow
    )
    location_denylist = (
        [p.strip() for p in args.deny_locations.split(",") if p.strip()]
        if args.deny_locations else backend_deny
    )
    print(f"Polling {len(targets)} target(s)  (dry-run={args.dry_run}, backend={backend})")
    print(f"Existing jobs in DB: {len(existing)}")
    if location_allowlist:
        print(f"Location allowlist: {', '.join(location_allowlist)}")
    if location_denylist:
        print(f"Location denylist:  {', '.join(location_denylist)}")

    started = time.time()
    summaries = []
    for t in targets:
        summaries.append(poll_target(t, backend=args.backend,
                                     existing_urls=existing,
                                     dry_run=args.dry_run,
                                     max_new=args.max_new,
                                     location_allowlist=location_allowlist,
                                     location_denylist=location_denylist))

    elapsed = time.time() - started
    total_found = sum(s["found"] for s in summaries)
    total_new = sum(s["new"] for s in summaries)
    total_scored = sum(s["scored"] for s in summaries)
    total_denied = sum(s["denied"] for s in summaries)
    total_errors = sum(s["errors"] for s in summaries)
    total_skipped = sum(s["skipped"] for s in summaries)
    total_out_of_region = sum(s["out_of_region"] for s in summaries)

    print()
    print(f"Done in {elapsed:.1f}s.  found={total_found}  new={total_new}  "
          f"denied={total_denied}  out_of_region={total_out_of_region}  "
          f"scored={total_scored}  skipped={total_skipped}  errors={total_errors}")
    if total_errors:
        print("\nErrors:")
        for s in summaries:
            for msg in s["error_detail"]:
                print(f"  [{s['name']}] {msg}")

    code = _exit_code(summaries)
    if code:
        attempted = sum(1 for s in summaries if not s["skipped"])
        print(f"\nFAIL: all {attempted} target(s) with an adapter errored "
              f"(systemic failure).")
    elif total_errors:
        print("\n(partial errors above are non-fatal; the run succeeded)")
    return code


if __name__ == "__main__":
    sys.exit(main())
