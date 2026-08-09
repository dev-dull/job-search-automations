"""Corpus walker + redaction-linter tests. Synthetic fixtures only."""

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import corpus  # noqa: E402


def _mk_corpus(files):
    root = tempfile.mkdtemp()
    for rel, text in files.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
    return root


PROFILE = "---\ntitle: Profile\ntype: profile\nsummary: test candidate\n---\nJordan Sample, platform engineer.\n"
POST = "---\ntitle: A Post\ntype: post\ndate: 2026-01-01\n---\nWrote about testing.\n"


class FrontMatterTest(unittest.TestCase):
    def test_parse(self):
        meta, body = corpus._parse_front_matter(PROFILE)
        self.assertEqual(meta["title"], "Profile")
        self.assertEqual(body, "Jordan Sample, platform engineer.")

    def test_no_front_matter(self):
        meta, body = corpus._parse_front_matter("plain text")
        self.assertEqual((meta, body), ({}, "plain text"))


class LoadTest(unittest.TestCase):
    def test_load_and_assemble(self):
        root = _mk_corpus({"profile.md": PROFILE, "posts/a.md": POST})
        c = corpus.Corpus(root=root, denylist_path=None)
        c.load_or_die()
        text = c.assembled()
        self.assertIn("title: Profile", text)
        self.assertIn("Wrote about testing.", text)
        self.assertIn("Jordan Sample", c.profile_summary())

    def test_missing_corpus_dies(self):
        c = corpus.Corpus(root="/nonexistent/corpus", denylist_path=None)
        with self.assertRaises(SystemExit):
            c.load_or_die()

    def test_empty_corpus_dies(self):
        c = corpus.Corpus(root=tempfile.mkdtemp(), denylist_path=None)
        with self.assertRaises(SystemExit):
            c.load_or_die()


class LinterTest(unittest.TestCase):
    def _denylist(self, *needles):
        f = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
        f.write("# denylist\n" + "\n".join(needles) + "\n")
        f.close()
        self.addCleanup(os.remove, f.name)
        return f.name

    def test_startup_hit_dies(self):
        root = _mk_corpus({"profile.md": PROFILE,
                           "posts/leak.md": "I applied at MegaCorp today"})
        c = corpus.Corpus(root=root, denylist_path=self._denylist("megacorp"))
        with self.assertRaises(SystemExit) as ctx:
            c.load_or_die()
        self.assertIn("leak.md", str(ctx.exception))

    def test_reload_hit_keeps_last_good(self):
        root = _mk_corpus({"profile.md": PROFILE})
        c = corpus.Corpus(root=root, denylist_path=self._denylist("megacorp"))
        c.load_or_die()
        good = c.assembled()
        # A bad document arrives via content sync.
        with open(os.path.join(root, "bad.md"), "w") as f:
            f.write("Interviewing at MegaCorp\n")
        c._last_check = 0                       # bypass the throttle
        c.check_reload()
        self.assertEqual(c.assembled(), good)   # last-good corpus still serves
        self.assertNotIn("MegaCorp", c.assembled())

    def test_reload_clean_update_applies(self):
        root = _mk_corpus({"profile.md": PROFILE})
        c = corpus.Corpus(root=root, denylist_path=self._denylist("megacorp"))
        c.load_or_die()
        time.sleep(0.01)
        with open(os.path.join(root, "new.md"), "w") as f:
            f.write("---\ntitle: New\n---\nFresh clean doc\n")
        c._last_check = 0
        c.check_reload()
        self.assertIn("Fresh clean doc", c.assembled())


if __name__ == "__main__":
    unittest.main()
