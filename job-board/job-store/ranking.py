"""
Ranking math. See AUTOMATION_PLAN.md §Ranking for the source formula and the
rationale behind each constant.

  rank_score = fit_score * age_decay(posted_at) * platform_factor(...)
"""

from datetime import date


# Tunable constants. See AUTOMATION_PLAN.md for rationale; expect to revisit
# once ≥30 outcomes exist in the system.
AGE_DECAY_DAYS = 45
AGE_DECAY_FLOOR = 0.3
UNKNOWN_AGE_FACTOR = 0.7

PLATFORM_FACTOR_MIN = 0.5
PLATFORM_FACTOR_MAX = 1.5

# Bayesian smoothing prior — pretends every platform starts with 2 callbacks
# out of 10 applications, dragging low-n platforms toward the global rate.
SMOOTHING_PRIOR_CALLBACKS = 2
SMOOTHING_PRIOR_APPLICATIONS = 10

# Used when there is no historical data at all (empty store).
DEFAULT_GLOBAL_CALLBACK_RATE = 0.10


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
    if days < 0:
        return 1.0
    return max(AGE_DECAY_FLOOR, min(1.0, 1.0 - days / AGE_DECAY_DAYS))


def platform_factor(platform_callbacks, platform_applied, global_callback_rate):
    if global_callback_rate <= 0:
        global_callback_rate = DEFAULT_GLOBAL_CALLBACK_RATE
    smoothed = (platform_callbacks + SMOOTHING_PRIOR_CALLBACKS) / (
        platform_applied + SMOOTHING_PRIOR_APPLICATIONS
    )
    raw = 0.5 + (smoothed / global_callback_rate) * 0.5
    return max(PLATFORM_FACTOR_MIN, min(PLATFORM_FACTOR_MAX, raw))


def compute_rank_score(fit_score, posted_at, platform_stats, discovered_at=None):
    """
    platform_stats = (platform_callbacks, platform_applied, global_callback_rate)

    Falls back to `discovered_at` when `posted_at` is missing — most plugin
    POSTs and some Workday postings don't carry an explicit publish date, and
    without a fallback those rows freeze at `UNKNOWN_AGE_FACTOR` forever
    instead of aging out alongside everything else.
    """
    if fit_score is None:
        return None
    p_cb, p_app, g_rate = platform_stats
    decay = age_decay(posted_at) if posted_at else age_decay(discovered_at)
    factor = platform_factor(p_cb, p_app, g_rate)
    return round(fit_score * decay * factor, 2)
