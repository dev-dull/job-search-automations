"""SQLite schema and query helpers for the job store."""

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


# Default keeps the DB next to the code so local dev (`flask run`) is unchanged.
# Set JOBS_DB_PATH to relocate it — e.g. the container points it at a writable
# mounted volume (/data/jobs.db) so state survives pod restarts and the rest of
# the root filesystem can stay read-only. WAL/SHM siblings land in the same dir.
DB_PATH = Path(os.environ.get("JOBS_DB_PATH") or (Path(__file__).parent / "jobs.db"))

# Canonical ats_platform labels. Different ingestion eras wrote different
# spellings for the same ATS ('ashbyhq' from early plugin builds vs 'ashby'
# from the poller), which splits per-platform ranking stats and breaks
# platform-keyed logic (#67). Normalized on every write and migrated once.
_PLATFORM_ALIASES = {
    "ashbyhq": "ashby",
}


def normalize_platform(ats_platform):
    if not ats_platform:
        return ats_platform
    p = ats_platform.strip().lower()
    return _PLATFORM_ALIASES.get(p, p)


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    url TEXT NOT NULL UNIQUE,
    company TEXT,
    title TEXT,
    description TEXT,
    ats_platform TEXT,
    posted_at DATE,
    discovered_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    discovered_by TEXT,
    status TEXT NOT NULL DEFAULT 'discovered',
    fit_score REAL,
    desirability_score REAL,
    analysis_json TEXT,
    rank_score REAL,
    applied_at TIMESTAMP,
    branch TEXT,
    dedupe_key TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_rank ON jobs(rank_score DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_ats ON jobs(ats_platform);
-- idx_jobs_dedupe_key is created after the migration step in init_db so it
-- exists for both fresh DBs (column came in via CREATE TABLE) and migrated
-- DBs (column added by ALTER TABLE).

CREATE TABLE IF NOT EXISTS outcomes (
    job_id INTEGER PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    referral INTEGER NOT NULL DEFAULT 0,
    callback INTEGER NOT NULL DEFAULT 0,
    callback_at DATE,
    ghosted INTEGER NOT NULL DEFAULT 0,
    rejected INTEGER NOT NULL DEFAULT 0,
    offer INTEGER NOT NULL DEFAULT 0,
    notes TEXT,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS company_targets (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    careers_url TEXT NOT NULL UNIQUE,
    ats_platform TEXT,
    ats_identifier TEXT,
    deny_list TEXT NOT NULL DEFAULT '[]',
    last_polled_at TIMESTAMP,
    last_polled_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_conn()
    conn.executescript(SCHEMA)
    # Migration: older DBs were created before `dedupe_key` existed. Add the
    # column + index if missing, then backfill from existing URLs.
    existing_cols = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    if "dedupe_key" not in existing_cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN dedupe_key TEXT")
    # desirability_score (the "do I want it" axis) was added after fit_score.
    if "desirability_score" not in existing_cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN desirability_score REAL")
    # Either path (fresh DB or migrated DB) reaches here with the column
    # in place; create the index unconditionally.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_dedupe_key ON jobs(dedupe_key)")
    # outcomes.rejected was added after the original schema. Older DBs need
    # the column added explicitly.
    existing_outcome_cols = {r["name"] for r in conn.execute("PRAGMA table_info(outcomes)").fetchall()}
    if "rejected" not in existing_outcome_cols:
        conn.execute("ALTER TABLE outcomes ADD COLUMN rejected INTEGER NOT NULL DEFAULT 0")
    # Normalize drifted ats_platform labels everywhere (#67) — early plugin
    # builds wrote 'ashbyhq' where the poller writes 'ashby', splitting
    # per-platform ranking stats across two labels.
    for alias, canonical in _PLATFORM_ALIASES.items():
        conn.execute("UPDATE jobs SET ats_platform = ? WHERE ats_platform = ?",
                     (canonical, alias))
        conn.execute("UPDATE company_targets SET ats_platform = ? WHERE ats_platform = ?",
                     (canonical, alias))

    # Re-key EVERY row whose dedupe_key doesn't match the current format (#66).
    # dedupe_key is derived data and its format has changed over time (plain
    # canonical URLs -> platform-prefixed keys); the old NULL-only backfill
    # left stale-format keys in place, so a re-encountered posting missed the
    # dedupe lookup and resurrected as a fresh row — bypassing the #51
    # dismissed/applied protection. Idempotent and cheap at this table size.
    from urls import compute_dedupe_key
    for r in conn.execute("SELECT id, url, dedupe_key FROM jobs").fetchall():
        key = compute_dedupe_key(r["url"])
        if key != r["dedupe_key"]:
            conn.execute("UPDATE jobs SET dedupe_key = ? WHERE id = ?", (key, r["id"]))

    # Merge rows the re-key just made collide (the resurrection duplicates).
    # Keep the row carrying workflow state (applied/closed — the user's
    # decision), else the oldest; drop the rest. If MULTIPLE rows carry
    # workflow state, leave them alone rather than destroy user data.
    dupes = conn.execute(
        "SELECT dedupe_key FROM jobs WHERE dedupe_key IS NOT NULL "
        "GROUP BY dedupe_key HAVING COUNT(*) > 1"
    ).fetchall()
    for d in dupes:
        rows = conn.execute(
            "SELECT id, status FROM jobs WHERE dedupe_key = ? ORDER BY id",
            (d["dedupe_key"],),
        ).fetchall()
        with_state = [r for r in rows if r["status"] in ("applied", "closed")]
        if len(with_state) > 1:
            continue
        keep = (with_state[0] if with_state else rows[0])["id"]
        conn.execute("DELETE FROM jobs WHERE dedupe_key = ? AND id != ?",
                     (d["dedupe_key"], keep))

    # Flush the migration writes first: journal_mode can't change inside the
    # implicit transaction the UPDATEs above open.
    conn.commit()
    conn.execute("PRAGMA journal_mode = WAL")
    conn.commit()
    conn.close()


@contextmanager
def cursor():
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

def upsert_job(*, url, company, title, description, ats_platform, posted_at,
               discovered_by, fit_score, analysis_json, desirability_score=None):
    """
    Insert if the posting (by dedupe_key) is new; otherwise update fit/analysis
    fields without clobbering applied/branch/status. Returns the row id.

    Lookups go through `dedupe_key` rather than `url` so that two URLs pointing
    at the same Greenhouse posting (embed wrapper vs. boards-api host) merge
    into a single row.
    """
    from urls import compute_dedupe_key
    dk = compute_dedupe_key(url)
    ats_platform = normalize_platform(ats_platform)
    with cursor() as conn:
        existing = conn.execute(
            "SELECT id FROM jobs WHERE dedupe_key = ? OR url = ?", (dk, url)
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE jobs SET
                    company = COALESCE(?, company),
                    title = COALESCE(?, title),
                    description = COALESCE(?, description),
                    ats_platform = COALESCE(?, ats_platform),
                    posted_at = COALESCE(?, posted_at),
                    discovered_by = COALESCE(?, discovered_by),
                    fit_score = COALESCE(?, fit_score),
                    desirability_score = COALESCE(?, desirability_score),
                    analysis_json = COALESCE(?, analysis_json),
                    dedupe_key = COALESCE(dedupe_key, ?)
                WHERE id = ?
                """,
                (company, title, description, ats_platform, posted_at,
                 discovered_by, fit_score, desirability_score, analysis_json,
                 dk, existing["id"]),
            )
            return existing["id"]
        cur = conn.execute(
            """
            INSERT INTO jobs (url, company, title, description, ats_platform,
                              posted_at, discovered_by, fit_score,
                              desirability_score, analysis_json,
                              status, dedupe_key)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'discovered', ?)
            """,
            (url, company, title, description, ats_platform, posted_at,
             discovered_by, fit_score, desirability_score, analysis_json, dk),
        )
        return cur.lastrowid


def get_job(job_id):
    with cursor() as conn:
        return conn.execute(
            """
            SELECT j.*, o.referral, o.callback, o.callback_at, o.ghosted, o.rejected, o.offer, o.notes
            FROM jobs j
            LEFT JOIN outcomes o ON o.job_id = j.id
            WHERE j.id = ?
            """,
            (job_id,),
        ).fetchone()


def get_job_by_url(url):
    """Lookup by dedupe key derived from `url`. Same posting served from
    different hosts (embed wrapper vs. Greenhouse-direct) lands on the same
    row. Falls back to URL match for rows that haven't been backfilled yet."""
    from urls import compute_dedupe_key
    dk = compute_dedupe_key(url)
    with cursor() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE dedupe_key = ? OR url = ?", (dk, url)
        ).fetchone()
        return dict(row) if row else None


def list_jobs(*, statuses=None, order="rank_score DESC NULLS LAST"):
    sql = """
        SELECT j.*, o.referral, o.callback, o.callback_at, o.ghosted, o.rejected, o.offer, o.notes
        FROM jobs j
        LEFT JOIN outcomes o ON o.job_id = j.id
    """
    params = []
    if statuses:
        placeholders = ",".join(["?"] * len(statuses))
        sql += f" WHERE j.status IN ({placeholders})"
        params.extend(statuses)
    sql += f" ORDER BY {order}"
    with cursor() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def update_status(job_id, status):
    with cursor() as conn:
        conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (status, job_id))


def update_rank_score(job_id, rank_score, status=None):
    """Update rank (and optionally status). The status write only applies to
    rows still in the discovery pipeline (discovered/ranked): a re-score of a
    job the user already acted on must never demote `applied` back to `ranked`
    or resurrect a dismissed `closed` row (issue #51) — those transitions
    belong to the apply/dismiss/outcome routes, not the scorer."""
    with cursor() as conn:
        if status is not None:
            conn.execute(
                """
                UPDATE jobs SET rank_score = ?,
                    status = CASE WHEN status IN ('discovered', 'ranked')
                                  THEN ? ELSE status END
                WHERE id = ?
                """,
                (rank_score, status, job_id),
            )
        else:
            conn.execute(
                "UPDATE jobs SET rank_score = ? WHERE id = ?",
                (rank_score, job_id),
            )


def mark_applied(job_id, branch):
    with cursor() as conn:
        conn.execute(
            """
            UPDATE jobs SET status = 'applied',
                            applied_at = CURRENT_TIMESTAMP,
                            branch = ?
            WHERE id = ?
            """,
            (branch, job_id),
        )


def status_counts():
    with cursor() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"
        ).fetchall()
    counts = {r["status"]: r["n"] for r in rows}
    counts["open"] = counts.get("discovered", 0) + counts.get("ranked", 0)
    counts["total"] = sum(r["n"] for r in rows)
    return counts


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------

def upsert_outcome(job_id, *, referral=False, callback=False, callback_at=None,
                   ghosted=False, rejected=False, offer=False, notes=""):
    with cursor() as conn:
        conn.execute(
            """
            INSERT INTO outcomes (job_id, referral, callback, callback_at,
                                  ghosted, rejected, offer, notes, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(job_id) DO UPDATE SET
                referral = excluded.referral,
                callback = excluded.callback,
                callback_at = excluded.callback_at,
                ghosted = excluded.ghosted,
                rejected = excluded.rejected,
                offer = excluded.offer,
                notes = excluded.notes,
                updated_at = CURRENT_TIMESTAMP
            """,
            (job_id, int(referral), int(callback), callback_at,
             int(ghosted), int(rejected), int(offer), notes),
        )


# ---------------------------------------------------------------------------
# Stats / ranking inputs
# ---------------------------------------------------------------------------

def get_platform_stats(ats_platform):
    """
    Return (platform_callbacks, platform_applied, global_callback_rate,
    global_applied). Used by the ranker; global_applied gates whether
    platform_factor is trusted yet (see ranking.MIN_OUTCOMES_FOR_PLATFORM_FACTOR).
    """
    with cursor() as conn:
        global_row = conn.execute(
            """
            SELECT
                SUM(CASE WHEN o.callback = 1 THEN 1 ELSE 0 END) AS cb,
                COUNT(*) AS applied
            FROM jobs j JOIN outcomes o ON o.job_id = j.id
            WHERE j.status = 'applied' OR j.applied_at IS NOT NULL
            """
        ).fetchone()
        plat_row = conn.execute(
            """
            SELECT
                SUM(CASE WHEN o.callback = 1 THEN 1 ELSE 0 END) AS cb,
                COUNT(*) AS applied
            FROM jobs j JOIN outcomes o ON o.job_id = j.id
            WHERE (j.status = 'applied' OR j.applied_at IS NOT NULL)
              AND j.ats_platform = ?
            """,
            (ats_platform,),
        ).fetchone()

    g_applied = global_row["applied"] or 0
    g_callbacks = global_row["cb"] or 0
    p_applied = plat_row["applied"] or 0
    p_callbacks = plat_row["cb"] or 0
    global_rate = (g_callbacks / g_applied) if g_applied else 0.0
    return p_callbacks, p_applied, global_rate, g_applied


def platform_stats_summary():
    """Counts for the top-of-page banner."""
    with cursor() as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS total_applied,
                SUM(CASE WHEN o.callback = 1 THEN 1 ELSE 0 END) AS total_callbacks,
                SUM(CASE WHEN o.offer = 1 THEN 1 ELSE 0 END) AS total_offers
            FROM jobs j JOIN outcomes o ON o.job_id = j.id
            WHERE j.status = 'applied' OR j.applied_at IS NOT NULL
            """
        ).fetchone()

    total_applied = row["total_applied"] or 0
    total_callbacks = row["total_callbacks"] or 0
    total_offers = row["total_offers"] or 0
    rate_pct = round(100 * total_callbacks / total_applied, 1) if total_applied else 0.0
    return {
        "total_applied": total_applied,
        "total_callbacks": total_callbacks,
        "total_offers": total_offers,
        "callback_rate_pct": rate_pct,
    }


def all_jobs_for_rerank():
    with cursor() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT id, fit_score, desirability_score, posted_at, discovered_at, "
            "ats_platform FROM jobs WHERE fit_score IS NOT NULL"
        ).fetchall()]


def top_open_missing_desirability(limit):
    """Highest-fit open rows that have no desirability score yet — the ones most
    worth re-scoring first when backfilling the desirability axis (rescore.py)."""
    with cursor() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT id, url, description, ats_platform, posted_at, discovered_at "
            "FROM jobs WHERE status IN ('discovered', 'ranked') "
            "AND fit_score IS NOT NULL AND desirability_score IS NULL "
            "ORDER BY fit_score DESC LIMIT ?",
            (limit,),
        ).fetchall()]


def all_job_urls():
    """Every stored job URL. Backs GET /jobs/urls, which the poller uses for
    dedupe / stop-when-seen without direct DB access."""
    with cursor() as conn:
        return [r["url"] for r in conn.execute("SELECT url FROM jobs").fetchall()]


# ---------------------------------------------------------------------------
# Company targets (per-company polling config for the discovery bot)
# ---------------------------------------------------------------------------

def list_company_targets():
    with cursor() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM company_targets ORDER BY name COLLATE NOCASE ASC"
        ).fetchall()]


def get_company_target(target_id):
    with cursor() as conn:
        row = conn.execute(
            "SELECT * FROM company_targets WHERE id = ?", (target_id,)
        ).fetchone()
        return dict(row) if row else None


def create_company_target(*, name, careers_url, ats_platform, ats_identifier,
                          deny_list=None):
    ats_platform = normalize_platform(ats_platform)
    with cursor() as conn:
        cur = conn.execute(
            """
            INSERT INTO company_targets
                (name, careers_url, ats_platform, ats_identifier, deny_list)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                name,
                careers_url,
                ats_platform,
                json.dumps(ats_identifier) if ats_identifier else None,
                json.dumps(deny_list or []),
            ),
        )
        return cur.lastrowid


def update_company_target(target_id, *, name=None, deny_list=None,
                          ats_platform=None, ats_identifier=None,
                          last_polled_at=None, last_polled_count=None):
    fields = []
    values = []
    if name is not None:
        fields.append("name = ?")
        values.append(name)
    if deny_list is not None:
        fields.append("deny_list = ?")
        values.append(json.dumps(deny_list))
    if ats_platform is not None:
        fields.append("ats_platform = ?")
        values.append(ats_platform)
    if ats_identifier is not None:
        fields.append("ats_identifier = ?")
        values.append(json.dumps(ats_identifier))
    if last_polled_at is not None:
        fields.append("last_polled_at = ?")
        values.append(last_polled_at)
    if last_polled_count is not None:
        fields.append("last_polled_count = ?")
        values.append(last_polled_count)
    if not fields:
        return
    fields.append("updated_at = CURRENT_TIMESTAMP")
    values.append(target_id)
    with cursor() as conn:
        conn.execute(
            f"UPDATE company_targets SET {', '.join(fields)} WHERE id = ?",
            values,
        )


def delete_company_target(target_id):
    with cursor() as conn:
        conn.execute("DELETE FROM company_targets WHERE id = ?", (target_id,))


def parse_company_target(row):
    """Decode the JSON columns into Python values for templates / API responses."""
    row = dict(row)
    try:
        row["deny_list"] = json.loads(row.get("deny_list") or "[]")
    except (TypeError, ValueError):
        row["deny_list"] = []
    if row.get("ats_identifier"):
        try:
            row["ats_identifier_parsed"] = json.loads(row["ats_identifier"])
        except (TypeError, ValueError):
            row["ats_identifier_parsed"] = None
    else:
        row["ats_identifier_parsed"] = None
    return row


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------

def parse_analysis(analysis_json):
    if not analysis_json:
        return {}
    try:
        return json.loads(analysis_json)
    except (TypeError, ValueError):
        return {}


# ---------------------------------------------------------------------------
# Settings — key/value store for things like the stale-cleanup threshold.
# ---------------------------------------------------------------------------

def get_setting(key, default=None):
    with cursor() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    with cursor() as conn:
        conn.execute(
            """
            INSERT INTO settings (key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = CURRENT_TIMESTAMP
            """,
            (key, str(value)),
        )


# ---------------------------------------------------------------------------
# Cleanup helpers
# ---------------------------------------------------------------------------

def list_cleanup_candidates():
    """Rows eligible for stale-cleanup: not applied, not closed, no recorded
    outcome data. Returns id, url, discovered_at."""
    with cursor() as conn:
        return [dict(r) for r in conn.execute(
            """
            SELECT j.id, j.url, j.discovered_at
            FROM jobs j
            LEFT JOIN outcomes o ON o.job_id = j.id
            WHERE j.status IN ('discovered', 'ranked')
              AND (o.job_id IS NULL OR
                   (o.callback = 0 AND o.offer = 0 AND o.referral = 0
                    AND o.rejected = 0 AND o.ghosted = 0
                    AND COALESCE(o.notes, '') = ''))
            """
        ).fetchall()]


def delete_jobs(job_ids):
    """Bulk delete by id. Outcomes rows cascade via ON DELETE CASCADE."""
    if not job_ids:
        return 0
    placeholders = ",".join(["?"] * len(job_ids))
    with cursor() as conn:
        cur = conn.execute(f"DELETE FROM jobs WHERE id IN ({placeholders})", list(job_ids))
        return cur.rowcount
