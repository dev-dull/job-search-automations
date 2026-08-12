"""WWR feed parsing: the Region prefix must not rescue a thin JD past the
scoring floor (PR #76 round-5 finding — this path spends money)."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sources import wwr  # noqa: E402


def _feed(items):
    body = "".join(
        f"<item><title>{t}</title><link>{l}</link>"
        f"<region>{r}</region><description>{d}</description></item>"
        for t, l, r, d in items)
    return f"<rss><channel>{body}</channel></rss>".encode()


class RegionFloorTest(unittest.TestCase):
    def _collect(self, items):
        with mock.patch.object(wwr, "_fetch", return_value=_feed(items)):
            return wwr.collect(feeds={"devops": "x"})

    def test_region_padding_does_not_rescue_a_teaser(self):
        # 70-char JD + a long region would clear 100 chars combined, but the
        # JD alone is under the floor -> dropped.
        teaser = "We are hiring a devops engineer to help us out. Apply soon!"  # ~59
        self.assertLess(len(teaser), wwr.MIN_JD_CHARS)
        out = self._collect([("Acme: DevOps Engineer",
                              "https://example.com/j",
                              "United States (long region string here)", teaser)])
        self.assertEqual(out, [])

    def test_real_jd_survives_and_region_is_included(self):
        jd = "x" * 200
        out = self._collect([("Acme: DevOps Engineer",
                              "https://example.com/j", "Europe", jd)])
        self.assertEqual(len(out), 1)
        self.assertIn("Region: Europe", out[0]["description"])
        self.assertIn(jd, out[0]["description"])


if __name__ == "__main__":
    unittest.main()
