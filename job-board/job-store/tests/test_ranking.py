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
