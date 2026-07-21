"""Bulk-branch high-priority jobs.

For each open job that scores fit > 79 AND live rank > 99, create a branch
off the latest `origin/main` following the project's branch convention
(`companyName-jobID-YYYYMMDD`), write `job.txt` with the stored JD, commit,
and push. The push triggers the existing `process-resume.yaml` workflow.

Usage:
    .venv/bin/python branch_high_priority.py [--dry-run] [--fit-min N] [--rank-min N]
    .venv/bin/python branch_high_priority.py --limit 3       # process at most N
    .venv/bin/python branch_high_priority.py --resume-repo /path/to/repo

Run from the job-store directory (so `db.py` is importable). The resume repo
defaults to the parent directory.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import db
import ranking


SLUG_RE = re.compile(r"[^a-z0-9]+")


def slug(value: str) -> str:
    """Match the project's existing slug convention (see app.py:_slug)."""
    return SLUG_RE.sub("", (value or "").lower()) or "unknown"


def extract_job_id(url: str, ats_platform: str, row_id: int) -> str:
    """Best-effort ID to put in the branch name's suffix."""
    if not url:
        return str(row_id)
    try:
        u = urllib.parse.urlparse(url)
    except Exception:
        return str(row_id)

    if ats_platform == "greenhouse":
        params = urllib.parse.parse_qs(u.query)
        if "gh_jid" in params:
            return params["gh_jid"][0]
        m = re.search(r"/jobs/(\d+)", u.path)
        if m:
            return m.group(1)

    if ats_platform == "workday":
        # Trailing _JR12345, _R12345, or just _12345 segment of the path
        m = re.search(r"_([A-Z]*\d+)$", u.path)
        if m:
            return m.group(1)

    if ats_platform in ("ashby", "lever"):
        segs = [s for s in u.path.split("/") if s]
        if segs:
            # UUID at end — use the first stanza for a shorter branch name.
            return segs[-1].split("-")[0]

    return str(row_id)


def branch_name(company: str, url: str, ats_platform: str, row_id: int,
                today: str) -> str:
    return f"{slug(company)}-{extract_job_id(url, ats_platform, row_id)}-{today}"


def git(args: list[str], *, cwd: Path, check: bool = True,
        capture: bool = False) -> subprocess.CompletedProcess:
    cmd = ["git", *args]
    return subprocess.run(
        cmd, cwd=cwd, check=check,
        capture_output=capture, text=True,
    )


def remote_branch_exists(repo: Path, branch: str) -> bool:
    """True if `branch` exists on origin. Doesn't fetch — caller refreshes."""
    result = git(["ls-remote", "--exit-code", "--heads", "origin", branch],
                 cwd=repo, check=False, capture=True)
    return result.returncode == 0


def local_branch_exists(repo: Path, branch: str) -> bool:
    result = git(["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
                 cwd=repo, check=False, capture=True)
    return result.returncode == 0


def build_job_txt(row: dict) -> str:
    header = (
        f"Title: {row.get('title') or ''}\n"
        f"Company: {row.get('company') or ''}\n"
        f"URL: {row.get('url') or ''}\n"
        f"ATS: {row.get('ats_platform') or 'unknown'}\n"
        f"Posted: {row.get('posted_at') or ''}\n"
        f"Discovered: {row.get('discovered_at') or ''}\n"
        f"\n---\n\n"
    )
    return header + (row.get("description") or "").strip() + "\n"


def list_candidates(*, fit_min: float, rank_min: float) -> list[tuple[float, dict]]:
    with db.cursor() as conn:
        rows = conn.execute(
            """
            SELECT id, company, title, fit_score, desirability_score, gated,
                   posted_at, discovered_at,
                   ats_platform, url, description, branch, status
            FROM jobs
            WHERE status IN ('discovered','ranked')
              AND fit_score IS NOT NULL
              AND fit_score > ?
            """,
            (fit_min,),
        ).fetchall()

    platform_cache: dict[str | None, tuple] = {}
    out: list[tuple[float, dict]] = []
    for r in rows:
        d = dict(r)
        ats = d.get("ats_platform")
        if ats not in platform_cache:
            platform_cache[ats] = db.get_platform_stats(ats)
        rank = ranking.compute_rank_score(
            d.get("fit_score"), d.get("posted_at"), platform_cache[ats],
            discovered_at=d.get("discovered_at"),
            desirability_score=d.get("desirability_score"),
            gated=bool(d.get("gated")),
            company=d.get("company"),
        )
        if rank is None or rank <= rank_min:
            continue
        out.append((rank, d))
    out.sort(key=lambda t: -t[0])
    return out


def process_one(row: dict, *, repo: Path, dry_run: bool, today: str) -> dict:
    name = branch_name(row["company"], row["url"], row.get("ats_platform"),
                       row["id"], today)
    summary = {
        "id": row["id"],
        "company": row["company"],
        "title": row["title"],
        "branch": name,
        "action": "pending",
        "detail": "",
    }

    if not (row.get("description") or "").strip():
        summary["action"] = "skipped"
        summary["detail"] = "no description text in DB"
        return summary

    if local_branch_exists(repo, name) or remote_branch_exists(repo, name):
        summary["action"] = "skipped"
        summary["detail"] = "branch already exists"
        return summary

    if row.get("branch"):
        summary["action"] = "skipped"
        summary["detail"] = f"row already tracks branch={row['branch']!r}"
        return summary

    if dry_run:
        summary["action"] = "would-create"
        return summary

    # Create the branch off the freshly-fetched origin/main so this works
    # even if local main is behind.
    try:
        git(["checkout", "-b", name, "origin/main"], cwd=repo, capture=True)
    except subprocess.CalledProcessError as e:
        summary["action"] = "error"
        summary["detail"] = f"checkout failed: {e.stderr or e}"
        return summary

    try:
        (repo / "job.txt").write_text(build_job_txt(row), encoding="utf-8")
        git(["add", "job.txt"], cwd=repo, capture=True)
        msg = (
            f"Add {row.get('company','?')} job description ({row.get('title','?')})\n\n"
            f"Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>\n"
        )
        git(["commit", "-m", msg], cwd=repo, capture=True)
        git(["push", "-u", "origin", name], cwd=repo, capture=True)
        # Record the branch name back on the row so we don't double-create on
        # a future run. Keeps status as-is — caller can use the UI's "Mark
        # applied" button to transition state.
        db.update_status  # no-op; just a sanity import check
        with db.cursor() as conn:
            conn.execute("UPDATE jobs SET branch = ? WHERE id = ?",
                         (name, row["id"]))
        summary["action"] = "created"
    except subprocess.CalledProcessError as e:
        summary["action"] = "error"
        summary["detail"] = f"git op failed: {e.stderr or e}"
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would happen; no git ops, no DB writes")
    parser.add_argument("--fit-min", type=float, default=79.0,
                        help="minimum fit_score (default: 79)")
    parser.add_argument("--rank-min", type=float, default=99.0,
                        help="minimum live rank_score (default: 99)")
    parser.add_argument("--limit", type=int, default=None,
                        help="process at most N candidates")
    parser.add_argument("--resume-repo",
                        default=os.environ.get("RESUME_REPO"),
                        help="path to the resume repo. Required: pass --resume-repo "
                             "or set RESUME_REPO env var (e.g. ~/wip/resume).")
    args = parser.parse_args(argv)

    if not args.resume_repo:
        print("--resume-repo / RESUME_REPO is required.", file=sys.stderr)
        return 2
    repo = Path(args.resume_repo).expanduser().resolve()
    if not (repo / ".git").exists():
        print(f"resume repo not found: {repo}", file=sys.stderr)
        return 2

    # Make sure origin/main is current — branches need to be cut off the
    # latest commit, not a stale local copy.
    if not args.dry_run:
        print(f"Fetching origin/main in {repo} …")
        git(["fetch", "origin", "main"], cwd=repo, capture=True)

    cands = list_candidates(fit_min=args.fit_min, rank_min=args.rank_min)
    if args.limit:
        cands = cands[:args.limit]
    print(f"{len(cands)} candidates (fit > {args.fit_min}, live rank > {args.rank_min}):")
    print()

    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    starting_branch = git(["rev-parse", "--abbrev-ref", "HEAD"],
                          cwd=repo, capture=True).stdout.strip()

    summaries = []
    for rank, row in cands:
        print(f"-- id={row['id']} rank={rank:.1f} fit={row['fit_score']:.0f}"
              f"  {row['company']}  |  {row['title']}")
        s = process_one(row, repo=repo, dry_run=args.dry_run, today=today)
        summaries.append(s)
        print(f"   → {s['action']}: {s['branch']}{(' — ' + s['detail']) if s['detail'] else ''}")

    if not args.dry_run:
        # Return to the branch the user started on so we don't leave them on
        # the last newly-created branch.
        try:
            git(["checkout", starting_branch], cwd=repo, capture=True)
            print(f"\nReturned to {starting_branch}.")
        except subprocess.CalledProcessError as e:
            print(f"\nWarning: failed to return to {starting_branch}: {e}",
                  file=sys.stderr)

    print()
    print("Summary:")
    counts: dict[str, int] = {}
    for s in summaries:
        counts[s["action"]] = counts.get(s["action"], 0) + 1
    for action, n in sorted(counts.items()):
        print(f"  {action}: {n}")
    errors = [s for s in summaries if s["action"] == "error"]
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
