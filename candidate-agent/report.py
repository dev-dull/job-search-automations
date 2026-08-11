#!/usr/bin/env python3
"""Read-only reporting over the phase-2 database (the pre-UI admin surface).

    python3 report.py                        # per-code rollup
    python3 report.py --code <code>          # sessions for one code
    python3 report.py --session <session-id> # full transcript

Run where AGENT_DB_PATH points at the live database.
"""

from __future__ import annotations

import argparse
import sys

import store


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--code", help="list sessions for this code")
    ap.add_argument("--session", help="print the transcript for this session id")
    args = ap.parse_args(argv)
    db = store.Store()

    if args.session:
        for m in db.transcript(args.session):
            who = "EMPLOYER" if m["role"] == "user" else "AGENT   "
            cost = f"  (${m['cost_usd']:.4f})" if m["cost_usd"] else ""
            print(f"[{m['created_at']}] {who}{cost}\n{m['content']}\n")
        return 0

    if args.code:
        for s in db.sessions_for_code(args.code):
            print(f"{s['id']}\n  surface={s['surface']} "
                  f"client={s['client_name'] or '-'}/{s['client_version'] or '-'} "
                  f"ip={s['ip'] or '-'}\n  {s['started_at']} -> {s['last_active_at']}")
        return 0

    rows = db.summary()
    if not rows:
        print("No codes in the database yet.")
        return 0
    print(f"{'code':<22} {'label':<20} {'src':<5} {'sess':>4} {'msgs':>5} "
          f"{'cost':>8}  last seen")
    for r in rows:
        flag = " (REVOKED)" if r["revoked"] else ""
        print(f"{r['code']:<22} {r['label'][:19]:<20} {r['source']:<5} "
              f"{r['sessions']:>4} {r['messages']:>5} "
              f"${r['cost_usd']:>7.2f}  {r['last_seen'] or '-'}{flag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
