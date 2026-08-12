"""The HN parse heuristics — the most brittle code here. Synthetic comments."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sources import hn  # noqa: E402

POSTING_TEXT = ("Acme | Senior DevOps Engineer | REMOTE (US) | $150k-$180k\n"
                "We're hiring a senior engineer to own our deploy pipeline. "
                "Apply at https://boards.greenhouse.io/acme/jobs/123 or see "
                "https://news.ycombinator.com/item?id=1 for discussion.")


class CompanyTitleTest(unittest.TestCase):
    def test_convention_parse(self):
        c, t = hn._company_and_title("Acme | Senior DevOps Engineer | REMOTE | $150k")
        self.assertEqual((c, t), ("Acme", "Senior DevOps Engineer"))

    def test_noise_parts_skipped_for_title(self):
        c, t = hn._company_and_title("Acme | REMOTE (US) | Senior Platform Engineer")
        self.assertEqual(t, "Senior Platform Engineer")

    def test_empty(self):
        self.assertEqual(hn._company_and_title(""), ("", ""))


class LooksLikePostingTest(unittest.TestCase):
    def test_real_posting_passes(self):
        self.assertTrue(hn._looks_like_posting("Acme", "Senior DevOps Engineer",
                                               POSTING_TEXT))

    def test_salary_as_title_rejected(self):
        self.assertFalse(hn._looks_like_posting("Acme", "70k", POSTING_TEXT))

    def test_location_as_title_rejected(self):
        self.assertFalse(hn._looks_like_posting("Acme", "New York, NY", POSTING_TEXT))

    def test_short_text_rejected(self):
        self.assertFalse(hn._looks_like_posting("Acme", "Engineer", "too short"))

    def test_sentence_fragment_company_rejected(self):
        self.assertFalse(hn._looks_like_posting(
            "This book was one of the best I", "Engineer", POSTING_TEXT))


class BestUrlTest(unittest.TestCase):
    def test_prefers_ats_link_and_skips_hn(self):
        self.assertEqual(hn._best_url(POSTING_TEXT),
                         "https://boards.greenhouse.io/acme/jobs/123")

    def test_no_usable_url(self):
        self.assertIsNone(hn._best_url("see https://news.ycombinator.com/item?id=1"))


if __name__ == "__main__":
    unittest.main()
