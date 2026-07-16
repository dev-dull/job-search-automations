"""SPA dead-link probe tests (issue #65).

Workday and Ashby serve HTTP 200 SPA shells for removed postings, so liveness
must be asked of their JSON APIs. posting_dead() returns True (dead), False
(alive), or None (undeterminable — callers keep the row).

Run with: python3 -m unittest discover -s tests
"""

import json
import os
import sys
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adapters import ashby, workday  # noqa: E402

WD_URL = ("https://redhat.wd5.myworkdayjobs.com/jobs/job/Remote-US-CO/"
          "Senior-Software-Engineer--GCP_R-053100")


def _http_error(code):
    return urllib.error.HTTPError(url="x", code=code, msg="", hdrs=None, fp=None)


class WorkdayPostingDeadTest(unittest.TestCase):
    def test_removed_posting_403_is_dead(self):
        # Workday answers 403 (errorCode S22) on the CXS detail endpoint for
        # removed reqs — the live evidence in #65.
        with mock.patch.object(workday, "_http_json", side_effect=_http_error(403)):
            self.assertIs(workday.posting_dead(WD_URL), True)

    def test_404_is_dead(self):
        with mock.patch.object(workday, "_http_json", side_effect=_http_error(404)):
            self.assertIs(workday.posting_dead(WD_URL), True)

    def test_live_posting_is_alive(self):
        with mock.patch.object(workday, "_http_json",
                               return_value={"jobPostingInfo": {"title": "T"}}):
            self.assertIs(workday.posting_dead(WD_URL), False)

    def test_noise_is_undeterminable(self):
        with mock.patch.object(workday, "_http_json", side_effect=_http_error(500)):
            self.assertIsNone(workday.posting_dead(WD_URL))
        with mock.patch.object(workday, "_http_json", side_effect=OSError("timeout")):
            self.assertIsNone(workday.posting_dead(WD_URL))

    def test_unparseable_url_is_undeterminable(self):
        self.assertIsNone(workday.posting_dead("https://redhat.wd5.myworkdayjobs.com/jobs"))


class AshbyPostingDeadTest(unittest.TestCase):
    URL = "https://jobs.ashbyhq.com/render/d52dc923-1641-4386-b8ba-5c15e1d7028d"

    def _board(self, ids):
        body = json.dumps({"jobs": [{"id": i, "jobUrl": f"https://jobs.ashbyhq.com/render/{i}"}
                                    for i in ids]}).encode()
        m = mock.MagicMock()
        m.__enter__ = lambda s: s
        m.__exit__ = mock.MagicMock(return_value=False)
        m.read.return_value = body
        return m

    def test_absent_uuid_is_dead(self):
        with mock.patch.object(ashby.urllib.request, "urlopen",
                               return_value=self._board(["aaaaaaaa-1111-2222-3333-444444444444"])):
            self.assertIs(ashby.posting_dead(self.URL), True)

    def test_listed_uuid_is_alive(self):
        with mock.patch.object(ashby.urllib.request, "urlopen",
                               return_value=self._board(["d52dc923-1641-4386-b8ba-5c15e1d7028d"])):
            self.assertIs(ashby.posting_dead(self.URL), False)

    def test_board_fetch_failure_is_undeterminable(self):
        with mock.patch.object(ashby.urllib.request, "urlopen",
                               side_effect=OSError("boom")):
            self.assertIsNone(ashby.posting_dead(self.URL))

    def test_non_posting_url_is_undeterminable(self):
        self.assertIsNone(ashby.posting_dead("https://jobs.ashbyhq.com/render"))


if __name__ == "__main__":
    unittest.main()
