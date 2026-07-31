"""Access-code table and limiter tests. Pure stdlib; synthetic codes only."""

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import codes  # noqa: E402


def _write(text):
    f = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
    f.write(text)
    f.close()
    return f.name


class ParseTest(unittest.TestCase):
    def test_full_line(self):
        c = codes._parse_line(
            "maple-K7RT-hazel | Acme Corp | expires=2099-01-01 | url_auth | note=met at conf")
        self.assertEqual((c.code, c.label, c.expires, c.url_auth, c.note),
                         ("maple-K7RT-hazel", "Acme Corp", "2099-01-01",
                          True, "met at conf"))

    def test_comments_blanks_and_junk(self):
        self.assertIsNone(codes._parse_line("# comment"))
        self.assertIsNone(codes._parse_line(""))
        self.assertIsNone(codes._parse_line("no-pipe-at-all"))

    def test_usable(self):
        self.assertTrue(codes.Code("x", "L").usable())
        self.assertFalse(codes.Code("x", "L", revoked=True).usable())
        self.assertFalse(codes.Code("x", "L", expires="2000-01-01").usable())
        # Unparseable expiry fails closed.
        self.assertFalse(codes.Code("x", "L", expires="soonish").usable())


class TableTest(unittest.TestCase):
    def test_lookup_and_revocation_via_rewrite(self):
        path = _write("alpha-1 | A\nbravo-2 | B | url_auth\n")
        self.addCleanup(os.remove, path)
        t = codes.CodeTable(path)
        self.assertEqual(t.lookup("alpha-1").label, "A")
        self.assertTrue(t.lookup("bravo-2").url_auth)
        self.assertIsNone(t.lookup("charlie-3"))
        # Rewrite the file (revocation): must be picked up via mtime.
        time.sleep(0.01)
        with open(path, "w") as f:
            f.write("bravo-2 | B | url_auth\n")
        os.utime(path, (time.time() + 1, time.time() + 1))
        self.assertIsNone(t.lookup("alpha-1"))
        self.assertEqual(t.status("alpha-1"), "unknown")

    def test_status_expired_vs_unknown(self):
        path = _write("old-1 | Old | expires=2000-01-01\n")
        self.addCleanup(os.remove, path)
        t = codes.CodeTable(path)
        self.assertIsNone(t.lookup("old-1"))
        self.assertEqual(t.status("old-1"), "expired")
        self.assertEqual(t.status("nope"), "unknown")


class LimiterTest(unittest.TestCase):
    def test_rate_limit_window(self):
        lim = codes.Limiter()
        allowed = [lim.allow_request("c") for _ in range(codes.RATE_LIMIT_PER_HOUR + 5)]
        self.assertTrue(all(allowed[:codes.RATE_LIMIT_PER_HOUR]))
        self.assertFalse(allowed[-1])

    def test_budget(self):
        lim = codes.Limiter()
        self.assertTrue(lim.budget_ok("c"))
        lim.record_spend("c", codes.DAILY_BUDGET_USD_PER_CODE + 1)
        self.assertFalse(lim.budget_ok("c"))
        # Global kill-switch trips for OTHER codes too.
        lim2 = codes.Limiter()
        lim2.record_spend("x", codes.DAILY_BUDGET_USD_GLOBAL + 1)
        self.assertFalse(lim2.budget_ok("y"))

    def test_failed_attempts(self):
        lim = codes.Limiter()
        for _ in range(codes.FAILED_ATTEMPTS_PER_IP_HOUR):
            self.assertTrue(lim.attempts_ok("1.2.3.4"))
            lim.record_failed_attempt("1.2.3.4")
        self.assertFalse(lim.attempts_ok("1.2.3.4"))
        self.assertTrue(lim.attempts_ok("5.6.7.8"))     # other IPs unaffected


if __name__ == "__main__":
    unittest.main()
