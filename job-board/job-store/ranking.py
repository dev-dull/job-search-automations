"""
Ranking math.

  rank_score = fit_score * age_decay(posted_at) * platform_factor(...)

fit_score is the dominant signal by design. The other two factors are gentle
modifiers, bounded so they can nudge ordering without flipping a clearly
better-fit job below a worse one. (Previously the multipliers spanned 0.3-1.0
and 0.5-1.5 - a ~10x swing that let a stale posting on a "bad" platform outrank
a much better-fit fresh one, which is why rank drifted away from fit.)
"""

import os
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
# the candidate WANT this" (from their stated preferences). Six weeks of real
# application decisions showed desirability doing most of the ranking work among
# gate survivors — fit mostly just qualifies — so the blend leans desirability.
DESIRABILITY_WEIGHT = 0.7

# When preferences ARE configured but a row has no desirability score yet, the
# row simply hasn't been evaluated on the axis that does most of the ranking —
# treating it as fit-only let unevaluated rows outrank fully-evaluated ones
# (one hit rank 117 via the platform factor). Pending rows rank at a discount
# until a re-score fills the axis in. Without preferences configured the whole
# board is fit-only and no discount applies.
PENDING_DESIRABILITY_FACTOR = 0.6

# --- Company adjustments (rank-time, #70 stopgap) ---------------------------
# Outside-view company reality (layoff patterns, review-site signals, funding
# state) is where JD-text desirability diverges hardest from the user's actual
# judgment. COMPANY_ADJUSTMENTS_PATH points at a user-maintained file in their
# PRIVATE repo (never committed here) of per-company rank nudges:
#
#     # comment lines start with '#'
#     Unqork: -25        # layoff rounds; comp band below floor
#     Tailscale: +10
#
# Matching is case-insensitive; a key also matches when it's a substring of the
# stored company name ("Fivetran" matches "Fivetran (merged with ...)"). The
# adjustment shifts the blended base before decay, clamped to 0-100. Reloaded
# when the file's mtime changes, so edits show up on the next page load.
COMPANY_ADJUSTMENTS_ENV = "COMPANY_ADJUSTMENTS_PATH"
_adjustments_cache = {"path": None, "mtime": None, "table": {}}

# --- Gates (#72 phase 1) --------------------------------------------------
# A posting that fails a HARD deal-breaker from the preferences profile (level
# band, location/timezone, onsite, excluded role family, missing required
# qualification) is floored into a low band rather than averaged: no amount of
# fit buys back a disqualification. The band stays fit-ordered (rank = fit *
# factor, ~0-5) so gated rows cluster at the bottom of the board where a
# mis-fired gate is easy to spot and review — floored, never hidden.
GATED_RANK_FACTOR = 0.05


def _load_company_adjustments():
    path = os.environ.get(COMPANY_ADJUSTMENTS_ENV)
    if not path or not os.path.exists(path):
        return {}
    mtime = os.path.getmtime(path)
    cache = _adjustments_cache
    if cache["path"] == path and cache["mtime"] == mtime:
        return cache["table"]
    table = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            name, _, value = line.rpartition(":")
            value = value.split("#", 1)[0].strip()
            try:
                table[name.strip().lower()] = float(value)
            except ValueError:
                continue
    cache.update(path=path, mtime=mtime, table=table)
    return table


def company_adjustment(company):
    """Rank nudge for a company from COMPANY_ADJUSTMENTS_PATH (0 if none)."""
    if not company:
        return 0.0
    table = _load_company_adjustments()
    if not table:
        return 0.0
    needle = company.strip().lower()
    if needle in table:
        return table[needle]
    for key, value in table.items():
        if key in needle:
            return value
    return 0.0


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
                       desirability_score=None, gated=False, company=None):
    """
    platform_stats = (platform_callbacks, platform_applied, global_callback_rate,
                      global_applied)

    base = blend(fit_score, desirability_score) when both exist. A missing
    desirability score with preferences configured means the row is pending
    evaluation and ranks at a discount, not on fit alone. Falls back to
    `discovered_at` when `posted_at` is missing - most plugin POSTs and some
    Workday postings don't carry an explicit publish date.

    gated=True (the posting failed a hard deal-breaker, #72) floors the rank
    into the GATED_RANK_FACTOR band regardless of fit/desirability/age.
    `company` enables the COMPANY_ADJUSTMENTS_PATH nudge. Result is clamped to
    0-100 so the platform factor can never push a rank above scale.
    """
    if fit_score is None:
        return None
    if gated:
        return round(fit_score * GATED_RANK_FACTOR, 2)
    p_cb, p_app, g_rate, g_applied = platform_stats
    if desirability_score is not None:
        w = DESIRABILITY_WEIGHT
        base = w * desirability_score + (1.0 - w) * fit_score
    elif os.environ.get("PREFERENCES_PATH"):
        base = fit_score * PENDING_DESIRABILITY_FACTOR
    else:
        base = fit_score
    base = max(0.0, min(100.0, base + company_adjustment(company)))
    decay = age_decay(posted_at) if posted_at else age_decay(discovered_at)
    # Platform stats stay neutral until they're trustworthy (enough outcomes).
    factor = (platform_factor(p_cb, p_app, g_rate)
              if g_applied >= MIN_OUTCOMES_FOR_PLATFORM_FACTOR else 1.0)
    return round(min(100.0, base * decay * factor), 2)
