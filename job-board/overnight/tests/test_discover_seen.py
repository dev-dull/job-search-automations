"""The seen-set is the spend guardrail: prefer dedupe_keys, fall back to
computing from urls (older backends), never silently empty on a fallback."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discover  # noqa: E402


class SeenKeysTest(unittest.TestCase):
    def test_prefers_dedupe_keys(self):
        got = discover.seen_keys({"dedupe_keys": ["gh:1", "ashby:x"],
                                  "urls": ["https://example.com/ignored"]})
        self.assertEqual(got, {"gh:1", "ashby:x"})

    def test_falls_back_to_urls(self):
        got = discover.seen_keys(
            {"urls": ["https://boards.greenhouse.io/acme/jobs/123",
                      "https://jobs.ashbyhq.com/beta/senior-eng"]})
        self.assertEqual(len(got), 2)
        self.assertTrue(any(k.startswith("gh:") for k in got))

    def test_empty_payload_is_empty_not_crash(self):
        self.assertEqual(discover.seen_keys({}), set())
        self.assertEqual(discover.seen_keys({"dedupe_keys": None, "urls": None}), set())


if __name__ == "__main__":
    unittest.main()
