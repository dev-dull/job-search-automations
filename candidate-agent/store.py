"""Phase-2 persistence: codes, sessions, transcripts, durable counters.

SQLite at AGENT_DB_PATH (default ./agent.db — the container should point this
at a mounted volume). WAL + generous busy timeout, single-process serving
(uvicorn --workers 1) — the same one-writer discipline as job-store, learned
the hard way there.

Code-table semantics (the phase-1 file stays first-class):
- Rows synced from ACCESS_CODES_PATH carry source='file' and are replaced to
  match the file on every reload — so the phase-1 revocation story (delete the
  line, let content sync deliver it) still works unchanged.
- Rows minted by mint_code.py carry source='cli' and are never touched by file
  sync. Revoke those with mint_code.py --revoke.

Sessions are honest about surface semantics (see PLAN.md): a `web` session is
a real conversation; an `mcp` session is a transport session (best-effort
grouping); a `fetch` "session" is one code's traffic for one UTC day.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from datetime import datetime, timezone

DB_PATH = os.environ.get("AGENT_DB_PATH", "agent.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS codes (
    code TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    expires TEXT,
    url_auth INTEGER NOT NULL DEFAULT 0,
    revoked INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'file',          -- 'file' | 'cli'
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    code TEXT NOT NULL,
    surface TEXT NOT NULL,                        -- 'web' | 'mcp' | 'fetch'
    client_name TEXT,
    client_version TEXT,
    ip TEXT,
    user_agent TEXT,
    started_at TEXT NOT NULL,
    last_active_at TEXT NOT NULL,
    ended_at TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL,                           -- 'user' | 'assistant'
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);

-- Durable fixed-window counters (rate limits, budgets, failed attempts).
-- key encodes the counter kind + subject; window is the fixed-window number.
CREATE TABLE IF NOT EXISTS counters (
    key TEXT PRIMARY KEY,
    window INTEGER NOT NULL,
    value REAL NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str | None = None):
        self.path = path or DB_PATH
        self._lock = threading.Lock()
        conn = self._conn()
        conn.executescript(_SCHEMA)
        conn.commit()
        conn.execute("PRAGMA journal_mode = WAL")
        conn.commit()
        # Sweep stale counters on startup: bounded table growth, and expired
        # fail:<ip> rows stop retaining IPs beyond their window (the README's
        # own personal-data guidance applies to us too). Window numbers are
        # only comparable within one window size, so prune per kind.
        import time as _time
        hour_w, day_w = int(_time.time() // 3600), int(_time.time() // 86400)
        conn.execute("DELETE FROM counters WHERE (key LIKE 'req:%' OR "
                     "key LIKE 'fail:%') AND window < ?", (hour_w,))
        conn.execute("DELETE FROM counters WHERE key LIKE 'spend:%' "
                     "AND window < ?", (day_w,))
        conn.commit()
        conn.close()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    # -- codes --------------------------------------------------------------

    def sync_codes_from_file(self, parsed_codes: list) -> None:
        """Sync source='file' rows to the file WITHOUT deleting history: rows
        absent from the file are tombstoned (revoked), not removed, so a code
        revoked by deleting its line keeps its sessions/cost in every rollup —
        revocation is every code's normal end state and attribution is the
        point of phase 2. Re-adding a line un-revokes (the file stays
        authoritative); created_at survives resyncs. CLI-minted rows are
        untouched, preserving both minting paths."""
        with self._lock, self._conn() as conn:
            conn.execute("UPDATE codes SET revoked = 1 WHERE source = 'file'")
            for c in parsed_codes:
                conn.execute(
                    "INSERT INTO codes "
                    "(code, label, expires, url_auth, revoked, note, source, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'file', ?) "
                    "ON CONFLICT(code) DO UPDATE SET label=excluded.label, "
                    "expires=excluded.expires, url_auth=excluded.url_auth, "
                    "revoked=excluded.revoked, note=excluded.note, source='file'",
                    (c.code, c.label, c.expires, int(c.url_auth),
                     int(c.revoked), c.note, _now()))

    def get_code(self, code: str) -> sqlite3.Row | None:
        with self._conn() as conn:
            return conn.execute("SELECT * FROM codes WHERE code = ?",
                                (code,)).fetchone()

    def upsert_cli_code(self, code: str, label: str, expires: str | None,
                        url_auth: bool, note: str = "") -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO codes "
                "(code, label, expires, url_auth, revoked, note, source, created_at) "
                "VALUES (?, ?, ?, ?, 0, ?, 'cli', ?)",
                (code, label, expires, int(url_auth), note, _now()))

    def revoke_cli_code(self, code: str) -> bool:
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                "UPDATE codes SET revoked = 1 WHERE code = ? AND source = 'cli'",
                (code,))
            return cur.rowcount > 0

    def all_codes(self) -> list[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute("SELECT * FROM codes ORDER BY created_at").fetchall()

    # -- sessions & messages ------------------------------------------------

    def touch_session(self, session_id: str, code: str, surface: str,
                      ip: str | None = None, user_agent: str | None = None,
                      client_name: str | None = None,
                      client_version: str | None = None) -> None:
        """Create the session row if new; refresh last_active either way.
        Client/UA fields only fill in when previously NULL (first writer wins)."""
        now = _now()
        with self._lock, self._conn() as conn:
            conn.execute(
                "INSERT INTO sessions (id, code, surface, client_name, "
                "client_version, ip, user_agent, started_at, last_active_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "last_active_at = excluded.last_active_at, "
                "client_name = COALESCE(sessions.client_name, excluded.client_name), "
                "client_version = COALESCE(sessions.client_version, excluded.client_version), "
                "ip = COALESCE(sessions.ip, excluded.ip), "
                "user_agent = COALESCE(sessions.user_agent, excluded.user_agent)",
                (session_id, code, surface, client_name, client_version,
                 ip, user_agent, now, now))

    def end_session(self, session_id: str) -> None:
        with self._lock, self._conn() as conn:
            conn.execute("UPDATE sessions SET ended_at = ? WHERE id = ?",
                         (_now(), session_id))

    def record_exchange(self, session_id: str, question: str, answer: str,
                        usage=None, cost_usd: float = 0.0) -> None:
        # Both branches must default missing keys to 0 — the token columns are
        # NOT NULL, and a None here would silently drop the whole exchange.
        get = ((lambda k, d=0: usage.get(k, d) or 0) if isinstance(usage, dict)
               else lambda k, d=0: getattr(usage, k, d) or 0) if usage else lambda k, d=0: 0
        now = _now()
        with self._lock, self._conn() as conn:
            conn.execute(
                "INSERT INTO messages (session_id, role, content, created_at) "
                "VALUES (?, 'user', ?, ?)", (session_id, question, now))
            conn.execute(
                "INSERT INTO messages (session_id, role, content, created_at, "
                "input_tokens, cache_read_tokens, cache_write_tokens, "
                "output_tokens, cost_usd) VALUES (?, 'assistant', ?, ?, ?, ?, ?, ?, ?)",
                (session_id, answer, now,
                 get("input_tokens"), get("cache_read_input_tokens"),
                 get("cache_creation_input_tokens"), get("output_tokens"),
                 cost_usd))
            conn.execute("UPDATE sessions SET last_active_at = ? WHERE id = ?",
                         (now, session_id))

    # -- durable fixed-window counters ---------------------------------------

    def bump(self, key: str, amount: float, window_s: int,
             now: float | None = None) -> float:
        """Add to a fixed-window counter, resetting when the window rolls.
        Returns the post-add value. amount=0 reads without counting."""
        now = now if now is not None else time.time()
        w = int(now // window_s)
        with self._lock, self._conn() as conn:
            row = conn.execute("SELECT window, value FROM counters WHERE key = ?",
                               (key,)).fetchone()
            value = (row["value"] if row and row["window"] == w else 0.0) + amount
            conn.execute(
                "INSERT OR REPLACE INTO counters (key, window, value) VALUES (?, ?, ?)",
                (key, w, value))
            return value

    # -- reporting -----------------------------------------------------------

    def summary(self) -> list[dict]:
        """Per-code rollup for report.py / the admin endpoint."""
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT c.code, c.label, c.source, c.revoked,
                       COUNT(DISTINCT s.id) AS sessions,
                       COUNT(m.id) AS messages,
                       COALESCE(SUM(m.cost_usd), 0) AS cost_usd,
                       MAX(s.last_active_at) AS last_seen
                FROM codes c
                LEFT JOIN sessions s ON s.code = c.code
                LEFT JOIN messages m ON m.session_id = s.id
                GROUP BY c.code ORDER BY last_seen DESC NULLS LAST
            """).fetchall()
            return [dict(r) for r in rows]

    def sessions_for_code(self, code: str) -> list[dict]:
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM sessions WHERE code = ? ORDER BY started_at",
                (code,)).fetchall()]

    def transcript(self, session_id: str) -> list[dict]:
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT role, content, created_at, cost_usd FROM messages "
                "WHERE session_id = ? ORDER BY id", (session_id,)).fetchall()]


class DurableLimiter:
    """Store-backed drop-in for codes.Limiter — same API, restart-proof.
    Closes the phase-1 'counters reset on restart' gap."""

    def __init__(self, store: Store):
        self.store = store

    def allow_request(self, code: str) -> bool:
        import codes as codes_mod
        return (self.store.bump(f"req:{code}", 1, 3600)
                <= codes_mod.RATE_LIMIT_PER_HOUR)

    def record_spend(self, code: str, usd: float) -> None:
        self.store.bump(f"spend:{code}", usd, 86400)
        self.store.bump("spend:GLOBAL", usd, 86400)

    def budget_ok(self, code: str) -> bool:
        import codes as codes_mod
        per = self.store.bump(f"spend:{code}", 0, 86400)
        glob = self.store.bump("spend:GLOBAL", 0, 86400)
        return (per < codes_mod.DAILY_BUDGET_USD_PER_CODE
                and glob < codes_mod.DAILY_BUDGET_USD_GLOBAL)

    def record_failed_attempt(self, ip: str) -> None:
        self.store.bump(f"fail:{ip}", 1, 3600)
        self.store.bump("fail:GLOBAL", 1, 3600)

    def attempts_ok(self, ip: str) -> bool:
        import codes as codes_mod
        per = self.store.bump(f"fail:{ip}", 0, 3600)
        glob = self.store.bump("fail:GLOBAL", 0, 3600)
        return (per < codes_mod.FAILED_ATTEMPTS_PER_IP_HOUR
                and glob < codes_mod.FAILED_ATTEMPTS_GLOBAL_HOUR)
