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
             mock.patch("urllib.request.urlopen", fake_urlopen), \
             mock.patch.object(sources, "_MIN_DELAY_S", 2.0):
            # _MIN_DELAY_S is pinned: it's env-derived at import, and this
            # test must not inherit the operator's $SOURCE_MIN_DELAY_S.
            sources._last_hit.clear()
            sources.polite_get("https://a.example/one")
            self.assertEqual(sleeps, [])            # first hit: no wait
            clock["now"] += 0.5                     # 0.5s later, same host
            sources.polite_get("https://a.example/two")
            self.assertEqual(len(sleeps), 1)        # had to wait
            self.assertAlmostEqual(sleeps[0], 1.5, places=3)
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


class CircuitBreakerTest(unittest.TestCase):
    def test_repeated_403_blocks_host_for_the_night(self):
        import urllib.error

        def deny(req, timeout=0):
            raise urllib.error.HTTPError(req.full_url, 403, "no", {}, None)

        with mock.patch("urllib.request.urlopen", deny), \
             mock.patch("time.sleep", lambda s: None), \
             mock.patch.object(sources, "_MIN_DELAY_S", 0.0):
            sources._last_hit.clear()
            sources._refusals.clear()
            sources._blocked.clear()
            for _ in range(sources._BLOCK_AFTER):
                with self.assertRaises(urllib.error.HTTPError):
                    sources.polite_get("https://denied.example/x")
            with self.assertRaises(sources.HostBlocked):
                sources.polite_get("https://denied.example/x")
        sources._refusals.clear()
        sources._blocked.clear()


class WwrFeedVisibilityTest(unittest.TestCase):
    def test_dead_feed_lands_in_problems(self):
        from sources import wwr
        problems = []
        with mock.patch.object(wwr, "_fetch",
                               side_effect=RuntimeError("connection refused")):
            out = wwr.collect(problems=problems)
        self.assertEqual(out, [])
        self.assertEqual(len(problems), len(wwr.FEEDS))
        self.assertIn("wwr feed", problems[0])


if __name__ == "__main__":
    unittest.main()
