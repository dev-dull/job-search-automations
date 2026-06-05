"""Adapter smoke tests.

Run with either:
    python3 -m unittest discover -s tests
    python3 -m pytest tests/        # if pytest is installed

These tests mock the network layer (`_http_json`) so they make no real HTTP
calls and need no credentials.
"""

import os
import sys
import unittest
from unittest import mock

# Make the job-store package root importable when the test is run directly
# (e.g. `python3 -m unittest discover -s tests`) without an installed package.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adapters import workday  # noqa: E402


def _page(start, count, total):
    """Build a CXS-shaped list response with `count` postings starting at
    req number `start`. Only the first page carries a non-zero `total`,
    mirroring the real Workday CXS API (see issue #41)."""
    postings = [
        {
            "externalPath": f"/job/Remote/Engineer_JR{start + i:07d}",
            "title": f"Engineer {start + i}",
            "locationsText": "Remote",
            "startDate": "2026-06-01",
        }
        for i in range(count)
    ]
    return {"jobPostings": postings, "total": total}


class WorkdayPaginationTest(unittest.TestCase):
    IDENT = {
        "host": "nvidia.wd5.myworkdayjobs.com",
        "site": "NVIDIAExternalCareerSite",
        "lang": "en-US",
    }

    def test_total_latched_from_first_page(self):
        """Regression for #41: the CXS API only returns `total` on page 1
        (later pages report total=0). The loop must latch the first page's
        total and keep paginating, rather than terminating at offset 20."""
        # Two pages: page 1 advertises total=40, page 2 reports total=0
        # (as the real API does). Both pages carry 20 real postings.
        pages = [
            _page(start=1, count=workday.PAGE_SIZE, total=40),
            _page(start=21, count=workday.PAGE_SIZE, total=0),
        ]
        with mock.patch.object(workday, "_http_json", side_effect=pages) as m:
            jobs = workday.list_jobs(self.IDENT)

        # Both pages processed -> 40 postings, not 20.
        self.assertEqual(len(jobs), 40)
        # Exactly two list calls were made (offset=0, offset=20), then the
        # latched total=40 stops the walk.
        self.assertEqual(m.call_count, 2)
        # Postings from both pages are present.
        titles = {j["title"] for j in jobs}
        self.assertIn("Engineer 1", titles)
        self.assertIn("Engineer 40", titles)

    def test_empty_page_terminates_when_total_unknown(self):
        """If a tenant never reports a usable `total`, the `if not items`
        guard still terminates the walk once the API runs out of postings."""
        pages = [
            _page(start=1, count=workday.PAGE_SIZE, total=0),
            _page(start=21, count=workday.PAGE_SIZE, total=0),
            {"jobPostings": [], "total": 0},  # walked off the end
        ]
        with mock.patch.object(workday, "_http_json", side_effect=pages) as m:
            jobs = workday.list_jobs(self.IDENT)

        self.assertEqual(len(jobs), 40)
        self.assertEqual(m.call_count, 3)

    def test_max_pages_caps_runaway_walk(self):
        """A tenant whose `total` exceeds the safety cap stops at
        MAX_PAGES * PAGE_SIZE rather than walking forever."""
        big_total = (workday.MAX_PAGES + 5) * workday.PAGE_SIZE

        def _always_full(url, *, method="GET", body=None):
            return _page(start=body["offset"] + 1, count=workday.PAGE_SIZE,
                         total=big_total)

        with mock.patch.object(workday, "_http_json", side_effect=_always_full) as m:
            jobs = workday.list_jobs(self.IDENT)

        self.assertEqual(len(jobs), workday.MAX_PAGES * workday.PAGE_SIZE)
        self.assertEqual(m.call_count, workday.MAX_PAGES)


if __name__ == "__main__":
    unittest.main()
