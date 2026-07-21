#!/usr/bin/env python3
"""Backtest the ranking formula against actual application decisions.

The DB's applied rows are the ground truth for what the board SHOULD have
surfaced: for each of them, compute where it would rank today among the open
rows, under both the legacy formula (0.5/0.5 blend, fit-alone when desirability
is missing, no clamp) and the current one. The metric to drive up is the top-5
hit rate — "of the roles actually applied to, how many would sit in the board's
top 5?" (see #72).

Read-only; no Anthropic calls. Run any time:

    python3 backtest_rank.py            # table + hit rates
    python3 backtest_rank.py --top 10   # count hits within a different cutoff

Caveat: rows scored before the gated schema have no gate/desirability data, so
the current formula treats them as pending. Re-score them (rescore.py
--gate-backfill) for a faithful comparison.
"""

from __future__ import annotations

import argparse

import db
import ranking


def _legacy_rank(job, stats):
    """The pre-#72 formula: 0.5/0.5 blend, fit-alone on missing desirability,
    no gates, no clamp, no company adjustment."""
    fit = job.get("fit_score")
    if fit is None:
        return None
    p_cb, p_app, g_rate, g_applied = stats
    des = job.get("desirability_score")
    base = 0.5 * des + 0.5 * fit if des is not None else fit
    decay = (ranking.age_decay(job.get("posted_at")) if job.get("posted_at")
             else ranking.age_decay(job.get("discovered_at")))
    factor = (ranking.platform_factor(p_cb, p_app, g_rate)
              if g_applied >= ranking.MIN_OUTCOMES_FOR_PLATFORM_FACTOR else 1.0)
    return round(base * decay * factor, 2)


def _current_rank(job, stats):
    return ranking.compute_rank_score(
        job.get("fit_score"), job.get("posted_at"), stats,
        discovered_at=job.get("discovered_at"),
        desirability_score=job.get("desirability_score"),
        gated=bool(job.get("gated")),
        company=job.get("company"),
    )


def _position(rank, fit, ranked_open):
    """1-based board position this (rank, fit) would take among open rows."""
    return 1 + sum(1 for r, f in ranked_open
                   if r > rank or (r == rank and f > (fit or 0)))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--top", type=int, default=5,
                    help="cutoff for the hit-rate metric (default: 5)")
    args = ap.parse_args(argv)

    open_rows = db.list_jobs(statuses=["discovered", "ranked"], order="id DESC")
    applied = db.list_jobs(statuses=["applied"], order="applied_at DESC")
    if not applied:
        print("No applied rows to backtest against.")
        return 0

    cache = {}

    def stats_for(job):
        ats = job.get("ats_platform")
        if ats not in cache:
            cache[ats] = db.get_platform_stats(ats)
        return cache[ats]

    boards = {}
    for name, fn in (("legacy", _legacy_rank), ("current", _current_rank)):
        boards[name] = [(r, j.get("fit_score") or 0) for j in open_rows
                        if (r := fn(j, stats_for(j))) is not None]

    print(f"{len(applied)} applied row(s) vs {len(open_rows)} open row(s)\n")
    print(f"{'company':<24} {'title':<34} {'legacy':>12} {'current':>12}")
    hits = {"legacy": 0, "current": 0}
    pending = 0
    for j in applied:
        pos = {}
        for name, fn in (("legacy", _legacy_rank), ("current", _current_rank)):
            r = fn(j, stats_for(j))
            if r is None:
                pos[name] = "unscored"
                continue
            p = _position(r, j.get("fit_score"), boards[name])
            pos[name] = f"#{p} ({r})"
            if p <= args.top:
                hits[name] += 1
        if j.get("desirability_score") is None:
            pending += 1
        print(f"{str(j.get('company'))[:23]:<24} {str(j.get('title'))[:33]:<34} "
              f"{pos['legacy']:>12} {pos['current']:>12}")

    n = len(applied)
    print(f"\ntop-{args.top} hit rate:  legacy {hits['legacy']}/{n}   "
          f"current {hits['current']}/{n}")
    if pending:
        print(f"note: {pending} applied row(s) have no desirability/gate data "
              f"(scored pre-#72) — re-score them for a faithful comparison.")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
