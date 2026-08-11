#!/usr/bin/env python3
"""Mint (or revoke) access codes directly in the phase-2 database.

The flat file at ACCESS_CODES_PATH remains fully supported — file-sourced
codes sync on reload and revoke by deleting the line. This CLI manages
DB-native ('cli') codes, which the file sync never touches.

    python3 mint_code.py "Acme Recruiting"                  # 30-day code
    python3 mint_code.py "Beta Search" --days 14 --url-auth
    python3 mint_code.py --revoke maple-K7RT-hazel

Run where AGENT_DB_PATH points at the live database (e.g. inside the
container/pod, or against the mounted volume).
"""

from __future__ import annotations

import argparse
import secrets
import sys
from datetime import date, timedelta

import store

WORDS = ("maple birch cedar aspen alder hazel rowan willow juniper laurel "
         "otter heron finch marten osprey plover raven teal wren ibis").split()


def generate_code() -> str:
    a, b = secrets.choice(WORDS), secrets.choice(WORDS)
    mid = "".join(secrets.choice("ABCDEFGHJKMNPQRSTUVWXYZ23456789") for _ in range(4))
    return f"{a}-{mid}-{b}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("label", nargs="?", help="who this code is issued to")
    ap.add_argument("--days", type=int, default=30, help="expiry (default 30)")
    ap.add_argument("--url-auth", action="store_true",
                    help="allow URL-carried use (/c/<code>, /mcp/<code>)")
    ap.add_argument("--note", default="")
    ap.add_argument("--revoke", metavar="CODE",
                    help="revoke a CLI-minted code instead of minting")
    args = ap.parse_args(argv)

    db = store.Store()
    if args.revoke:
        if db.revoke_cli_code(args.revoke):
            print(f"revoked {args.revoke}")
            return 0
        print(f"no CLI-minted code {args.revoke!r} (file codes revoke by "
              f"deleting their line)", file=sys.stderr)
        return 1

    if not args.label:
        ap.error("label is required when minting")
    code = generate_code()
    expires = (date.today() + timedelta(days=args.days)).isoformat()
    db.upsert_cli_code(code, args.label, expires, args.url_auth, args.note)
    print(f"{code}   ({args.label}, expires {expires}"
          f"{', url_auth' if args.url_auth else ''})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
