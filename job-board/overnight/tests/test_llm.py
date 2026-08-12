"""LlamaSwap's model circuit breaker, incl. the connect-phase timeout that
URLError-wraps a socket timeout (PR #76 round-6 finding)."""

import os
import socket
import sys
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import llm  # noqa: E402


class TimeoutPredicateTest(unittest.TestCase):
    def test_direct_and_wrapped_timeouts(self):
        self.assertTrue(llm._is_timeout(TimeoutError("read")))
        self.assertTrue(llm._is_timeout(socket.timeout("read")))
        # Connect-phase: urllib wraps socket.timeout in URLError.
        self.assertTrue(llm._is_timeout(urllib.error.URLError(socket.timeout())))
        self.assertFalse(llm._is_timeout(urllib.error.URLError("dns")))
        self.assertFalse(llm._is_timeout(ValueError("nope")))


class BreakerTest(unittest.TestCase):
    def _hang(self, exc):
        def _raise(req, timeout=0):
            raise exc
        return _raise

    def test_connect_timeout_trips_breaker_and_fails_fast(self):
        c = llm.LlamaSwap("http://x", dead_after=2)
        wrapped = urllib.error.URLError(socket.timeout("connect"))
        with mock.patch("urllib.request.urlopen", self._hang(wrapped)), \
             mock.patch("time.sleep", lambda s: None):
            # First call: retries exhaust, breaker trips at the 2nd timeout.
            with self.assertRaises(llm.LLMError):
                c.json_call("coder", "s", "u", {}, retries=2)
            self.assertIn("coder", c._dead)
            # Second call: fails fast, no request attempted.
            calls = []
            with mock.patch("urllib.request.urlopen",
                            lambda *a, **k: calls.append(1)):
                with self.assertRaises(llm.LLMError) as ctx:
                    c.json_call("coder", "s", "u", {})
            self.assertIn("declared dead", str(ctx.exception))
            self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
