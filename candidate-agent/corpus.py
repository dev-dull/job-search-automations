"""Corpus loading: walk CORPUS_PATH, parse front-matter, enforce redactions.

The corpus is the operator's curated, employer-facing content (see PLAN.md).
Every .md/.txt file under CORPUS_PATH becomes a document; YAML front-matter
(--- delimited) supplies title/type/date/tags/summary metadata.

Redaction-linter semantics (exactly as specified in the plan):
- a denylist hit at STARTUP fails startup loudly with file and line;
- a hit on a live RELOAD rejects the new corpus, keeps serving the last-good
  one, and logs loudly. A redaction miss is a blocked update, never a leak
  and never a crash-loop.

Reload checks are mtime-based and throttled (at most every RELOAD_CHECK_S),
so per-request calls are cheap. The content sync mechanism (git-sync sidecar
or otherwise) is the operator's choice; its interval bounds freshness.
"""

from __future__ import annotations

import logging
import os
import threading
import time

log = logging.getLogger("candidate-agent.corpus")

RELOAD_CHECK_S = 30
_EXTENSIONS = (".md", ".txt")


class RedactionError(Exception):
    def __init__(self, path: str, line_no: int, needle: str):
        self.path, self.line_no, self.needle = path, line_no, needle
        super().__init__(
            f"redaction denylist hit in {path}:{line_no} (matched {needle!r})")


def _parse_front_matter(text: str) -> tuple[dict, str]:
    """Minimal front-matter parser: leading '---' block of 'key: value' lines.
    Deliberately not full YAML — corpus metadata is flat by design."""
    meta: dict = {}
    if not text.startswith("---"):
        return meta, text
    lines = text.splitlines()
    try:
        end = next(i for i, l in enumerate(lines[1:], 1) if l.strip() == "---")
    except StopIteration:
        return meta, text
    for raw in lines[1:end]:
        if ":" in raw:
            k, _, v = raw.partition(":")
            meta[k.strip().lower()] = v.strip()
    return meta, "\n".join(lines[end + 1:]).strip()


def _load_denylist(path: str | None) -> list[str]:
    if not path or not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                out.append(line)
    return out


class Document:
    def __init__(self, rel_path: str, meta: dict, body: str):
        self.rel_path = rel_path
        self.meta = meta
        self.body = body

    @property
    def title(self) -> str:
        return self.meta.get("title") or self.rel_path

    @property
    def doc_type(self) -> str:
        return self.meta.get("type") or self.rel_path.split(os.sep)[0]

    def header(self) -> str:
        bits = [f"title: {self.title}", f"type: {self.doc_type}"]
        for k in ("date", "tags", "summary"):
            if self.meta.get(k):
                bits.append(f"{k}: {self.meta[k]}")
        return " | ".join(bits)


class Corpus:
    """Last-good in-memory corpus with throttled, reject-on-lint reload."""

    def __init__(self, root: str | None = None, denylist_path: str | None = None):
        self.root = root or os.environ.get("CORPUS_PATH")
        self.denylist_path = denylist_path or os.environ.get("REDACTION_DENYLIST_PATH")
        self.docs: list[Document] = []
        self._fingerprint: tuple = ()
        self._last_check = 0.0
        self._lock = threading.Lock()

    # -- loading ------------------------------------------------------------

    def _walk(self) -> list[str]:
        found = []
        for dirpath, _dirnames, filenames in os.walk(self.root):
            for name in sorted(filenames):
                if name.lower().endswith(_EXTENSIONS):
                    found.append(os.path.join(dirpath, name))
        return sorted(found)

    def _fingerprint_now(self, paths: list[str]) -> tuple:
        return tuple((p, os.path.getmtime(p)) for p in paths)

    def _lint(self, path: str, text: str, denylist: list[str]) -> None:
        lowered = [d.lower() for d in denylist]
        for i, line in enumerate(text.splitlines(), 1):
            low = line.lower()
            for needle, orig in zip(lowered, denylist):
                if needle in low:
                    raise RedactionError(path, i, orig)

    def _read_all(self) -> tuple[list[Document], tuple]:
        if not self.root:
            raise SystemExit(
                "CORPUS_PATH is not set. The agent refuses to run without an "
                "operator-supplied corpus — see candidate-agent/PLAN.md.")
        if not os.path.isdir(self.root):
            raise SystemExit(f"CORPUS_PATH does not exist: {self.root}")
        paths = self._walk()
        if not paths:
            raise SystemExit(f"CORPUS_PATH contains no .md/.txt documents: {self.root}")
        denylist = _load_denylist(self.denylist_path)
        docs = []
        for p in paths:
            with open(p, encoding="utf-8") as f:
                text = f.read()
            if denylist:
                self._lint(p, text, denylist)
            meta, body = _parse_front_matter(text)
            docs.append(Document(os.path.relpath(p, self.root), meta, body))
        return docs, self._fingerprint_now(paths)

    def load_or_die(self) -> None:
        """Startup path: any problem (missing corpus, lint hit) is fatal."""
        try:
            docs, fp = self._read_all()
        except RedactionError as e:
            raise SystemExit(f"REFUSING TO START: {e}") from e
        with self._lock:
            self.docs, self._fingerprint = docs, fp
        log.info("corpus loaded: %d documents, ~%d tokens (full-context ceiling ~150k)",
                 len(docs), self.token_estimate())

    def check_reload(self) -> None:
        """Request path: throttled; a bad new corpus is rejected loudly and
        the last-good one keeps serving."""
        now = time.time()
        if now - self._last_check < RELOAD_CHECK_S:
            return
        self._last_check = now
        try:
            paths = self._walk()
            if self._fingerprint_now(paths) == self._fingerprint:
                return
            docs, fp = self._read_all()
        except RedactionError as e:
            log.error("CORPUS RELOAD REJECTED (serving last-good corpus): %s", e)
            return
        except Exception as e:                          # noqa: BLE001
            log.error("corpus reload failed (serving last-good corpus): %s", e)
            return
        with self._lock:
            self.docs, self._fingerprint = docs, fp
        log.info("corpus reloaded: %d documents, ~%d tokens",
                 len(docs), self.token_estimate())

    # -- views --------------------------------------------------------------

    def assembled(self) -> str:
        """The rung-1 full-context block: every document with its header."""
        with self._lock:
            docs = list(self.docs)
        parts = []
        for d in docs:
            parts.append(f"<document {d.header()}>\n{d.body}\n</document>")
        return "\n\n".join(parts)

    def profile_summary(self) -> str:
        """The public 'card': the profile document's front-matter summary and
        first lines — used by get_profile_summary()."""
        with self._lock:
            docs = list(self.docs)
        for d in docs:
            if d.rel_path in ("profile.md", "profile.txt"):
                head = "\n".join(d.body.splitlines()[:12]).strip()
                return f"{d.header()}\n\n{head}"
        return "No profile document published."

    def token_estimate(self) -> int:
        return len(self.assembled()) // 4
