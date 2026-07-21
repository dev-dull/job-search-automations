"""Guards the server-side desirability schema + prompt (the has_preferences
path), including the pace_signals field.

The scorer imports the `anthropic` SDK at module load; it isn't in the host test
env, so we stub it when absent (these tests only exercise the pure schema/prompt
builders, never the API).
"""

import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:  # use the real SDK when present (container/CI), else a stub
    import anthropic  # noqa: F401
except Exception:
    _stub = types.ModuleType("anthropic")
    _stub.Anthropic = object
    sys.modules["anthropic"] = _stub

import anthropic_client as ac  # noqa: E402

_DESIRABILITY_FIELDS = ("desirability_score", "desirability_explanation",
                        "pace_signals", "gate_failures")
_EVIDENCE_FIELDS = ("candidate_strong_matches", "required_qualification_misses")


def _prompt(has_preferences):
    return ac._format_user_message(
        description="x" * 200, url=None, title=None, ats_platform=None,
        growth_keywords="", has_preferences=has_preferences)


class DesirabilitySchemaTest(unittest.TestCase):
    def test_fields_present_and_required(self):
        s = ac._schema_with_desirability()
        for k in _DESIRABILITY_FIELDS:
            self.assertIn(k, s["properties"], k)
            self.assertIn(k, s["required"], k)
        self.assertEqual(s["properties"]["pace_signals"]["type"], "array")
        # closed schema: structured output must match exactly
        self.assertIs(s.get("additionalProperties"), False)

    def test_base_fit_schema_not_mutated(self):
        ac._schema_with_desirability()  # deep-copies; must not touch the shared base
        self.assertNotIn("pace_signals", ac.FIT_SCHEMA["properties"])
        self.assertNotIn("desirability_score", ac.FIT_SCHEMA["properties"])

    def test_prompt_and_schema_stay_in_lockstep(self):
        p = _prompt(has_preferences=True)
        for k in _DESIRABILITY_FIELDS:
            self.assertIn(f"{k}:", p, k)

    def test_no_preferences_path_has_no_desirability(self):
        p = _prompt(has_preferences=False)
        self.assertNotIn("desirability", p)
        self.assertNotIn("pace_signals", p)
        self.assertNotIn("gate_failures", p)

    def test_evidence_fields_in_base_schema_and_both_prompts(self):
        # #72 stage 2: evidence-weighted fit applies to every scoring call.
        for k in _EVIDENCE_FIELDS:
            self.assertIn(k, ac.FIT_SCHEMA["properties"], k)
            self.assertIn(k, ac.FIT_SCHEMA["required"], k)
            self.assertIn(f"{k}:", _prompt(True), k)
            self.assertIn(f"{k}:", _prompt(False), k)

    def test_gate_failures_is_an_object_array_requiring_evidence(self):
        s = ac._schema_with_desirability()
        item = s["properties"]["gate_failures"]["items"]
        self.assertEqual(item["type"], "object")
        self.assertEqual(sorted(item["required"]), ["evidence", "gate"])


if __name__ == "__main__":
    unittest.main()
