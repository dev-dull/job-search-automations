"""iCIMS adapter tests (issue #29).

Fixtures mirror the live classic-iframe HTML captured from
careers-healthedge.icims.com (2026-07-19). No network: _http_get is mocked.

Run with: python3 -m unittest discover -s tests
"""

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adapters import icims  # noqa: E402

TENANT = "careers-healthedge"


def _row(job_id, slug, title, location):
    return f'''
<li class="iCIMS_JobCardItem">
  <div class="row">
    <div class="col-xs-6 header left">
      <span class="sr-only field-label">Job Locations</span>
      <span >{location}</span>
    </div>
    <div class="col-xs-12 title">
      <a href="https://{TENANT}.icims.com/jobs/{job_id}/{slug}/job?in_iframe=1"
         class="iCIMS_Anchor" title="{job_id} - {title}">
        <span class="sr-only field-label">Title</span>
        <h3 >{title}</h3>
      </a>
    </div>
  </div>
</li>'''


LISTING = ("<html><body><ul>"
           + _row(8174, "director-bizops", "Director, Business Operations", "US-Remote")
           + _row(8130, "senior-release-engineer", "Senior Release Engineer", "US-MA-Burlington")
           + _row(8161, "program-manager", "Program Manager", "IN-Hyderabad")
           + "</ul></body></html>")

DETAIL = ('<html><head><script type="application/ld+json">'
          + json.dumps({"@type": "JobPosting", "title": "Senior Release Engineer",
                        "datePosted": "2026-06-29T04:00:00.000Z",
                        "description": "<p>Own the <b>release pipeline</b>.</p>"})
          + "</script></head><body></body></html>")


class ParseListingTest(unittest.TestCase):
    def test_rows_parse(self):
        stubs = icims.parse_listing(LISTING, TENANT)
        self.assertEqual(len(stubs), 3)
        s = stubs[1]
        self.assertEqual(s["url"], f"https://{TENANT}.icims.com/jobs/8130/senior-release-engineer/job")
        self.assertEqual(s["title"], "Senior Release Engineer")
        self.assertEqual(s["location"], "US-MA-Burlington")
        self.assertIsNone(s["posted_at"])       # dates come from the detail page

    def test_bot_shell_parses_to_nothing(self):
        self.assertEqual(icims.parse_listing("<html>redirecting…</html>", TENANT), [])


class ListJobsTest(unittest.TestCase):
    def test_short_page_stops_pagination(self):
        with mock.patch.object(icims, "_http_get", return_value=LISTING) as m:
            jobs = icims.list_jobs({"tenant": TENANT})
        self.assertEqual(len(jobs), 3)
        self.assertEqual(m.call_count, 1)       # 3 rows < 50 -> last page

    def test_duplicate_page_stops_pagination(self):
        # A tenant that serves the same full page forever must not loop: fake
        # a 50-row page by patching the page-size check boundary via repeat rows.
        rows = "".join(_row(9000 + i, f"job-{i}", f"Job {i}", "US-Remote") for i in range(50))
        page = f"<html><ul>{rows}</ul></html>"
        with mock.patch.object(icims, "_http_get", return_value=page) as m:
            jobs = icims.list_jobs({"tenant": TENANT})
        self.assertEqual(len(jobs), 50)         # second fetch yields no fresh rows
        self.assertEqual(m.call_count, 2)

    def test_missing_tenant_raises(self):
        with self.assertRaises(ValueError):
            icims.list_jobs({})


class FetchDescriptionTest(unittest.TestCase):
    def test_description_and_posted_at(self):
        job = {"url": f"https://{TENANT}.icims.com/jobs/8130/senior-release-engineer/job",
               "posted_at": None}
        with mock.patch.object(icims, "_http_get", return_value=DETAIL) as m:
            desc = icims.fetch_description(job)
        self.assertIn("release pipeline", desc)
        self.assertEqual(job["posted_at"], "2026-06-29")
        self.assertIn("in_iframe=1", m.call_args[0][0])

    def test_transient_failure(self):
        with mock.patch.object(icims, "_http_get", side_effect=OSError("boom")):
            self.assertEqual(icims.fetch_description({"url": "https://x.icims.com/jobs/1/a/job"}), "")


class VerifyTenantTest(unittest.TestCase):
    def test_crawlable_tenant(self):
        with mock.patch.object(icims, "_http_get", return_value=LISTING):
            self.assertTrue(icims.verify_tenant(TENANT))

    def test_bot_shell_tenant_fails_closed(self):
        with mock.patch.object(icims, "_http_get", return_value="<html>guard</html>"):
            self.assertFalse(icims.verify_tenant("careers-rivian"))

    def test_fetch_error_fails_closed(self):
        with mock.patch.object(icims, "_http_get", side_effect=OSError("403")):
            self.assertFalse(icims.verify_tenant(TENANT))
        self.assertFalse(icims.verify_tenant(""))


if __name__ == "__main__":
    unittest.main()
