"""
Ranking math.

  rank_score = fit_score * age_decay(posted_at) * platform_factor(...)

fit_score is the dominant signal by design. The other two factors are gentle
modifiers, bounded so they can nudge ordering without flipping a clearly
better-fit job below a worse one. (Previously the multipliers spanned 0.3-1.0
and 0.5-1.5 - a ~10x swing that let a stale posting on a "bad" platform outrank
a much better-fit fresh one, which is why rank drifted away from fit.)
"""

from datetime import date


# --- Age: a gentle freshness tiebreak --------------------------------------
# No decay for the first AGE_GRACE_DAYS, then a shallow linear slide to a high
# floor. Bounded to [AGE_DECAY_FLOOR, 1.0], so age reorders jobs only within
# ~25% of each other on fit and never dominates it.
AGE_GRACE_DAYS = 30
AGE_DECAY_SPAN_DAYS = 90          # grace-end -> floor
AGE_DECAY_FLOOR = 0.8
UNKNOWN_AGE_FACTOR = 0.9          # no usable date: a small nudge, not a cliff

# --- Platform: parked until there's enough outcome data --------------------
# With few applications the per-platform callback rate is dominated by the
# smoothing prior and is mostly noise, so platform_factor stays at a neutral 1.0
# until the global application count crosses this threshold.
MIN_OUTCOMES_FOR_PLATFORM_FACTOR = 30
PLATFORM_FACTOR_MIN = 0.8
PLATFORM_FACTOR_MAX = 1.25
# Bayesian smoothing strength: a platform's rate is pulled toward the GLOBAL
# rate (so an unknown platform scores a neutral 1.0) and converges to its own
# rate as applications accumulate. (The old prior pulled toward a fixed 0.2,
# which pegged every low-data platform to the max.)
SMOOTHING_PRIOR_STRENGTH = 10
DEFAULT_GLOBAL_CALLBACK_RATE = 0.10

# --- Desirability vs fit ----------------------------------------------------
# fit_score answers "is the candidate a MATCH"; desirability_score answers "does
# the candidate WANT this" (from their stated preferences). When both exist,
# rank blends them with this weight (0.5 = equal). Judgment-set for now; tune
# against applied/dismissed history (step 3). When no desirability score exists
# (no preferences configured, or a row not yet re-scored), rank uses fit alone.
DESIRABILITY_WEIGHT = 0.5


def _parse_date(d):
    if not d:
        return None
    if isinstance(d, date):
        return d
    try:
        return date.fromisoformat(str(d)[:10])
    except ValueError:
        return None


def age_decay(posted_at):
    parsed = _parse_date(posted_at)
    if parsed is None:
        return UNKNOWN_AGE_FACTOR
    days = (date.today() - parsed).days
    if days <= AGE_GRACE_DAYS:
        return 1.0
    over = days - AGE_GRACE_DAYS
    decayed = 1.0 - (over / AGE_DECAY_SPAN_DAYS) * (1.0 - AGE_DECAY_FLOOR)
    return max(AGE_DECAY_FLOOR, decayed)


def platform_factor(platform_callbacks, platform_applied, global_callback_rate):
    """Per-platform callback-rate modifier, smoothed toward the global rate.

    An unknown platform returns ~1.0 (neutral). A platform with a clearly higher
    or lower callback rate than average nudges within [MIN, MAX]. Only applied
    once there are enough outcomes (gated in compute_rank_score).
    """
    if global_callback_rate <= 0:
        global_callback_rate = DEFAULT_GLOBAL_CALLBACK_RATE
    k = SMOOTHING_PRIOR_STRENGTH
    smoothed = (platform_callbacks + k * global_callback_rate) / (platform_applied + k)
    raw = 0.5 + (smoothed / global_callback_rate) * 0.5
    return max(PLATFORM_FACTOR_MIN, min(PLATFORM_FACTOR_MAX, raw))


def compute_rank_score(fit_score, posted_at, platform_stats, discovered_at=None,
                       desirability_score=None):
    """
    platform_stats = (platform_callbacks, platform_applied, global_callback_rate,
                      global_applied)

    base = blend(fit_score, desirability_score) when both exist, else fit_score.
    Falls back to `discovered_at` when `posted_at` is missing - most plugin POSTs
    and some Workday postings don't carry an explicit publish date.
    """
    if fit_score is None:
        return None
    p_cb, p_app, g_rate, g_applied = platform_stats
    base = fit_score
    if desirability_score is not None:
        w = DESIRABILITY_WEIGHT
        base = w * desirability_score + (1.0 - w) * fit_score
    decay = age_decay(posted_at) if posted_at else age_decay(discovered_at)
    # Platform stats stay neutral until they're trustworthy (enough outcomes).
    factor = (platform_factor(p_cb, p_app, g_rate)
              if g_applied >= MIN_OUTCOMES_FOR_PLATFORM_FACTOR else 1.0)
    return round(base * decay * factor, 2)
