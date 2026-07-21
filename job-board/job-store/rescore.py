#!/usr/bin/env python3
"""Bounded re-score of the highest-fit open jobs to add the desirability axis.

Rows scored before preferences existed have no desirability_score, so they rank
on fit alone. Rather than re-score the whole inbox (real Anthropic spend), this
re-scores the top-N open rows by fit — the ones you're most likely to act on —
with PREFERENCES_PATH now in the prompt, and updates fit/desirability/analysis/
rank. Run it again with a higher --limit to go deeper.

Usage:
    python3 rescore.py --limit 50            # re-score the top 50
    python3 rescore.py --limit 50 --dry-run  # preview, no Anthropic calls

Co-located with the DB (like branch_high_priority.py / csv_import.py). Needs the
same env as the backend: ANTHROPIC_API_KEY, RESUME_PATH, and — for the
desirability axis — PREFERENCES_PATH.
"""

from __future__ import annotations

import argparse
import json
import sys

import db
import ranking
from anthropic_client import score_job, read_preferences


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=50,
                    help="max rows to re-score (bounds Anthropic spend)")
    ap.add_argument("--dry-run", action="store_true",
                    help="list what would be re-scored, no Anthropic calls")
    args = ap.parse_args(argv)

    if not read_preferences():
        print("warning: PREFERENCES_PATH is unset/empty — re-scoring will only "
              "refresh fit, not add a desirability score.", file=sys.stderr)

    rows = db.top_open_missing_desirability(args.limit)
    print(f"{len(rows)} open row(s) without a desirability score"
          f"{' [dry-run]' if args.dry_run else ''}\n")

    done = failed = skipped = 0
    for r in rows:
        if not r["description"] or len(r["description"]) < 100:
            skipped += 1
            print(f"  skip id={r['id']} (no stored description to re-score)")
            continue
        if args.dry_run:
            print(f"  [dry-run] would re-score id={r['id']}")
            continue
        try:
            analysis = score_job(description=r["description"], url=r["url"],
                                  ats_platform=r["ats_platform"])["fit"]
        except Exception as e:
            failed += 1
            print(f"  ! id={r['id']} scoring failed: {e}")
            continue
        fit = analysis.get("candidate_score")
        des = analysis.get("desirability_score")
        gated = bool(analysis.get("gate_failures"))
        # url-keyed upsert; None fields are COALESCE'd (kept) so we only touch
        # the scored fields.
        db.upsert_job(url=r["url"], company=None, title=None, description=None,
                      ats_platform=None, posted_at=None, discovered_by=None,
                      fit_score=fit, analysis_json=json.dumps(analysis),
                      desirability_score=des, gated=1 if gated else 0)
        rank = ranking.compute_rank_score(
            fit, r["posted_at"], db.get_platform_stats(r["ats_platform"]),
            discovered_at=r["discovered_at"], desirability_score=des,
            gated=gated)
        db.update_rank_score(r["id"], rank)
        done += 1
        print(f"  ~ id={r['id']} fit={fit} desirability={des} rank={rank}")

    print(f"\n{'Would re-score' if args.dry_run else 'Re-scored'}: {done}"
          f"   skipped: {skipped}   failed: {failed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
