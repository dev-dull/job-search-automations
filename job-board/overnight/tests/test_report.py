"""write_reports against fixtures — the renderer is where a drifted
/jobs/score contract goes unnoticed (PR #76 round-2 finding: rank=None in
every row of the scored table)."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import report  # noqa: E402
from prescreen import Decision  # noqa: E402


class WriteReportsTest(unittest.TestCase):
    def _decision(self, **kw):
        base = dict(url="https://example.com/j", title="DevOps Engineer",
                    company="Acme", keep=True, stage="kept",
                    reason="fit sketch 90", detail={"fit_sketch": 90})
        base.update(kw)
        return Decision(**base)

    def test_scored_table_renders_real_ranks(self):
        outdir = Path(tempfile.mkdtemp())
        submitted = [{"url": "https://example.com/j", "title": "DevOps Engineer",
                      "company": "Acme", "rank": 84.5, "score": 88.0}]
        report.write_reports(outdir, [self._decision()], [], [],
                             submitted=submitted, dry_run=False)
        md = next(outdir.glob("*.md")).read_text()
        self.assertIn("## Scored overnight", md)
        self.assertIn("| 84.5 | 88.0 | Acme | DevOps Engineer |", md)
        self.assertNotIn("| None |", md)     # the exact symptom of key drift

    def test_problems_section_and_rejection_log(self):
        outdir = Path(tempfile.mkdtemp())
        dropped = [self._decision(keep=False, stage="gates",
                                  reason="gate: location", detail={})]
        report.write_reports(outdir, [], dropped,
                             ["seen-set is EMPTY — dedupe may be broken"],
                             submitted=[], dry_run=True)
        md = next(outdir.glob("*.md")).read_text()
        self.assertIn("## Problems", md)
        self.assertIn("seen-set is EMPTY", md)
        csv = next(outdir.glob("*.csv")).read_text()
        self.assertIn("gate: location", csv)


if __name__ == "__main__":
    unittest.main()
