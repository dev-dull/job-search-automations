"""Unit tests for the Workday title backfill helpers (issue #42).

These cover the pure URL/slug functions only — no network. Run with:
    python3 -m unittest discover -s tests
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backfill_workday_titles as bf  # noqa: E402


class ParsePublicUrlTest(unittest.TestCase):
    def test_clean_url_no_location(self):
        host, site, ext = bf._parse_public_url(
            "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite"
            "/job/Senior-Software-Engineer--DGX-Cloud_JR2017916"
        )
        self.assertEqual(host, "nvidia.wd5.myworkdayjobs.com")
        self.assertEqual(site, "NVIDIAExternalCareerSite")
        self.assertEqual(ext, "/job/Senior-Software-Engineer--DGX-Cloud_JR2017916")

    def test_url_with_location_segment_and_query(self):
        # Plugin-captured browser URLs carry a location segment + query string.
        host, site, ext = bf._parse_public_url(
            "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite"
            "/job/US-CA-Santa-Clara/Senior-Software-Engineer--Agentic-AI_JR2017340"
            "?locationHierarchy2=abc&jobFamilyGroup=def"
        )
        self.assertEqual(site, "NVIDIAExternalCareerSite")
        self.assertEqual(
            ext, "/job/US-CA-Santa-Clara/Senior-Software-Engineer--Agentic-AI_JR2017340"
        )

    def test_url_without_lang_segment(self):
        # Some tenants omit the language segment; site is still the segment
        # immediately before "job".
        _, site, ext = bf._parse_public_url(
            "https://example.wd1.myworkdayjobs.com/SomeSite/job/Engineer_R12345"
        )
        self.assertEqual(site, "SomeSite")
        self.assertEqual(ext, "/job/Engineer_R12345")

    def test_missing_job_segment_raises(self):
        with self.assertRaises(ValueError):
            bf._parse_public_url("https://example.wd1.myworkdayjobs.com/en-US/SomeSite/")


class StripReqidTest(unittest.TestCase):
    def test_plain_reqid(self):
        self.assertEqual(bf._strip_reqid("Senior-Engineer_JR2017916"), "Senior-Engineer")

    def test_hyphenated_reqid(self):  # Red Hat style
        self.assertEqual(bf._strip_reqid("Senior-Engineer_R-056651"), "Senior-Engineer")

    def test_alnum_hyphen_reqid(self):  # Autodesk style
        self.assertEqual(bf._strip_reqid("Principal-Engineer_26WD95712-1"), "Principal-Engineer")

    def test_no_reqid_left_untouched(self):
        self.assertEqual(bf._strip_reqid("Senior-Engineer"), "Senior-Engineer")


class SlugTitleTest(unittest.TestCase):
    def test_separators_approximated(self):
        # "--" -> ", " and "---" -> " - " (lossy but readable).
        title = bf._slug_title(
            "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite"
            "/job/Senior-Software-Engineer--Distributed-Systems-Engineer---DGX-Cloud_JR2017916"
        )
        self.assertEqual(
            title, "Senior Software Engineer, Distributed Systems Engineer - DGX Cloud"
        )

    def test_uses_last_segment_ignoring_location(self):
        title = bf._slug_title(
            "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite"
            "/job/US-CA-Santa-Clara/Senior-Software-Engineer--Agentic-AI_JR2017340"
        )
        self.assertEqual(title, "Senior Software Engineer, Agentic AI")


class LooksGenericTest(unittest.TestCase):
    def test_banners_are_generic(self):
        for t in ["CAREERS AT NVIDIA", "Careers", "Intel Careers",
                  "Careers at Red Hat", "Career Site", "CAREERS", ""]:
            self.assertTrue(bf._looks_generic(t), t)

    def test_real_titles_are_not_generic(self):
        for t in ["Senior Software Engineer, Distributed Systems Engineer - DGX Cloud",
                  "Principal Engineer, AI-Enabled Engineering",
                  "Staff Software Engineer"]:
            self.assertFalse(bf._looks_generic(t), t)


if __name__ == "__main__":
    unittest.main()
