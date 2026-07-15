"""ATS adapters used by the targeted-company poller.

Each adapter exposes `list_jobs(identifier: dict) -> list[dict]` where every
returned dict has at least: url, title, description. Optional fields: posted_at.

Adapters do not call the job-store backend or touch the DB. They are pure
ATS-fetch functions; orchestration lives in poller.py.
"""

from . import ashby, greenhouse, lever, rippling, workday

# `ats_platform` values come from `app.py:detect_ats()` and the plugin's
# ATS_HOSTS list. Both surfaces standardize on the short slug (ashby, not
# ashbyhq) so a single dispatch table works for either source.
ADAPTERS = {
    "greenhouse": greenhouse,
    "ashby": ashby,
    "lever": lever,
    "rippling": rippling,
    "workday": workday,
}
