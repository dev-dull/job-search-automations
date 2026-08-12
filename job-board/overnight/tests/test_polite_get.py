"""polite_get's per-host delay bookkeeping, with a fake clock — no sleeping,
no network."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sources  # noqa: E402


class PoliteGetTest(unittest.TestCase):
    def test_same_host_waits_and_different_host_does_not(self):
        clock = {"now": 1000.0}
        sleeps = []

        def fake_monotonic():
            return clock["now"]

        def fake_sleep(s):
            sleeps.append(s)
            clock["now"] += s

        fetched = []

        class _Resp:
            def read(self):
                return b"ok"
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=0):
            fetched.append(req.full_url)
            return _Resp()

        with mock.patch("time.monotonic", fake_monotonic), \
             mock.patch("time.sleep", fake_sleep), \
             mock.patch("urllib.request.urlopen", fake_urlopen):
            sources._last_hit.clear()
            sources.polite_get("https://a.example/one")
            self.assertEqual(sleeps, [])            # first hit: no wait
            clock["now"] += 0.5                     # 0.5s later, same host
            sources.polite_get("https://a.example/two")
            self.assertEqual(len(sleeps), 1)        # had to wait
            self.assertAlmostEqual(sleeps[0], sources._MIN_DELAY_S - 0.5, places=3)
            sources.polite_get("https://b.example/other")
            self.assertEqual(len(sleeps), 1)        # new host: no wait
        self.assertEqual(len(fetched), 3)

    def test_sends_the_configured_user_agent(self):
        seen = {}

        class _Resp:
            def read(self):
                return b"ok"
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=0):
            seen["ua"] = req.get_header("User-agent")
            return _Resp()

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            sources._last_hit.clear()
            sources.polite_get("https://c.example/x")
        self.assertEqual(seen["ua"], sources.USER_AGENT)


if __name__ == "__main__":
    unittest.main()
