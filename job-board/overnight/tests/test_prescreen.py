"""Funnel behavior against a stubbed LLM: triage decides on family (not the
model's keep boolean), model failures pass through as stage='error' (report,
never paid submission), and title drops are generic-by-default."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import prescreen  # noqa: E402


def _posting(title, company="Acme", desc="x" * 200):
    return {"url": f"https://example.com/{title}", "title": title,
            "company": company, "description": desc}


class _StubLLM:
    """json_call stub keyed by model alias; triage='scorer', gates='coder'."""

    def __init__(self, triage=None, gates=None, raise_for=()):
        self.triage = triage or {}
        self.gates = gates or {}
        self.raise_for = raise_for

    def json_call(self, model, system, user, schema, max_tokens=0):
        if model in self.raise_for:
            raise prescreen.LLMError(f"{model}: boom")
        return dict(self.triage if model == "scorer" else self.gates)


class RulesStageTest(unittest.TestCase):
    def test_generic_default_drops_entry_level_only(self):
        self.assertFalse(prescreen.rules_stage("Platform Engineering Intern")[0])
        self.assertFalse(prescreen.rules_stage("New Grad SWE")[0])
        # One operator's band/family exclusions are NOT baked in:
        for t in ("Staff Software Engineer", "Engineering Manager",
                  "Frontend Developer", "Principal SRE"):
            self.assertTrue(prescreen.rules_stage(t)[0], t)

    def test_extra_drops_pattern(self):
        extra = prescreen.build_title_drops("staff, principal, head of")
        self.assertFalse(prescreen.rules_stage("Staff Engineer", extra)[0])
        self.assertFalse(prescreen.rules_stage("Head of Platform", extra)[0])
        self.assertTrue(prescreen.rules_stage("Senior DevOps Engineer", extra)[0])
        self.assertIsNone(prescreen.build_title_drops(""))


class MinDescriptionTest(unittest.TestCase):
    def test_short_description_dropped_at_rules_for_any_source(self):
        llm = _StubLLM()   # never consulted — must drop before any model call
        p = prescreen.Prescreener(llm, gates_text="-", resume_text="r")
        (d,) = p.screen_batch([_posting("DevOps Engineer", desc="too short")],
                              triage_workers=1, gate_workers=1,
                              progress=lambda m: None)
        self.assertFalse(d.keep)
        self.assertEqual(d.stage, "rules")


class FunnelTest(unittest.TestCase):
    def _screen(self, llm, postings):
        p = prescreen.Prescreener(llm, gates_text="- no gates", resume_text="r",
                                  title_drops_extra=None)
        return p.screen_batch(postings, triage_workers=1, gate_workers=1,
                              progress=lambda m: None)

    def test_triage_decides_on_family_not_keep(self):
        # Model says keep=True but the family is out — code must drop it.
        llm = _StubLLM(triage={"keep": True, "family": "devrel",
                               "level": "senior", "reason": "sounds fun"})
        (d,) = self._screen(llm, [_posting("Developer Advocate")])
        self.assertFalse(d.keep)
        self.assertEqual(d.stage, "triage")

    def test_keeper_family_reaches_gates_and_keeps(self):
        llm = _StubLLM(
            triage={"keep": False, "family": "devops", "level": "unclear"},
            gates={"gate_failures": [], "fit_sketch": 88,
                   "worth_paid_scoring": True})
        (d,) = self._screen(llm, [_posting("DevOps Engineer")])
        self.assertTrue(d.keep)
        self.assertEqual(d.stage, "kept")

    def test_gate_model_failure_is_error_stage_not_crash(self):
        llm = _StubLLM(
            triage={"keep": True, "family": "platform", "level": "senior"},
            raise_for=("coder",))
        (d,) = self._screen(llm, [_posting("Platform Engineer")])
        self.assertTrue(d.keep)                    # fail open into the report
        self.assertEqual(d.stage, "error")         # but never the paid queue


if __name__ == "__main__":
    unittest.main()
