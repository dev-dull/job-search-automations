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

_DESIRABILITY_FIELDS = ("desirability_score", "desirability_explanation", "pace_signals")


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

    def test_no_preferences_path_is_unchanged(self):
        p = _prompt(has_preferences=False)
        self.assertNotIn("desirability", p)
        self.assertNotIn("pace_signals", p)


if __name__ == "__main__":
    unittest.main()
