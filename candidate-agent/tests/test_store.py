"""Phase-2 store tests: code sync semantics, durable counters, transcripts.
Pure stdlib; synthetic data only."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import codes  # noqa: E402
import store  # noqa: E402


def _db(self):
    path = os.path.join(tempfile.mkdtemp(), "t.db")
    return store.Store(path)


class CodeSyncTest(unittest.TestCase):
    def setUp(self):
        self.db = _db(self)
        self.codes_file = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
        self.codes_file.write("file-code-1 | File Co | url_auth\n")
        self.codes_file.close()
        self.addCleanup(os.remove, self.codes_file.name)
        self.table = codes.CodeTable(self.codes_file.name)
        self.table.attach_store(self.db)

    def _rewrite(self, text):
        import time
        with open(self.codes_file.name, "w") as f:
            f.write(text)
        os.utime(self.codes_file.name, (time.time() + 2, time.time() + 2))

    def test_file_codes_sync_and_revoke_by_removal(self):
        self.assertEqual(self.table.lookup("file-code-1").label, "File Co")
        self.assertTrue(self.table.lookup("file-code-1").url_auth)
        # Phase-1 revocation story must survive: delete the line -> gone.
        self._rewrite("other-code-2 | Other Co\n")
        self.assertIsNone(self.table.lookup("file-code-1"))
        self.assertEqual(self.table.lookup("other-code-2").label, "Other Co")

    def test_cli_codes_survive_file_sync(self):
        self.db.upsert_cli_code("cli-code-9", "Minted Co", "2099-01-01", True)
        self._rewrite("file-code-1 | File Co\n")     # triggers resync
        self.assertEqual(self.table.lookup("cli-code-9").label, "Minted Co")
        # ...and revoke via the CLI path, not the file.
        self.assertTrue(self.db.revoke_cli_code("cli-code-9"))
        self.assertIsNone(self.table.lookup("cli-code-9"))
        # revoke_cli_code must not touch file-sourced rows
        self.assertFalse(self.db.revoke_cli_code("file-code-1"))


class DurableCounterTest(unittest.TestCase):
    def test_counters_survive_restart(self):
        path = os.path.join(tempfile.mkdtemp(), "t.db")
        lim = store.DurableLimiter(store.Store(path))
        lim.record_spend("c", 3.0)
        # Simulate a process restart: fresh Store over the same file.
        lim2 = store.DurableLimiter(store.Store(path))
        self.assertAlmostEqual(lim2.store.bump("spend:c", 0, 86400), 3.0)

    def test_window_rollover(self):
        db = _db(self)
        t0 = 1_000_000_000
        self.assertEqual(db.bump("k", 5, 3600, now=t0), 5)
        self.assertEqual(db.bump("k", 1, 3600, now=t0 + 10), 6)
        self.assertEqual(db.bump("k", 1, 3600, now=t0 + 3600), 1)  # new window

    def test_limiter_api_parity(self):
        lim = store.DurableLimiter(_db(self))
        allowed = [lim.allow_request("c") for _ in range(codes.RATE_LIMIT_PER_HOUR + 2)]
        self.assertTrue(allowed[0])
        self.assertFalse(allowed[-1])
        self.assertTrue(lim.budget_ok("c"))
        lim.record_spend("c", codes.DAILY_BUDGET_USD_PER_CODE + 1)
        self.assertFalse(lim.budget_ok("c"))
        for _ in range(codes.FAILED_ATTEMPTS_PER_IP_HOUR):
            lim.record_failed_attempt("1.2.3.4")
        self.assertFalse(lim.attempts_ok("1.2.3.4"))


class RecordingTest(unittest.TestCase):
    def test_sessions_messages_and_summary(self):
        db = _db(self)
        db.upsert_cli_code("c-1", "Acme", None, False)
        db.touch_session("web:s1", "c-1", "web", ip="10.0.0.1", user_agent="UA")
        db.record_exchange("web:s1", "q1", "a1",
                           {"input_tokens": 10, "output_tokens": 5,
                            "cache_read_input_tokens": 100,
                            "cache_creation_input_tokens": 0},
                           cost_usd=0.01)
        db.touch_session("mcp:c-1:x", "c-1", "mcp",
                         client_name="claude-code", client_version="2.0")
        db.record_exchange("mcp:c-1:x", "q2", "a2", None, cost_usd=0.02)

        summary = {r["code"]: r for r in db.summary()}
        self.assertEqual(summary["c-1"]["sessions"], 2)
        self.assertEqual(summary["c-1"]["messages"], 4)
        self.assertAlmostEqual(summary["c-1"]["cost_usd"], 0.03)

        transcript = db.transcript("web:s1")
        self.assertEqual([m["role"] for m in transcript], ["user", "assistant"])
        self.assertEqual(transcript[1]["content"], "a1")

        sessions = db.sessions_for_code("c-1")
        mcp = next(s for s in sessions if s["surface"] == "mcp")
        self.assertEqual(mcp["client_name"], "claude-code")

    def test_touch_first_writer_wins_on_identity(self):
        db = _db(self)
        db.touch_session("s", "c", "mcp", client_name="claude-code")
        db.touch_session("s", "c", "mcp", client_name="someone-else")
        self.assertEqual(db.sessions_for_code("c")[0]["client_name"], "claude-code")


if __name__ == "__main__":
    unittest.main()
