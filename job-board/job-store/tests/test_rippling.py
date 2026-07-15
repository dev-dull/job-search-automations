"""Rippling adapter tests (issue #22).

Mocks the HTTP layer; shapes are copied from the live board API and a live
job page's __NEXT_DATA__ (board 'button', 2026-07-15 recon on #22).

Run with: python3 -m unittest discover -s tests
"""

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adapters import rippling  # noqa: E402

UUID = "6673232b-93a2-4737-a1b7-d72a109751cb"
JOB_URL = f"https://ats.rippling.com/button/jobs/{UUID}"

BOARD_RESPONSE = [
    {"name": "Senior DevOps Engineer - Infrastructure", "url": JOB_URL,
     "uuid": UUID, "department": {"name": "Engineering"},
     "workLocation": {"label": "Remote (US)"}},
    {"name": "Customer Service Operations Associate",
     "url": "https://ats.rippling.com/button/jobs/8ea164a8-21e2-410e-9ff4-000000000000",
     "uuid": "8ea164a8-21e2-410e-9ff4-000000000000",
     "department": {"name": "Ops"}, "workLocation": "New York, NY"},
    {"name": "No URL — skipped", "uuid": "x", "workLocation": ""},
]


def _page_html(job_post):
    blob = {"props": {"pageProps": {"apiData": {"jobPost": job_post}}}}
    return ('<html><body><div id="root"></div>'
            f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(blob)}</script>'
            "</body></html>")


JOB_POST = {
    "name": "Senior DevOps Engineer - Infrastructure",
    "companyName": "Button",
    "createdOn": "2026-05-20T13:45:46.122000-07:00",
    "description": {
        "company": '<meta><p style="font-family:X">Button is a company.</p>',
        "role": "<p>Build <b>infrastructure</b>.</p><li>Kubernetes</li>",
    },
}


class ListJobsTest(unittest.TestCase):
    def test_maps_board_items(self):
        with mock.patch.object(rippling, "_http_get",
                               return_value=json.dumps(BOARD_RESPONSE)):
            jobs = rippling.list_jobs({"slug": "button"})
        self.assertEqual(len(jobs), 2)          # url-less item skipped
        self.assertEqual(jobs[0]["title"], "Senior DevOps Engineer - Infrastructure")
        self.assertEqual(jobs[0]["url"], JOB_URL)
        self.assertIsNone(jobs[0]["posted_at"])  # list API carries no dates
        self.assertEqual(jobs[0]["location"], "Remote (US)")   # dict form
        self.assertEqual(jobs[1]["location"], "New York, NY")  # string form

    def test_missing_slug_raises(self):
        with self.assertRaises(ValueError):
            rippling.list_jobs({})

    def test_non_list_response_raises(self):
        with mock.patch.object(rippling, "_http_get", return_value='{"detail": "x"}'):
            with self.assertRaises(ValueError):
                rippling.list_jobs({"slug": "button"})


class FetchDescriptionTest(unittest.TestCase):
    def test_description_and_posted_at_side_effect(self):
        job = {"url": JOB_URL, "posted_at": None}
        with mock.patch.object(rippling, "_http_get",
                               return_value=_page_html(JOB_POST)):
            desc = rippling.fetch_description(job)
        self.assertIn("Build infrastructure", desc)
        self.assertIn("Kubernetes", desc)
        # role text leads; company boilerplate follows
        self.assertLess(desc.index("Build"), desc.index("Button is a company"))
        self.assertEqual(job["posted_at"], "2026-05-20")

    def test_existing_posted_at_not_overwritten(self):
        job = {"url": JOB_URL, "posted_at": "2026-07-01"}
        with mock.patch.object(rippling, "_http_get",
                               return_value=_page_html(JOB_POST)):
            rippling.fetch_description(job)
        self.assertEqual(job["posted_at"], "2026-07-01")

    def test_transient_failure_returns_empty(self):
        with mock.patch.object(rippling, "_http_get", side_effect=OSError("boom")):
            self.assertEqual(rippling.fetch_description({"url": JOB_URL}), "")

    def test_page_without_next_data(self):
        with mock.patch.object(rippling, "_http_get", return_value="<html></html>"):
            self.assertEqual(rippling.fetch_description({"url": JOB_URL}), "")


class HelperTest(unittest.TestCase):
    def test_flatten_plain_string(self):
        self.assertEqual(rippling.flatten_description("<p>hi</p>"), "hi")

    def test_flatten_junk(self):
        self.assertEqual(rippling.flatten_description(None), "")
        self.assertEqual(rippling.flatten_description(42), "")

    def test_verify_board(self):
        with mock.patch.object(rippling, "_http_get", return_value="[]"):
            self.assertTrue(rippling.verify_board("button"))
        with mock.patch.object(rippling, "_http_get", side_effect=OSError("404")):
            self.assertFalse(rippling.verify_board("nope"))
        self.assertFalse(rippling.verify_board(""))


if __name__ == "__main__":
    unittest.main()
