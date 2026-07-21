"""Status-transition guard tests (issue #51).

A re-score must never demote a job the user acted on: update_rank_score's
status write only applies to rows still in the discovery pipeline
(discovered/ranked); applied/closed keep their workflow state.

Uses the real db layer against a throwaway DB via JOBS_DB_PATH (set before
importing db, which reads it at import time).
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["JOBS_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test-jobs.db")

import db  # noqa: E402

db.init_db()


def _new_job(n):
    return db.upsert_job(
        url=f"https://boards.greenhouse.io/x/jobs/{n}", company="X", title=f"T{n}",
        description="d" * 200, ats_platform="greenhouse", posted_at=None,
        discovered_by="test", fit_score=80, analysis_json=None)


def _set_status(job_id, status):
    with db.cursor() as conn:
        conn.execute("UPDATE jobs SET status=? WHERE id=?", (status, job_id))


class UpdateRankScoreStatusGuardTest(unittest.TestCase):
    def test_rescore_does_not_demote_applied(self):
        jid = _new_job(1)
        _set_status(jid, "applied")
        db.update_rank_score(jid, 80.0, status="ranked")   # the POST-tail call
        self.assertEqual(db.get_job(jid)["status"], "applied")
        self.assertEqual(db.get_job(jid)["rank_score"], 80.0)  # rank still updates

    def test_rescore_does_not_resurrect_closed(self):
        jid = _new_job(2)
        _set_status(jid, "closed")
        db.update_rank_score(jid, 80.0, status="ranked")
        self.assertEqual(db.get_job(jid)["status"], "closed")

    def test_unscoreable_force_does_not_reset_applied_to_discovered(self):
        # the force + no-description sub-case: fit None -> status "discovered"
        jid = _new_job(3)
        _set_status(jid, "applied")
        db.update_rank_score(jid, None, status="discovered")
        self.assertEqual(db.get_job(jid)["status"], "applied")

    def test_pipeline_transitions_still_work(self):
        jid = _new_job(4)  # inserts as 'discovered'
        db.update_rank_score(jid, 80.0, status="ranked")
        self.assertEqual(db.get_job(jid)["status"], "ranked")

    def test_status_none_touches_only_rank(self):
        jid = _new_job(5)
        _set_status(jid, "applied")
        db.update_rank_score(jid, 42.0)
        row = db.get_job(jid)
        self.assertEqual((row["status"], row["rank_score"]), ("applied", 42.0))


class GatedPersistenceTest(unittest.TestCase):
    """#72: the gated flag persists via upsert and defaults to 0."""

    def test_upsert_stores_and_preserves_gated(self):
        jid = db.upsert_job(
            url="https://boards.greenhouse.io/x/jobs/909", company="X",
            title="T", description="d" * 200, ats_platform="greenhouse",
            posted_at=None, discovered_by="test", fit_score=80,
            analysis_json=None, gated=1)
        self.assertEqual(db.get_job(jid)["gated"], 1)
        # re-upsert with gated=None (legacy caller) must not clobber it
        db.upsert_job(url="https://boards.greenhouse.io/x/jobs/909",
                      company=None, title=None, description=None,
                      ats_platform=None, posted_at=None, discovered_by=None,
                      fit_score=None, analysis_json=None)
        self.assertEqual(db.get_job(jid)["gated"], 1)

    def test_default_ungated(self):
        jid = db.upsert_job(
            url="https://boards.greenhouse.io/x/jobs/910", company="X",
            title="T", description="d" * 200, ats_platform="greenhouse",
            posted_at=None, discovered_by="test", fit_score=80,
            analysis_json=None)
        self.assertEqual(db.get_job(jid)["gated"], 0)


if __name__ == "__main__":
    unittest.main()
