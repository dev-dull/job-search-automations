"""Skip-when-seen tests (issue #71).

The poller used to STOP at the first already-seen posting, assuming listings
are strictly newest-first — Workday re-bumps seen postings above new ones and
Rippling documents no ordering, so anything below the first seen item was
permanently invisible (the JR2019408 miss). It now walks the whole fetched
window, skipping seen postings by DEDUPE KEY so plugin-discovered URL variants
count as seen.

Run with: python3 -m unittest discover -s tests
"""

import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import poller  # noqa: E402
from urls import compute_dedupe_key  # noqa: E402


def _gh(n, title="T"):
    return {"url": f"https://boards.greenhouse.io/x/jobs/{n}", "title": f"{title}{n}",
            "description": "d" * 200, "posted_at": None, "location": "United States"}


def _fake_adapter(jobs):
    mod = types.SimpleNamespace()
    mod.list_jobs = lambda identifier: [dict(j) for j in jobs]
    mod.fetch_description = lambda job: job.get("description") or ""
    return mod


def _target():
    return {"id": 1, "name": "T", "ats_platform": "fake",
            "ats_identifier_parsed": {}, "deny_list": []}


def _run(jobs, seen_keys, **kw):
    scored = []
    def fake_post(backend, job, name):
        scored.append(job["url"])
        return True, {"fit_score": 80}
    with mock.patch.dict(poller.ADAPTERS, {"fake": _fake_adapter(jobs)}), \
         mock.patch.object(poller, "_post_score", side_effect=fake_post), \
         mock.patch.object(poller, "_post_json", return_value={}):
        summary = poller.poll_target(
            _target(), backend="http://x", seen_keys=seen_keys,
            dry_run=False, max_new=kw.get("max_new"),
            location_allowlist=["United States"], location_denylist=[])
    return summary, scored


class SkipWhenSeenTest(unittest.TestCase):
    def test_interleaved_seen_does_not_hide_new(self):
        # The issue's acceptance case: [seen, new, seen, new] -> BOTH new
        # postings scored; the old stop-at-first-seen scored neither.
        jobs = [_gh(1), _gh(2), _gh(3), _gh(4)]
        seen = {compute_dedupe_key(jobs[0]["url"]),
                compute_dedupe_key(jobs[2]["url"])}
        summary, scored = _run(jobs, seen)
        self.assertEqual(scored, [jobs[1]["url"], jobs[3]["url"]])
        self.assertEqual(summary["skipped_seen"], 2)
        self.assertEqual(summary["scored"], 2)

    def test_url_variants_count_as_seen(self):
        # Plugin stored the locale-less Workday URL; the adapter emits the
        # en-US variant. Key matching must treat them as the same posting.
        plugin_stored = ("https://nvidia.wd5.myworkdayjobs.com/"
                         "NVIDIAExternalCareerSite/job/US-CA/Slug_JR2019408")
        adapter_emits = {"url": ("https://nvidia.wd5.myworkdayjobs.com/en-US/"
                                 "NVIDIAExternalCareerSite/job/US-CA/Slug_JR2019408"),
                         "title": "T", "description": "d" * 200,
                         "posted_at": None, "location": "United States"}
        seen = {compute_dedupe_key(plugin_stored)}
        summary, scored = _run([adapter_emits], seen)
        self.assertEqual(scored, [])
        self.assertEqual(summary["skipped_seen"], 1)

    def test_max_new_still_caps(self):
        jobs = [_gh(i) for i in range(1, 6)]
        summary, scored = _run(jobs, set(), max_new=2)
        self.assertEqual(len(scored), 2)
        self.assertTrue(summary["max_new_reached"])

    def test_scored_postings_join_the_seen_set(self):
        # Same posting appearing twice in one walk scores once.
        jobs = [_gh(1), _gh(1)]
        seen = set()
        summary, scored = _run(jobs, seen)
        self.assertEqual(len(scored), 1)
        self.assertEqual(summary["skipped_seen"], 1)
        self.assertIn(compute_dedupe_key(jobs[0]["url"]), seen)


class SeenKeysFetchTest(unittest.TestCase):
    def test_prefers_backend_keys(self):
        with mock.patch.object(poller, "_get_json",
                               return_value={"urls": ["https://x/1"],
                                             "dedupe_keys": ["gh:1", "wd:h:JR2", None]}):
            self.assertEqual(poller._seen_keys("http://x"), {"gh:1", "wd:h:JR2"})

    def test_computes_from_urls_for_older_backends(self):
        with mock.patch.object(poller, "_get_json",
                               return_value={"urls": ["https://boards.greenhouse.io/x/jobs/7"]}):
            self.assertEqual(poller._seen_keys("http://x"), {"gh:7"})


if __name__ == "__main__":
    unittest.main()
