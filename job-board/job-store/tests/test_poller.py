"""Poller exit-code policy tests.

The poller must not fail the whole run (and trigger Kubernetes retries) just
because one ATS target is misconfigured or flaky. See poller._exit_code.

Run with: python3 -m unittest discover -s tests
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import poller  # noqa: E402


def _summary(*, skipped=False, adapter_error=False, errors=0):
    return {"skipped": 1 if skipped else 0,
            "adapter_error": adapter_error,
            "errors": errors}


class ExitCodeTest(unittest.TestCase):
    def test_no_targets(self):
        self.assertEqual(poller._exit_code([]), 0)

    def test_all_clean(self):
        self.assertEqual(poller._exit_code([_summary(), _summary(), _summary()]), 0)

    def test_one_bad_target_among_many_is_non_fatal(self):
        # The reported case: one 404ing target, the rest fine -> exit 0.
        s = [_summary(), _summary(adapter_error=True, errors=1), _summary()]
        self.assertEqual(poller._exit_code(s), 0)

    def test_all_adapters_failed_is_fatal(self):
        s = [_summary(adapter_error=True, errors=1),
             _summary(adapter_error=True, errors=1)]
        self.assertEqual(poller._exit_code(s), 1)

    def test_single_target_run_that_fails_is_fatal(self):
        # `poller.py --target N` against a broken target should report failure.
        self.assertEqual(poller._exit_code([_summary(adapter_error=True, errors=1)]), 1)

    def test_skipped_targets_dont_count_as_attempted(self):
        # All adapter-having targets failed, plus some no-adapter skips -> fatal.
        s = [_summary(skipped=True), _summary(adapter_error=True, errors=1)]
        self.assertEqual(poller._exit_code(s), 1)
        # Only skips, nothing actually attempted -> not a failure.
        self.assertEqual(poller._exit_code([_summary(skipped=True)]), 0)

    def test_per_posting_errors_are_non_fatal(self):
        # Listing fetched fine (no adapter_error) but some postings errored.
        self.assertEqual(poller._exit_code([_summary(errors=3)]), 0)


if __name__ == "__main__":
    unittest.main()
