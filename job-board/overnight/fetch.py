"""Rate-limited wrapper around the existing ATS adapters.

The adapters in `job-store/adapters/` are pure fetch functions and are reused
verbatim — this module does not reimplement any scraping. What it adds is the
politeness the overnight brief requires and the poller does not currently
implement:

  - a minimum delay between requests to the same host
  - no parallelism per host (fetching is strictly serial)
  - a circuit breaker that stops hitting a host after repeated 403/429

That last one is not theoretical: the Workday scrapers tripped bot detection
once already. A nightly job that retries into a block turns a one-off into a
pattern, so the breaker is deliberately trigger-happy — it gives up on a host
for the rest of the run and reports it, rather than backing off and trying again.
"""

from __future__ import annotations

import random
import sys
import time
import urllib.error
import urllib.parse
from pathlib import Path

# Reuse the job-store adapters rather than writing new scrapers.
_JOB_STORE = Path(__file__).resolve().parent.parent / "job-store"
if str(_JOB_STORE) not in sys.path:
    sys.path.insert(0, str(_JOB_STORE))

from adapters import ADAPTERS  # noqa: E402
from urls import compute_dedupe_key  # noqa: E402

__all__ = ["ADAPTERS", "compute_dedupe_key", "PoliteFetcher", "HostBlocked"]


class HostBlocked(RuntimeError):
    """Raised when a host has refused us enough times to stop for the night."""


class PoliteFetcher:
    def __init__(self, *, min_delay: float = 3.0, jitter: float = 1.5, block_after: int = 3):
        self.min_delay = min_delay
        self.jitter = jitter
        self.block_after = block_after
        self._last_hit: dict[str, float] = {}
        self._refusals: dict[str, int] = {}
        self.blocked: set[str] = set()

    @staticmethod
    def _host(url: str) -> str:
        try:
            return urllib.parse.urlparse(url).netloc.lower()
        except Exception:
            return url

    def _wait(self, host: str) -> None:
        last = self._last_hit.get(host)
        if last is not None:
            elapsed = time.monotonic() - last
            delay = self.min_delay + random.uniform(0, self.jitter)
            if elapsed < delay:
                time.sleep(delay - elapsed)
        self._last_hit[host] = time.monotonic()

    def _note_refusal(self, host: str) -> None:
        self._refusals[host] = self._refusals.get(host, 0) + 1
        if self._refusals[host] >= self.block_after:
            self.blocked.add(host)

    def list_jobs(self, platform: str, identifier: dict, *, careers_url: str = "") -> list[dict]:
        """Fetch a board's postings, serially and politely.

        Returns the adapter's list of {url, title, description, ...}. Raises
        HostBlocked if this host has already refused us too often tonight.
        """
        adapter = ADAPTERS.get(platform)
        if adapter is None:
            raise ValueError(f"no adapter for ats_platform={platform!r}")

        host = self._host(careers_url) or platform
        if host in self.blocked:
            raise HostBlocked(f"{host}: skipped, refused {self._refusals.get(host, 0)}x tonight")

        self._wait(host)
        try:
            return adapter.list_jobs(identifier) or []
        except urllib.error.HTTPError as err:
            if err.code in (403, 429):
                self._note_refusal(host)
                if host in self.blocked:
                    raise HostBlocked(
                        f"{host}: HTTP {err.code} {self._refusals[host]}x — stopping for tonight"
                    ) from err
            raise
