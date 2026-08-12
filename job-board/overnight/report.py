"""Morning artifacts: a CSV of everything and a short markdown summary.

The rejection log is the point. A funnel you cannot audit is a funnel you cannot
tune — every drop records which stage killed it and why, so a morning skim
answers "what did it throw away, and was it right?" That question is what makes
the fit_floor and the gate wording improvable instead of superstition.
"""

from __future__ import annotations

import csv
import datetime as dt
from collections import Counter
from pathlib import Path

FIELDS = ["company", "title", "url", "keep", "stage", "reason", "fit_sketch", "gates"]


def write_reports(outdir: Path, kept, dropped, fetch_errors, *, submitted,
                  dry_run: bool, new_companies: Counter | None = None) -> None:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    outdir.mkdir(parents=True, exist_ok=True)

    csv_path = outdir / f"prescreen-{stamp}.csv"
    with csv_path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        for d in list(kept) + list(dropped):
            w.writerow(d.row())

    by_stage = Counter(d.stage for d in dropped)
    md = outdir / f"summary-{stamp}.md"
    lines = [
        f"# Overnight prescreen — {stamp}",
        "",
        f"- screened: **{len(kept) + len(dropped)}** unseen postings",
        f"- kept: **{len(kept)}**",
        f"- dropped: **{len(dropped)}**"
        + (f" ({', '.join(f'{k} {v}' for k, v in by_stage.most_common())})" if by_stage else ""),
        f"- submitted for paid scoring: **{len(submitted)}**"
        + ("  _(dry run — nothing was spent)_" if dry_run else ""),
        "",
    ]

    if submitted:
        lines += ["## Scored overnight", "",
                  "| rank | fit | company | title |", "|---|---|---|---|"]
        for row in sorted(submitted, key=lambda x: x.get("rank") or 0, reverse=True):
            lines.append(f"| {row.get('rank')} | {row.get('score')} | "
                         f"{row['company']} | {row['title']} |")
        lines.append("")

    if kept:
        lines += ["## Kept, in local fit order", "",
                  "| fit~ | company | title | link |", "|---|---|---|---|"]
        for d in kept:
            lines.append(
                f"| {d.detail.get('fit_sketch','—')} | {d.company} | {d.title} | [link]({d.url}) |"
            )
        lines.append("")

    # Gate drops are the ones most worth a human eye — a wrong gate silently
    # costs a real job, and the quoted evidence is what makes that checkable.
    gate_drops = [d for d in dropped if d.stage == "gates"]
    if gate_drops:
        lines += ["## Dropped on gates — check these for false positives", "",
                  "| company | title | gate | evidence |", "|---|---|---|---|"]
        for d in gate_drops[:40]:
            for g in d.detail.get("gate_failures", [])[:1]:
                ev = (g.get("evidence", "") or "").replace("|", "\\|")[:160]
                lines.append(f"| {d.company} | {d.title} | {g.get('gate')} | {ev} |")
        lines.append("")

    # Companies are never auto-added to the watch list — that stays a human
    # decision. This is the shortlist to decide from.
    if new_companies:
        lines += [
            "## Companies worth considering for the watch list", "",
            "_Not added automatically. These produced roles that survived the prescreen._", "",
            "| company | keeps |", "|---|---|",
        ]
        for name, n in new_companies.most_common(25):
            lines.append(f"| {name} | {n} |")
        lines.append("")

    problems = [e for e in fetch_errors if e]
    if problems:
        lines += ["## Problems", ""] + [f"- {e}" for e in problems] + [""]

    lines += [
        "---",
        "",
        f"Full log: `{csv_path.name}`. If the LLM host's storage is scratch, "
        "sync this directory somewhere durable.",
    ]
    md.write_text("\n".join(lines))
