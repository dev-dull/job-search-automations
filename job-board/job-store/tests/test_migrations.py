"""init_db migration tests (issues #66 + #67).

Seeds a throwaway DB with the exact drift the live DB accumulated — old-format
dedupe keys (pre-e1e94a6 canonical URLs) and drifted ats_platform labels
('ashbyhq') — then runs init_db and asserts re-key, normalization, and the
resurrection-duplicate merge.

Sets db.DB_PATH directly (module attr is read at connect time), so this is
independent of import order and JOBS_DB_PATH.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402

RENDER_URL = "https://jobs.ashbyhq.com/render/d52dc923-1641-4386-b8ba-5c15e1d7028d"


def _seed(conn, url, dedupe_key, status, ats_platform, title="T"):
    cur = conn.execute(
        "INSERT INTO jobs (url, title, description, ats_platform, status, "
        "fit_score, dedupe_key) VALUES (?, ?, 'd', ?, ?, 80, ?)",
        (url, title, ats_platform, status, dedupe_key),
    )
    return cur.lastrowid


class MigrationTest(unittest.TestCase):
    def setUp(self):
        self._old_path = db.DB_PATH
        db.DB_PATH = Path(tempfile.mkdtemp()) / "mig.db"
        db.init_db()   # fresh schema

    def tearDown(self):
        db.DB_PATH = self._old_path

    def test_rekey_platform_normalize_and_merge(self):
        # The exact #66 pair: dismissed row under the OLD key format (its own
        # canonical URL, 'ashbyhq' label) + the resurrected duplicate under the
        # NEW key format.
        with db.cursor() as conn:
            closed_id = _seed(conn, f"{RENDER_URL}?source=LinkedIn",
                              RENDER_URL, "closed", "ashbyhq")
            _seed(conn, RENDER_URL, "ashby:d52dc923", "ranked", "ashby")

        db.init_db()   # the migration under test (idempotent re-run)

        with db.cursor() as conn:
            rows = conn.execute(
                "SELECT id, status, ats_platform, dedupe_key FROM jobs"
            ).fetchall()
        self.assertEqual(len(rows), 1, "duplicate should be merged away")
        survivor = rows[0]
        self.assertEqual(survivor["id"], closed_id, "workflow-state row wins")
        self.assertEqual(survivor["status"], "closed")
        self.assertEqual(survivor["ats_platform"], "ashby")       # #67
        self.assertEqual(survivor["dedupe_key"], "ashby:d52dc923")  # #66

    def test_rekeyed_row_now_blocks_resurrection(self):
        with db.cursor() as conn:
            _seed(conn, f"{RENDER_URL}?source=LinkedIn", RENDER_URL,
                  "closed", "ashbyhq")
        db.init_db()
        # Re-encounter via the poller/plugin: upsert must hit the survivor,
        # and the #51 guard keeps it closed.
        jid = db.upsert_job(url=RENDER_URL, company=None, title="T",
                            description="d" * 200, ats_platform="ashby",
                            posted_at=None, discovered_by="poller",
                            fit_score=82, analysis_json=None)
        db.update_rank_score(jid, 82.0, status="ranked")
        with db.cursor() as conn:
            rows = conn.execute("SELECT id, status FROM jobs").fetchall()
        self.assertEqual(len(rows), 1, "no new row on re-encounter")
        self.assertEqual(rows[0]["status"], "closed", "stays dismissed")

    def test_both_rows_with_workflow_state_are_left_alone(self):
        with db.cursor() as conn:
            _seed(conn, f"{RENDER_URL}?source=LinkedIn", RENDER_URL,
                  "applied", "ashby")
            _seed(conn, RENDER_URL, "ashby:d52dc923", "closed", "ashby")
        db.init_db()
        with db.cursor() as conn:
            n = conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
        self.assertEqual(n, 2, "never destroy rows that both carry user state")

    def test_no_state_keeps_oldest(self):
        with db.cursor() as conn:
            older = _seed(conn, f"{RENDER_URL}?source=LinkedIn", RENDER_URL,
                          "ranked", "ashby")
            _seed(conn, RENDER_URL, "ashby:d52dc923", "discovered", "ashby")
        db.init_db()
        with db.cursor() as conn:
            rows = conn.execute("SELECT id FROM jobs").fetchall()
        self.assertEqual([r["id"] for r in rows], [older])

    def test_upsert_normalizes_platform_on_write(self):
        jid = db.upsert_job(url="https://jobs.ashbyhq.com/zapier/aaaaaaaa-1111-2222-3333-444444444444",
                            company=None, title="T", description="d" * 200,
                            ats_platform="ashbyhq", posted_at=None,
                            discovered_by="plugin", fit_score=70,
                            analysis_json=None)
        self.assertEqual(db.get_job(jid)["ats_platform"], "ashby")


if __name__ == "__main__":
    unittest.main()
