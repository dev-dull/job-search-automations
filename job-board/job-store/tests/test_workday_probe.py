"""Workday probe-at-create tests (issue #33).

Candidate generation + verified resolution for pasted Workday URLs. The naive
lang/site parse saved a target with site='job' from a job-detail URL, which
404ed on every poll (the June poller incident) — resolve_identifier must pick
the parse that actually verifies against the CXS API. No network: `verify` and
`fetch_landing_path` are injected.

Run with: python3 -m unittest discover -s tests
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adapters import workday  # noqa: E402

HOST = "nvidia.wd5.myworkdayjobs.com"


def _verify_only(good_site):
    """A verify stub that accepts exactly one site name."""
    return lambda ident: ident.get("site") == good_site


class SiteCandidatesTest(unittest.TestCase):
    def test_locale_first_segment_prefers_lang_site(self):
        cands = workday.site_candidates(HOST, ["en-US", "NVIDIAExternalCareerSite"])
        self.assertEqual(cands[0], {"host": HOST, "lang": "en-US",
                                    "site": "NVIDIAExternalCareerSite"})
        self.assertEqual(cands[1], {"host": HOST, "site": "en-US"})

    def test_non_locale_first_segment_prefers_bare_site(self):
        # The incident shape: /<site>/job — site-first ordering means the
        # correct parse is tried (and verified) before the bogus one.
        cands = workday.site_candidates(HOST, ["NVIDIAExternalCareerSite", "job"])
        self.assertEqual(cands[0], {"host": HOST, "site": "NVIDIAExternalCareerSite"})
        self.assertEqual(cands[1], {"host": HOST, "lang": "NVIDIAExternalCareerSite",
                                    "site": "job"})

    def test_single_segment(self):
        self.assertEqual(workday.site_candidates(HOST, ["jobs"]),
                         [{"host": HOST, "site": "jobs"}])

    def test_no_segments(self):
        self.assertEqual(workday.site_candidates(HOST, []), [])


class ResolveIdentifierTest(unittest.TestCase):
    def test_incident_url_resolves_to_real_site(self):
        # The exact URL shape that created broken target id=18.
        url = f"https://{HOST}/NVIDIAExternalCareerSite/job"
        got = workday.resolve_identifier(
            url, verify=_verify_only("NVIDIAExternalCareerSite"))
        self.assertEqual(got, {"host": HOST, "site": "NVIDIAExternalCareerSite"})

    def test_job_detail_url_with_locale(self):
        url = f"https://{HOST}/en-US/NVIDIAExternalCareerSite/job/US-CA/Slug_JR123?src=LinkedIn"
        got = workday.resolve_identifier(
            url, verify=_verify_only("NVIDIAExternalCareerSite"))
        self.assertEqual(got, {"host": HOST, "lang": "en-US",
                               "site": "NVIDIAExternalCareerSite"})

    def test_nothing_verifies_returns_none(self):
        url = f"https://{HOST}/en-US/NVIDIAExternalCareerSite"
        self.assertIsNone(workday.resolve_identifier(url, verify=lambda _: False))

    def test_non_workday_url_returns_none(self):
        self.assertIsNone(workday.resolve_identifier(
            "https://boards.greenhouse.io/elastic", verify=lambda _: True))

    def test_bare_host_uses_landing_redirect(self):
        got = workday.resolve_identifier(
            f"https://{HOST}/",
            verify=_verify_only("NVIDIAExternalCareerSite"),
            fetch_landing_path=lambda host: "/en-US/NVIDIAExternalCareerSite",
        )
        self.assertEqual(got, {"host": HOST, "lang": "en-US",
                               "site": "NVIDIAExternalCareerSite"})

    def test_bare_host_landing_fetch_failure(self):
        self.assertIsNone(workday.resolve_identifier(
            f"https://{HOST}/", verify=lambda _: True,
            fetch_landing_path=lambda host: None))


if __name__ == "__main__":
    unittest.main()
