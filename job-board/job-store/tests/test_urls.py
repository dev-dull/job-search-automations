"""URL canonicalization + dedupe-key tests (issue #32).

Seeded with real-world URL shapes. The core contract: every URL form that
points at one posting must produce the same `compute_dedupe_key`, and
`canonicalize_url` must be idempotent.

Run with:
    python3 -m unittest discover -s tests
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from urls import canonicalize_url, compute_dedupe_key  # noqa: E402


class DedupeKeyCollapseTest(unittest.TestCase):
    """Each group is a set of URLs that must all collapse to one dedupe key."""

    GROUPS = {
        "greenhouse-voxel51": [
            "https://voxel51.com/jd?gh_jid=4689145005",
            "https://job-boards.greenhouse.io/voxel51/jobs/4689145005",
            "https://boards.greenhouse.io/voxel51/jobs/4689145005",  # legacy subdomain
            "https://boards.greenhouse.io/voxel51/jobs/4689145005/",  # trailing slash
            "https://voxel51.com/jd?gh_jid=4689145005&utm_source=linkedin",  # tracking
            "https://boards.greenhouse.io/embed/job_app?token=4689145005",   # job_app embed
        ],
        "workday-cisco": [
            "https://cisco.wd5.myworkdayjobs.com/en-US/cisco/job/San-Jose/Software-Engineer_JR1234567",
            "https://cisco.wd5.myworkdayjobs.com/cisco/job/San-Jose/Software-Engineer_JR1234567",  # no locale
            "https://cisco.wd5.myworkdayjobs.com/en-US/cisco/job/Software-Engineer_JR1234567",      # no location seg
            "https://cisco.wd5.myworkdayjobs.com/en-US/cisco/job/San-Jose/Software-Engineer_JR1234567?source=indeed",
        ],
        "linkedin-4012345678": [
            "https://www.linkedin.com/jobs/view/4012345678/",
            "https://www.linkedin.com/jobs/view/4012345678",
            "https://www.linkedin.com/jobs/view/4012345678?refId=abc%3D%3D&trackingId=xyz",
            "https://www.linkedin.com/jobs/collections/recommended/?currentJobId=4012345678",
        ],
        "ashby-acme": [
            "https://jobs.ashbyhq.com/acme/12345678-90ab-cdef-1234-567890abcdef",
            "https://jobs.ashbyhq.com/acme/12345678-90ab-cdef-1234-567890abcdef/",
            "https://jobs.ashbyhq.com/acme/12345678-90ab-cdef-1234-567890abcdef/application",
            "https://www.ashbyhq.com/careers?ashby_jid=12345678-90ab-cdef-1234-567890abcdef",
        ],
        "lever-foo": [
            "https://jobs.lever.co/foo/abcdef12-3456-7890-abcd-ef1234567890",
            "https://jobs.lever.co/foo/abcdef12-3456-7890-abcd-ef1234567890/",
            "https://jobs.lever.co/foo/abcdef12-3456-7890-abcd-ef1234567890/apply",
            "https://jobs.lever.co/foo/abcdef12-3456-7890-abcd-ef1234567890?lever-source=LinkedIn",
        ],
        "rippling-button": [
            # jobSite/src are inbound-link tracking; param order irrelevant.
            "https://ats.rippling.com/button/jobs/6673232b-93a2-4737-a1b7-d72a109751cb?jobSite=LinkedIn&src=linkedin",
            "https://ats.rippling.com/button/jobs/6673232b-93a2-4737-a1b7-d72a109751cb",
            "https://ats.rippling.com/button/jobs/6673232b-93a2-4737-a1b7-d72a109751cb?src=indeed&jobSite=Indeed",
        ],
        "taleo-costco": [
            # org + rid identify the requisition; cws + source/src/gns are
            # per-inbound-link noise that must not split the row.
            "https://phf.tbe.taleo.net/phf02/ats/careers/v2/viewRequisition?org=COSTCO&cws=41&rid=10040&source=LinkedIn&src=LinkedIn&gns=LinkedIn",
            "https://phf.tbe.taleo.net/phf02/ats/careers/v2/viewRequisition?org=COSTCO&cws=41&rid=10040",
            "https://phf.tbe.taleo.net/phf02/ats/careers/v2/viewRequisition?org=COSTCO&rid=10040&cws=9",
        ],
    }

    def test_each_group_collapses_to_one_key(self):
        for name, urls in self.GROUPS.items():
            keys = {u: compute_dedupe_key(u) for u in urls}
            distinct = set(keys.values())
            self.assertEqual(
                len(distinct), 1,
                f"group {name!r} did not collapse to one key:\n" +
                "\n".join(f"  {k} -> {v}" for k, v in keys.items()),
            )

    def test_distinct_postings_do_not_collide(self):
        # A representative URL from each group must yield a *different* key.
        reps = [urls[0] for urls in self.GROUPS.values()]
        keys = [compute_dedupe_key(u) for u in reps]
        self.assertEqual(len(set(keys)), len(keys), f"keys collided: {keys}")

    def test_different_ids_same_platform_differ(self):
        a = compute_dedupe_key("https://cisco.wd5.myworkdayjobs.com/cisco/job/X_JR1111111")
        b = compute_dedupe_key("https://cisco.wd5.myworkdayjobs.com/cisco/job/X_JR2222222")
        self.assertNotEqual(a, b)


class CanonicalizeUrlTest(unittest.TestCase):
    SAMPLES = [
        "https://voxel51.com/jd?gh_jid=4689145005&utm_source=linkedin",
        "https://boards.greenhouse.io/voxel51/jobs/4689145005/",
        "https://jobs.lever.co/foo/abcdef12-3456-7890-abcd-ef1234567890?lever-source=LinkedIn",
        "https://cisco.wd5.myworkdayjobs.com/en-US/cisco/job/Software-Engineer_JR1234567",
        "https://www.linkedin.com/jobs/view/4012345678?refId=abc",
    ]

    def test_idempotent(self):
        for u in self.SAMPLES:
            once = canonicalize_url(u)
            twice = canonicalize_url(once)
            self.assertEqual(once, twice, f"not idempotent for {u}: {once!r} != {twice!r}")

    def test_strips_tracking_params(self):
        out = canonicalize_url(
            "https://jobs.lever.co/foo/abcdef12-3456-7890-abcd-ef1234567890?lever-source=LinkedIn"
        )
        self.assertNotIn("lever-source", out)

    def test_keeps_gh_jid(self):
        out = canonicalize_url("https://voxel51.com/jd?gh_jid=4689145005&utm_source=x")
        self.assertIn("gh_jid=4689145005", out)
        self.assertNotIn("utm_source", out)

    def test_manual_paste_passthrough(self):
        self.assertEqual(canonicalize_url("manual-paste-123"), "manual-paste-123")
        self.assertEqual(compute_dedupe_key("manual-paste-123"), "manual-paste-123")


if __name__ == "__main__":
    unittest.main()
