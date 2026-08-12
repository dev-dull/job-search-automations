#!/usr/bin/env python3
"""Overnight discovery: crawl for good roles at companies we don't watch yet.

    sources (HN, WWR) -> dedupe vs job-store's seen-set -> local prescreen (free)
      -> POST survivors for authoritative scoring (paid) -> CSV + morning report

This is deliberately NOT a poller. The poller already covers the watch list on a
CronJob; running it again here would spend money to learn nothing. Everything
below comes from outside that list — the point is to widen the funnel.

Companies are never added to the watch list. Discovered positions go onto the
board, and the morning report lists which companies produced them so a human can
decide who is worth watching.

Spend and politeness are bounded by construction:
  * nothing is POSTed without --submit; the default is a dry run
  * --max-submit caps paid calls per night (default 25)
  * the seen-set is fetched first and every known dedupe key is skipped
  * `force` is never sent, so a re-POST returns the cached analysis for free
  * sources use official APIs/feeds, honour robots, and are rate-limited

Usage:
    python3 discover.py --gates ~/private/gates.txt --out ~/reports
    python3 discover.py --gates ~/private/gates.txt --out ~/reports --submit
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

from fetch import compute_dedupe_key
from llm import LlamaSwap
from prescreen import Decision, Prescreener
from report import write_reports
from sources import SOURCES

# No baked-in defaults for endpoints: this is a public toolkit and the
# operator's job-store/LLM locations are theirs to supply (env or flag).
DEFAULT_LLM = "http://localhost:8080"


def http_json(url: str, payload: dict | None = None, timeout: int = 60):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data,
        headers={"content-type": "application/json"} if data else {},
        method="POST" if data else "GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode()
        return json.loads(body) if body.strip() else {}


def seen_keys(payload: dict) -> set[str]:
    """The seen-set from GET /jobs/urls. Prefers dedupe_keys; falls back to
    computing keys from urls for older backends (same behavior as the
    poller's _seen_keys — this is the spend guardrail, so no silent empty)."""
    keys = set(payload.get("dedupe_keys") or [])
    if not keys:
        keys = {compute_dedupe_key(u) for u in payload.get("urls") or []}
    return keys


def load_gates(path: str | None) -> str:
    if not path:
        raise SystemExit(
            "--gates is required.\n\n"
            "The prescreen cannot check deal-breakers without them, and job-store\n"
            "keeps preferences server-side with no endpoint to read them. Point\n"
            "--gates at a local file describing level band, location/timezone,\n"
            "excluded role families and comp floor. Never commit it."
        )
    p = Path(path).expanduser()
    if not p.is_file():
        raise SystemExit(f"--gates: no such file: {p}")
    return p.read_text()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backend", default=os.environ.get("JOB_STORE"),
                    help="job-store base URL (env JOB_STORE)")
    ap.add_argument("--llm", default=os.environ.get("LLAMA_SWAP", DEFAULT_LLM),
                    help="OpenAI-compatible local LLM base URL (env LLAMA_SWAP)")
    ap.add_argument("--gates", default=os.environ.get("GATES_PATH"))
    ap.add_argument("--out", required=True,
                    help="report dir — keep it somewhere durable, not scratch "
                         "storage on the LLM host")
    ap.add_argument("--triage-model", default=os.environ.get("TRIAGE_MODEL", "scorer"),
                    help="model alias for the cheap triage stage (env TRIAGE_MODEL)")
    ap.add_argument("--gate-model", default=os.environ.get("GATE_MODEL", "coder"),
                    help="model alias for the gates+fit stage (env GATE_MODEL)")
    ap.add_argument("--title-drops-extra",
                    default=os.environ.get("TITLE_DROPS_EXTRA"),
                    help="comma-separated words/phrases to drop on sight in "
                         "titles (env TITLE_DROPS_EXTRA) — your personal band/"
                         "family exclusions live here or in the gates file, "
                         "not in the code")
    ap.add_argument("--sources", default="hn,wwr",
                    help=f"comma-separated: {','.join(SOURCES)}")
    ap.add_argument("--submit", action="store_true",
                    help="actually POST survivors for paid scoring (default: dry run)")
    ap.add_argument("--max-submit", type=int, default=25)
    ap.add_argument("--max-per-source", type=int, default=400)
    # Match each model's -np slot count: scorer serves 8, coder serves 1.
    ap.add_argument("--triage-workers", type=int, default=8)
    ap.add_argument("--gate-workers", type=int, default=1)
    args = ap.parse_args(argv)

    outdir = Path(args.out).expanduser()
    outdir.mkdir(parents=True, exist_ok=True)
    gates_text = load_gates(args.gates)

    if not args.backend:
        ap.error("--backend (or env JOB_STORE) is required — your job-store URL")
    print(f"[*] backend {args.backend}")
    seen_resp = http_json(f"{args.backend}/jobs/urls")
    seen = seen_keys(seen_resp)
    print(f"[*] seen-set: {len(seen)} dedupe keys")
    if not seen:
        # Legitimate only on a brand-new board. On an established one this
        # means dedupe is broken and every survivor would be re-submitted —
        # the single most expensive failure this agent can have. Say so in
        # the report, loudly.
        errors_boot = ["seen-set is EMPTY — fine on a fresh board, otherwise "
                       "dedupe is broken and paid re-scoring is likely"]
        print(f"[!] {errors_boot[0]}")
    else:
        errors_boot = []

    try:
        resume = http_json(f"{args.backend}/resume")
    except urllib.error.HTTPError as err:
        if err.code == 404:
            # 404 is the backend's documented answer when RESUME_PATH is
            # unset server-side. Without a resume there is nothing to sketch
            # fit against — fail with instructions, not a traceback.
            print("[!] job-store has no resume configured (GET /resume -> 404). "
                  "Set RESUME_PATH on the backend; the prescreen needs the "
                  "resume text for fit sketches.", file=sys.stderr)
            return 2
        raise
    resume_text = resume if isinstance(resume, str) else json.dumps(resume)

    # Companies already watched: their roles are the poller's job, not ours.
    watched = http_json(f"{args.backend}/companies.json")
    if isinstance(watched, dict):
        watched = watched.get("companies", [])
    watched_names = {(c.get("name") or "").strip().lower() for c in watched}
    print(f"[*] {len(watched_names)} companies already watched (their roles are the poller's job)")

    # --- collect ---------------------------------------------------------
    raw: list[dict] = []
    errors: list[str] = list(errors_boot)
    for name in [s.strip() for s in args.sources.split(",") if s.strip()]:
        mod = SOURCES.get(name)
        if mod is None:
            errors.append(f"unknown source {name!r}")
            continue
        try:
            got = mod.collect(limit=args.max_per_source)
            raw.extend(got)
            print(f"[*] {name}: {len(got)} postings")
        except Exception as err:
            errors.append(f"{name}: {type(err).__name__}: {err}")
            print(f"[!] {name}: {type(err).__name__}: {err}")

    # --- dedupe ----------------------------------------------------------
    fresh, skipped_seen, skipped_watched, dupes = [], 0, 0, 0
    batch_keys: set[str] = set()
    for p in raw:
        key = compute_dedupe_key(p["url"])
        if key in seen:
            skipped_seen += 1
            continue
        if key in batch_keys:
            dupes += 1
            continue
        if p.get("company", "").strip().lower() in watched_names:
            skipped_watched += 1
            continue
        batch_keys.add(key)
        fresh.append(p)

    print(f"[*] {len(raw)} collected -> {len(fresh)} to screen "
          f"({skipped_seen} already known, {skipped_watched} already watched, {dupes} dupes)")
    if not fresh:
        write_reports(outdir, [], [], errors, submitted=[], dry_run=not args.submit)
        return 0

    # --- prescreen (free) -------------------------------------------------
    llm = LlamaSwap(args.llm)
    screener = Prescreener(llm, gates_text=gates_text, resume_text=resume_text,
                           triage_model=args.triage_model,
                           screen_model=args.gate_model,
                           title_drops_extra=args.title_drops_extra)

    # Batched by stage: llama-swap holds ONE model, so screening postings
    # individually swaps models twice per posting. See prescreen.screen_batch.
    # Never lose a night's work to one bad request. A 20-minute run was lost to
    # a single ConnectionResetError escaping the executor; whatever survives
    # gets written either way.
    try:
        decisions = screener.screen_batch(
            fresh,
            triage_workers=args.triage_workers,
            gate_workers=args.gate_workers,
            progress=lambda m: print(f"[*] {m}"),
        )
    except BaseException as err:                     # includes KeyboardInterrupt
        errors.append(f"prescreen aborted: {type(err).__name__}: {err}")
        print(f"[!] prescreen aborted: {type(err).__name__}: {err}")
        decisions = []
    for d in sorted(decisions, key=lambda x: (not x.keep, x.company)):
        print(f"    {'keep' if d.keep else 'drop':4} {d.company[:24]:24} "
              f"{d.title[:44]:44} ({d.stage}: {d.reason[:44]})")

    kept = [d for d in decisions if d.keep]
    dropped = [d for d in decisions if not d.keep]
    kept.sort(key=lambda d: d.detail.get("fit_sketch") or 0, reverse=True)
    print(f"[*] kept {len(kept)}, dropped {len(dropped)}")

    # Fail-open keeps a posting IN THE REPORT, never in the paid queue: a
    # gate-model outage must not convert --max-submit slots into unscreened
    # Anthropic calls. They're listed for the human instead.
    submittable = [d for d in kept if d.stage != "error"]
    error_kept = len(kept) - len(submittable)
    if error_kept:
        msg = (f"{error_kept} posting(s) kept via screen-failure passthrough — "
               f"in the report for human review, EXCLUDED from paid submission")
        errors.append(msg)
        print(f"[!] {msg}")

    # --- submit (paid, capped) -------------------------------------------
    by_url = {p["url"]: p for p in fresh}
    submitted = []
    if args.submit:
        for d in submittable[: args.max_submit]:
            src = by_url.get(d.url, {})
            payload = {
                "url": d.url,
                "description": src.get("description", ""),
                "company": d.company,
                "title": d.title,
                "ats_platform": None,
                "posted_at": src.get("posted_at"),
                "discovered_by": f"overnight/{src.get('source', 'discovery')}",
            }
            try:
                res = http_json(f"{args.backend}/jobs/score", payload, timeout=180)
                submitted.append({"url": d.url, "title": d.title, "company": d.company,
                                  "rank": res.get("rank"), "score": res.get("candidate_score")})
                print(f"[$] {d.company} — {d.title[:44]} rank={res.get('rank')}")
            except urllib.error.HTTPError as err:
                print(f"[!] submit failed {d.url}: HTTP {err.code}")
            except Exception as err:
                print(f"[!] submit failed {d.url}: {err}")
        if len(submittable) > args.max_submit:
            print(f"[*] cap reached: {len(submittable) - args.max_submit} survivors not submitted")
    else:
        print(f"[*] DRY RUN — would submit {min(len(submittable), args.max_submit)} of {len(kept)}")

    # Companies worth a human look — never auto-added to the watch list.
    new_co = Counter(d.company for d in kept if d.company)
    if new_co:
        print("[*] companies producing keeps: " +
              ", ".join(f"{c}({n})" for c, n in new_co.most_common(12)))

    write_reports(outdir, kept, dropped, errors, submitted=submitted,
                  dry_run=not args.submit, new_companies=new_co)
    print(f"[*] reports -> {outdir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
