"""Ranking tests.

Locks in the redesign: fit dominates; age is a gentle tiebreak; platform_factor
stays neutral until there are enough outcomes.

Run with: python3 -m unittest discover -s tests
"""

import os
import sys
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ranking  # noqa: E402

# The pending-desirability discount keys off PREFERENCES_PATH; keep the
# baseline tests deterministic regardless of the dev machine's env.
os.environ.pop("PREFERENCES_PATH", None)
os.environ.pop(ranking.COMPANY_ADJUSTMENTS_ENV, None)

# Few outcomes -> platform_factor parked (g_applied below the threshold).
PARKED = (0, 0, 0.0, 9)
# Enough outcomes -> platform_factor active.
ACTIVE = (0, 0, 0.10, 50)


def _iso(days_ago):
    return (date.today() - timedelta(days=days_ago)).isoformat()


class AgeDecayTest(unittest.TestCase):
    def test_grace_period_no_decay(self):
        self.assertEqual(ranking.age_decay(_iso(0)), 1.0)
        self.assertEqual(ranking.age_decay(_iso(ranking.AGE_GRACE_DAYS)), 1.0)

    def test_bounded_and_monotonic(self):
        vals = [ranking.age_decay(_iso(d)) for d in (0, 30, 60, 90, 120, 300)]
        self.assertTrue(all(ranking.AGE_DECAY_FLOOR <= v <= 1.0 for v in vals))
        self.assertEqual(vals, sorted(vals, reverse=True))      # non-increasing
        self.assertEqual(ranking.age_decay(_iso(300)), ranking.AGE_DECAY_FLOOR)

    def test_missing_date_is_a_small_nudge(self):
        self.assertEqual(ranking.age_decay(None), ranking.UNKNOWN_AGE_FACTOR)
        self.assertGreaterEqual(ranking.UNKNOWN_AGE_FACTOR, 0.85)


class FitDominanceTest(unittest.TestCase):
    def test_high_fit_old_beats_low_fit_fresh(self):
        # The exact inversion the old formula produced: a strong-fit older job
        # must outrank a weak-fit brand-new one.
        strong_old = ranking.compute_rank_score(90, _iso(60), PARKED)
        weak_fresh = ranking.compute_rank_score(40, _iso(0), PARKED)
        self.assertGreater(strong_old, weak_fresh)

    def test_age_cannot_flip_a_big_fit_gap(self):
        # Even oldest-vs-newest, a 20-point fit lead survives.
        best_decay = ranking.compute_rank_score(70, _iso(0), PARKED)
        worst_decay = ranking.compute_rank_score(50, _iso(300), PARKED)
        self.assertGreater(worst_decay, 0)
        self.assertGreater(ranking.compute_rank_score(70, _iso(300), PARKED),
                           ranking.compute_rank_score(50, _iso(0), PARKED))


class DesirabilityBlendTest(unittest.TestCase):
    def test_no_desirability_falls_back_to_fit(self):
        with_none = ranking.compute_rank_score(60, _iso(0), PARKED)
        explicit = ranking.compute_rank_score(60, _iso(0), PARKED,
                                               desirability_score=None)
        self.assertEqual(with_none, explicit)
        self.assertEqual(with_none, 60.0)   # fresh, parked platform -> fit

    def test_high_desire_lifts_a_modest_fit_job(self):
        # A so-so fit you really want should rank above the same fit with no
        # desirability signal.
        wanted = ranking.compute_rank_score(40, _iso(0), PARKED, desirability_score=90)
        plain = ranking.compute_rank_score(40, _iso(0), PARKED)
        self.assertGreater(wanted, plain)

    def test_low_desire_sinks_a_high_fit_job(self):
        # Qualified but uninterested -> ranks below a balanced one.
        unwanted = ranking.compute_rank_score(90, _iso(0), PARKED, desirability_score=20)
        balanced = ranking.compute_rank_score(60, _iso(0), PARKED, desirability_score=60)
        self.assertLess(unwanted, balanced)

    def test_blend_is_the_configured_weight(self):
        w = ranking.DESIRABILITY_WEIGHT
        got = ranking.compute_rank_score(40, _iso(0), PARKED, desirability_score=80)
        self.assertAlmostEqual(got, w * 80 + (1 - w) * 40, places=2)


class GateFloorTest(unittest.TestCase):
    """#72 phase 1: a failed hard deal-breaker floors rank into a low band —
    no amount of fit or desirability buys back a disqualification."""

    def test_gated_high_fit_ranks_below_everything_ungated(self):
        gated_best = ranking.compute_rank_score(100, _iso(0), PARKED,
                                                desirability_score=100, gated=True)
        ungated_worst = ranking.compute_rank_score(
            20, _iso(300), PARKED, desirability_score=1)
        self.assertLess(gated_best, ungated_worst)

    def test_gated_band_is_fit_ordered(self):
        hi = ranking.compute_rank_score(90, _iso(0), PARKED, gated=True)
        lo = ranking.compute_rank_score(40, _iso(0), PARKED, gated=True)
        self.assertGreater(hi, lo)
        self.assertLessEqual(hi, 100 * ranking.GATED_RANK_FACTOR)

    def test_gate_ignores_age_and_desirability(self):
        a = ranking.compute_rank_score(80, _iso(0), PARKED,
                                       desirability_score=95, gated=True)
        b = ranking.compute_rank_score(80, _iso(300), PARKED,
                                       desirability_score=5, gated=True)
        self.assertEqual(a, b)

    def test_default_is_ungated(self):
        self.assertEqual(ranking.compute_rank_score(80, _iso(0), PARKED), 80.0)


class PendingDesirabilityTest(unittest.TestCase):
    """A missing desirability score with preferences configured means the row
    hasn't been evaluated on the axis that drives ranking — discount it rather
    than let it compete on fit alone."""

    def setUp(self):
        os.environ["PREFERENCES_PATH"] = "/nonexistent/prefs.md"
        self.addCleanup(os.environ.pop, "PREFERENCES_PATH", None)

    def test_pending_rows_are_discounted(self):
        pending = ranking.compute_rank_score(92, _iso(0), PARKED)
        self.assertEqual(pending,
                         round(92 * ranking.PENDING_DESIRABILITY_FACTOR, 2))

    def test_evaluated_row_outranks_pending_at_same_fit(self):
        pending = ranking.compute_rank_score(80, _iso(0), PARKED)
        evaluated = ranking.compute_rank_score(80, _iso(0), PARKED,
                                               desirability_score=70)
        self.assertGreater(evaluated, pending)

    def test_no_preferences_means_no_discount(self):
        os.environ.pop("PREFERENCES_PATH", None)
        self.assertEqual(ranking.compute_rank_score(92, _iso(0), PARKED), 92.0)


class ClampTest(unittest.TestCase):
    def test_platform_factor_cannot_push_rank_above_100(self):
        # A hot platform (factor up to 1.25) on a high base must not produce
        # an above-scale rank (one real row hit 117).
        hot = (100, 100, 0.10, 50)
        self.assertLessEqual(
            ranking.compute_rank_score(95, _iso(0), hot, desirability_score=95),
            100.0)


class CompanyAdjustmentTest(unittest.TestCase):
    """COMPANY_ADJUSTMENTS_PATH: deterministic per-company rank nudges from a
    user-maintained file (outside-view reality the JD can't provide)."""

    def _notes(self, text):
        import tempfile
        f = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
        f.write(text)
        f.close()
        os.environ[ranking.COMPANY_ADJUSTMENTS_ENV] = f.name
        self.addCleanup(os.environ.pop, ranking.COMPANY_ADJUSTMENTS_ENV, None)
        self.addCleanup(os.remove, f.name)

    def test_exact_and_substring_match(self):
        self._notes("# outside-view notes\nUnqork: -25  # layoffs\nFivetran: +10\n")
        self.assertEqual(ranking.company_adjustment("Unqork"), -25.0)
        self.assertEqual(ranking.company_adjustment("unqork"), -25.0)
        self.assertEqual(
            ranking.company_adjustment("Fivetran (merged with dbt Labs)"), 10.0)
        self.assertEqual(ranking.company_adjustment("SomeoneElse"), 0.0)

    def test_adjustment_moves_rank(self):
        self._notes("BadCo: -30\n")
        plain = ranking.compute_rank_score(80, _iso(0), PARKED,
                                           desirability_score=80)
        nudged = ranking.compute_rank_score(80, _iso(0), PARKED,
                                            desirability_score=80,
                                            company="BadCo")
        self.assertEqual(nudged, plain - 30)

    def test_no_file_is_neutral(self):
        os.environ.pop(ranking.COMPANY_ADJUSTMENTS_ENV, None)
        self.assertEqual(ranking.company_adjustment("Anything"), 0.0)


class PlatformFactorTest(unittest.TestCase):
    def test_parked_below_threshold(self):
        # With few outcomes, rank == fit * age_decay (factor == 1.0).
        rank = ranking.compute_rank_score(80, _iso(0), PARKED)
        self.assertEqual(rank, round(80 * 1.0 * 1.0, 2))

    def test_unknown_platform_is_neutral_when_active(self):
        # A platform with no history smooths to ~1.0, not the max.
        f = ranking.platform_factor(0, 0, 0.10)
        self.assertAlmostEqual(f, 1.0, places=2)

    def test_active_factor_is_bounded(self):
        hi = ranking.platform_factor(100, 100, 0.10)   # callback rate way above avg
        lo = ranking.platform_factor(0, 100, 0.10)     # way below
        self.assertLessEqual(hi, ranking.PLATFORM_FACTOR_MAX)
        self.assertGreaterEqual(lo, ranking.PLATFORM_FACTOR_MIN)

    def test_none_fit_score(self):
        self.assertIsNone(ranking.compute_rank_score(None, _iso(0), PARKED))


if __name__ == "__main__":
    unittest.main()
