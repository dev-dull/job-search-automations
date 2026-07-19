"""Greenhouse custom-domain resolution tests (issue #43).

Boards on vanity hosts (jobs.elastic.co) expose no board token anywhere in the
page; the only signal is gh_jid. resolve_board_from_url guesses tokens from the
domain and verifies each against the board API — a wrong guess is discarded, so
resolution fails closed. No network: `verify` is injected.

Run with: python3 -m unittest discover -s tests
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adapters import greenhouse  # noqa: E402

ELASTIC_URL = ("https://jobs.elastic.co/jobs/engineering/united-states/"
               "senior-software-engineer-vector-search-elasticsearch/7573961"
               "?gh_jid=7573961")


def _verify_only(good_board):
    return lambda board, jid: board == good_board


class BoardCandidatesTest(unittest.TestCase):
    def test_vanity_subdomain(self):
        self.assertEqual(greenhouse.board_candidates("jobs.elastic.co"),
                         ["elastic", "elasticjobs"])

    def test_careers_prefix_and_com(self):
        self.assertEqual(greenhouse.board_candidates("careers.acme.com"),
                         ["acme", "acmejobs"])

    def test_two_part_public_suffix(self):
        self.assertEqual(greenhouse.board_candidates("careers.acme.co.uk"),
                         ["acme", "acmejobs"])

    def test_hyphenated_label_adds_dehyphenated_variant(self):
        self.assertEqual(greenhouse.board_candidates("jobs.big-corp.com"),
                         ["big-corp", "big-corpjobs", "bigcorp", "bigcorpjobs"])

    def test_generic_registrable_label_yields_nothing(self):
        # e.g. a host like www.jobs.com — "jobs" is furniture, not a company.
        self.assertEqual(greenhouse.board_candidates("www.jobs.com"), [])

    def test_bare_or_empty(self):
        self.assertEqual(greenhouse.board_candidates("localhost"), [])
        self.assertEqual(greenhouse.board_candidates(""), [])


class BoardFromEmbedUrlTest(unittest.TestCase):
    """Issue #52: embed URLs carry the board in `for`, not the path."""

    def test_job_board_embed(self):
        self.assertEqual(greenhouse.board_from_embed_url(
            "https://boards.greenhouse.io/embed/job_board?for=acme"), "acme")

    def test_js_embed_form(self):
        self.assertEqual(greenhouse.board_from_embed_url(
            "https://boards.greenhouse.io/embed/job_board/js?for=acme&b=x"), "acme")

    def test_job_app_token_has_no_board(self):
        # application iframe: token is a posting id, not a board — no token.
        self.assertIsNone(greenhouse.board_from_embed_url(
            "https://boards.greenhouse.io/embed/job_app?token=4689145005"))

    def test_no_query(self):
        self.assertIsNone(greenhouse.board_from_embed_url(
            "https://boards.greenhouse.io/embed/job_board"))
        self.assertIsNone(greenhouse.board_from_embed_url(""))


class PathJidResolveTest(unittest.TestCase):
    """HubSpot-style: no gh_jid param, posting id as a trailing path segment."""

    HUBSPOT = "https://www.hubspot.com/careers/jobs/7988809?hubs_signup-cta=careers-apply"

    def test_hubspot_url_resolves_via_jobs_suffix_candidate(self):
        got = greenhouse.resolve_board_from_url(
            self.HUBSPOT, verify=lambda b, j: b == "hubspotjobs")
        self.assertEqual(got, "hubspotjobs")

    def test_verify_receives_the_path_id(self):
        seen = []
        greenhouse.resolve_board_from_url(self.HUBSPOT,
                                          verify=lambda b, j: seen.append(j) or True)
        self.assertEqual(seen[0], "7988809")

    def test_short_trailing_number_does_not_probe(self):
        calls = []
        got = greenhouse.resolve_board_from_url(
            "https://www.acme.com/blog/page/2",
            verify=lambda b, j: calls.append(b) or True)
        self.assertIsNone(got)
        self.assertEqual(calls, [])


class ResolveBoardTest(unittest.TestCase):
    def test_elastic_url_resolves(self):
        got = greenhouse.resolve_board_from_url(ELASTIC_URL,
                                                verify=_verify_only("elastic"))
        self.assertEqual(got, "elastic")

    def test_no_gh_jid_returns_none_without_verifying(self):
        calls = []
        got = greenhouse.resolve_board_from_url(
            "https://jobs.elastic.co/jobs/engineering/",
            verify=lambda b, j: calls.append((b, j)) or True)
        self.assertIsNone(got)
        self.assertEqual(calls, [])   # fail-closed, zero API calls

    def test_wrong_guess_fails_closed(self):
        got = greenhouse.resolve_board_from_url(ELASTIC_URL, verify=lambda b, j: False)
        self.assertIsNone(got)

    def test_verify_receives_the_gh_jid(self):
        seen = {}
        greenhouse.resolve_board_from_url(
            ELASTIC_URL, verify=lambda b, j: seen.update(board=b, jid=j) or True)
        self.assertEqual(seen, {"board": "elastic", "jid": "7573961"})

    def test_known_greenhouse_host_not_needed_here(self):
        # boards.greenhouse.io URLs never reach this resolver (detect_ats wins),
        # but if one did, resolution is still harmless: no gh_jid -> None.
        self.assertIsNone(greenhouse.resolve_board_from_url(
            "https://boards.greenhouse.io/elastic/jobs/7573961",
            verify=lambda b, j: True))


if __name__ == "__main__":
    unittest.main()
