"""Engine tests with a stubbed Anthropic client. No API calls, no PII."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import codes  # noqa: E402
import corpus as corpus_mod  # noqa: E402
import engine as engine_mod  # noqa: E402


def _corpus():
    root = tempfile.mkdtemp()
    with open(os.path.join(root, "profile.md"), "w") as f:
        f.write("---\ntitle: Profile\n---\nJordan Sample, platform engineer.\n")
    c = corpus_mod.Corpus(root=root, denylist_path=None)
    c.load_or_die()
    return c


class _FakeUsage(dict):
    pass


class _FakeResponse:
    def __init__(self, text):
        class B:  # minimal content block
            type = "text"
        b = B()
        b.text = text
        self.content = [b]
        self.usage = {"input_tokens": 100, "output_tokens": 50,
                      "cache_read_input_tokens": 1000,
                      "cache_creation_input_tokens": 0}


class _FakeMessages:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeResponse("stub answer")


class _FakeClient:
    def __init__(self):
        self.messages = _FakeMessages()


class CostTest(unittest.TestCase):
    def test_cost_weighting(self):
        usage = {"input_tokens": 1_000_000, "output_tokens": 0,
                 "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        p_in, _ = engine_mod._prices()
        self.assertAlmostEqual(engine_mod.usage_cost_usd(usage), p_in)
        usage = {"input_tokens": 0, "output_tokens": 0,
                 "cache_read_input_tokens": 1_000_000,
                 "cache_creation_input_tokens": 0}
        self.assertAlmostEqual(engine_mod.usage_cost_usd(usage), p_in * 0.1)


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.limiter = codes.Limiter()
        self.engine = engine_mod.Engine(_corpus(), self.limiter)
        self.engine._client = _FakeClient()

    def test_system_blocks_are_cached_and_stable(self):
        s1, s2 = self.engine._system(), self.engine._system()
        self.assertEqual(s1, s2)                       # byte-stable = cacheable
        for block in s1:
            self.assertEqual(block["cache_control"], {"type": "ephemeral"})
        self.assertIn("Jordan Sample", s1[1]["text"])
        self.assertNotIn("{", s1[0]["text"].replace("{", "", 0))  # no interpolation markers

    def test_answer_records_spend(self):
        out = self.engine.answer("code-1", "What do they do?")
        self.assertEqual(out, "stub answer")
        self.assertTrue(self.limiter._spend["code-1"].count > 0)

    def test_continuity_threads_history(self):
        self.engine.answer("code-1", "Q1", continuity_key="k")
        self.engine.answer("code-1", "Q2", continuity_key="k")
        second_call = self.engine._client.messages.calls[1]
        roles = [m["role"] for m in second_call["messages"]]
        self.assertEqual(roles, ["user", "assistant", "user"])

    def test_budget_exhausted_short_circuits(self):
        self.limiter.record_spend("code-1", codes.DAILY_BUDGET_USD_PER_CODE + 1)
        out = self.engine.answer("code-1", "Q")
        self.assertIn("budget", out.lower())
        self.assertEqual(self.engine._client.messages.calls, [])


if __name__ == "__main__":
    unittest.main()
