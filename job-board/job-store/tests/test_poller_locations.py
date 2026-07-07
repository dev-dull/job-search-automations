"""Location filter tests (issue #53).

The allowlist's code-style tokens ("US-", "US,", "US |") must not bleed across
word edges: "AUS-Sydney" contains "us-" but is not a US location, and the
allowlist short-circuits the denylist, so the old substring match let
Australian/Russian Workday codes through the geo filter.

Run with: python3 -m unittest discover -s tests
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import poller  # noqa: E402

ALLOW = poller.DEFAULT_LOCATION_ALLOWLIST
DENY = poller.DEFAULT_LOCATION_DENYLIST


def _allowed(loc):
    return poller._location_allowed(loc, ALLOW, DENY) is None


class LocationBoundaryTest(unittest.TestCase):
    def test_aus_codes_no_longer_leak_through(self):
        # The reported bug: allowlist "US-" matched inside "AUS-…", allowed
        # before the denylist (Australia/Sydney) was consulted.
        self.assertFalse(_allowed("AUS-Sydney"))
        self.assertFalse(_allowed("AUS, NSW, Sydney"))       # "US," variant
        self.assertFalse(_allowed("AUS | Sydney"))            # "US |" variant

    def test_rus_code_does_not_hit_us_allowlist(self):
        # With "Russia" denied, the RUS- code must reach the denylist.
        self.assertIsNotNone(poller._location_allowed(
            "RUS-Moscow", ALLOW, ["Russia", "Moscow"]))

    def test_real_us_codes_still_allowed(self):
        self.assertTrue(_allowed("US-CA-Santa-Clara"))         # Workday path code
        self.assertTrue(_allowed("US, CA, Santa Clara"))       # CXS list form
        self.assertTrue(_allowed("Portland, US | Remote"))     # pipe-delimited
        self.assertTrue(_allowed("United States"))
        self.assertTrue(_allowed("Issaquah, USA"))

    def test_allowlist_override_still_beats_denylist(self):
        # Documented semantics: multi-region postings survive on a US hit.
        self.assertTrue(_allowed("United States, Canada"))

    def test_denylist_still_rejects(self):
        self.assertFalse(_allowed("Toronto, Canada"))
        self.assertFalse(_allowed("Stockholm, Sweden | Remote"))

    def test_space_anchored_phrases_keep_substring_semantics(self):
        # " UK " starts with a space — it self-anchors and must keep working.
        self.assertFalse(_allowed("London UK Office"))

    def test_empty_and_unknown_locations_allowed(self):
        self.assertTrue(_allowed(""))
        self.assertTrue(_allowed("13 Locations"))              # Workday collapse
        self.assertTrue(_allowed("Atlantis"))                  # permissive default


if __name__ == "__main__":
    unittest.main()
