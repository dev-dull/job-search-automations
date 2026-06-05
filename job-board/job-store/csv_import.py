"""
One-time import of the existing CSV (`2025 Job Hunt Report - Sheet1.csv`) into
the jobs/outcomes tables. Brings historical platform success data into the
ranker so platform_factor starts with real signal instead of zero.

Usage:

    python csv_import.py "../2025 Job Hunt Report - Sheet1.csv"

Imports rows where Applied=TRUE (those are the only ones with outcomes worth
tracking). Synthesizes a `csv-import://` URL per row since the original
listing URLs aren't in the CSV.
"""

import argparse
import csv
import sys
from pathlib import Path

import db


def _bool(value):
    return (value or "").strip().upper() == "TRUE"


def import_csv(csv_path):
    inserted = 0
    skipped = 0
    duplicates = 0

    with open(csv_path, "r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if not _bool(row.get("Applied T/F")):
                skipped += 1
                continue

            position = (row.get("Position (branch)") or "").strip()
            company = (row.get("Company") or "").strip()
            url = f"csv-import://{position}"

            with db.cursor() as conn:
                exists = conn.execute(
                    "SELECT id FROM jobs WHERE url = ?", (url,)
                ).fetchone()
                if exists:
                    duplicates += 1
                    continue

                cur = conn.execute(
                    """
                    INSERT INTO jobs (url, company, title, ats_platform, status,
                                      discovered_by, applied_at)
                    VALUES (?, ?, ?, ?, 'applied', 'csv-import', CURRENT_TIMESTAMP)
                    """,
                    (url, company, position, (row.get("Platform Name") or "").strip()),
                )
                job_id = cur.lastrowid

            db.upsert_outcome(
                job_id,
                referral=_bool(row.get("Referral")),
                callback=_bool(row.get("Callback T/F")),
                ghosted=_bool(row.get("Ghosted")),
                offer=_bool(row.get("Offer")),
                notes=(row.get("Notes") or "").strip(),
            )
            inserted += 1

    return inserted, skipped, duplicates


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path", help="Path to the 2025 Job Hunt Report CSV")
    args = parser.parse_args()

    csv_path = Path(args.csv_path)
    if not csv_path.exists():
        print(f"CSV not found: {csv_path}", file=sys.stderr)
        sys.exit(1)

    db.init_db()
    inserted, skipped, duplicates = import_csv(csv_path)
    print(
        f"Imported {inserted} applied jobs · "
        f"skipped {skipped} non-applied rows · "
        f"{duplicates} already in DB"
    )


if __name__ == "__main__":
    main()
