"""Access-code table, rate/budget counters, and the failed-attempt limiter.

Phase 1 storage is a flat file at ACCESS_CODES_PATH, operator-owned and
delivered by whatever content sync the deployment uses (its interval bounds
revocation latency). One code per line, pipe-separated:

    # code            | label       | key=value ...
    maple-K7RT-hazel  | Acme Corp   | expires=2026-08-30 | url_auth
    birch-Q2ZX-otter  | Beta Search | note=met at conf

Recognized flags/keys: expires=YYYY-MM-DD (default +30 days from first-seen is
NOT applied — absent means no expiry), url_auth (code may travel in URLs:
/mcp/<code> and the /c/<code> fetch surface), revoked (tombstone that keeps
the line for the phase-2 import), note=... (free text).

All counters are in-memory and reset on restart — a known, accepted phase-1
gap at single-replica scale; thresholds default conservatively because of it.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from datetime import date


RATE_LIMIT_PER_HOUR = int(os.environ.get("RATE_LIMIT_PER_HOUR", "60"))
DAILY_BUDGET_USD_PER_CODE = float(os.environ.get("DAILY_BUDGET_USD_PER_CODE", "5"))
DAILY_BUDGET_USD_GLOBAL = float(os.environ.get("DAILY_BUDGET_USD_GLOBAL", "25"))
# Invalid-code guessing: per-IP hourly cap and a global circuit breaker.
FAILED_ATTEMPTS_PER_IP_HOUR = int(os.environ.get("FAILED_ATTEMPTS_PER_IP_HOUR", "10"))
FAILED_ATTEMPTS_GLOBAL_HOUR = int(os.environ.get("FAILED_ATTEMPTS_GLOBAL_HOUR", "100"))


@dataclass
class Code:
    code: str
    label: str
    expires: str | None = None
    url_auth: bool = False
    revoked: bool = False
    note: str = ""

    def usable(self, today: date | None = None) -> bool:
        if self.revoked:
            return False
        if self.expires:
            try:
                if (today or date.today()) > date.fromisoformat(self.expires):
                    return False
            except ValueError:
                return False   # unparseable expiry -> treat as expired, not open
        return True


def _parse_line(line: str) -> Code | None:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    parts = [p.strip() for p in line.split("|")]
    if len(parts) < 2 or not parts[0]:
        return None
    c = Code(code=parts[0], label=parts[1])
    for extra in parts[2:]:
        if extra == "url_auth":
            c.url_auth = True
        elif extra == "revoked":
            c.revoked = True
        elif extra.startswith("expires="):
            c.expires = extra.split("=", 1)[1]
        elif extra.startswith("note="):
            c.note = extra.split("=", 1)[1]
    return c


class CodeTable:
    """Loads the code file, reloading when its mtime changes."""

    def __init__(self, path: str | None = None):
        self.path = path or os.environ.get("ACCESS_CODES_PATH")
        self._mtime: float | None = None
        self._codes: dict[str, Code] = {}
        self._lock = threading.Lock()

    def _refresh(self) -> None:
        if not self.path or not os.path.exists(self.path):
            return
        mtime = os.path.getmtime(self.path)
        if mtime == self._mtime:
            return
        table: dict[str, Code] = {}
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                c = _parse_line(line)
                if c:
                    table[c.code] = c
        with self._lock:
            self._codes = table
            self._mtime = mtime

    def lookup(self, code: str | None) -> Code | None:
        """The usable Code for this string, else None. Revoked/expired/absent
        are indistinguishable to callers on purpose."""
        if not code:
            return None
        self._refresh()
        with self._lock:
            c = self._codes.get(code.strip())
        return c if c and c.usable() else None

    def status(self, code: str | None) -> str:
        """For friendlier browser-surface messaging: 'ok' | 'expired' | 'unknown'."""
        if not code:
            return "unknown"
        self._refresh()
        with self._lock:
            c = self._codes.get(code.strip())
        if c is None:
            return "unknown"
        return "ok" if c.usable() else "expired"


@dataclass
class _Window:
    """Fixed-window counter (hour or day granularity)."""
    window: int = 0
    count: float = 0.0

    def add(self, amount: float, size_s: int, now: float | None = None) -> float:
        now = now if now is not None else time.time()
        w = int(now // size_s)
        if w != self.window:
            self.window, self.count = w, 0.0
        self.count += amount
        return self.count


class Limiter:
    """All in-memory abuse guards in one place. Thread-safe."""

    def __init__(self):
        self._lock = threading.Lock()
        self._req: dict[str, _Window] = {}
        self._spend: dict[str, _Window] = {}
        self._global_spend = _Window()
        self._failed_ip: dict[str, _Window] = {}
        self._failed_global = _Window()

    def allow_request(self, code: str) -> bool:
        with self._lock:
            w = self._req.setdefault(code, _Window())
            return w.add(1, 3600) <= RATE_LIMIT_PER_HOUR

    def record_spend(self, code: str, usd: float) -> None:
        with self._lock:
            self._spend.setdefault(code, _Window()).add(usd, 86400)
            self._global_spend.add(usd, 86400)

    def budget_ok(self, code: str) -> bool:
        with self._lock:
            per = self._spend.setdefault(code, _Window()).add(0, 86400)
            glob = self._global_spend.add(0, 86400)
        return per < DAILY_BUDGET_USD_PER_CODE and glob < DAILY_BUDGET_USD_GLOBAL

    def record_failed_attempt(self, ip: str) -> None:
        with self._lock:
            self._failed_ip.setdefault(ip, _Window()).add(1, 3600)
            self._failed_global.add(1, 3600)

    def attempts_ok(self, ip: str) -> bool:
        with self._lock:
            per = self._failed_ip.setdefault(ip, _Window()).add(0, 3600)
            glob = self._failed_global.add(0, 3600)
        return (per < FAILED_ATTEMPTS_PER_IP_HOUR
                and glob < FAILED_ATTEMPTS_GLOBAL_HOUR)
