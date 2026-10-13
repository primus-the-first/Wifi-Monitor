"""Tiny database layer: Postgres in production (DATABASE_URL), SQLite for local tests.

SQL is written once with `?` placeholders and plain syntax that both databases accept;
for Postgres the placeholders are rewritten to `%s`. Keep `?` and `%` out of SQL text.
"""
import os
import sqlite3
import threading
from contextlib import contextmanager

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS networks (gateway TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', "
    "enforce INTEGER NOT NULL DEFAULT 0, last_seen TEXT)",
    "CREATE TABLE IF NOT EXISTS devices (mac TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', "
    "last_ip TEXT, last_seen TEXT)",
    "CREATE TABLE IF NOT EXISTS usage (day TEXT NOT NULL, network TEXT NOT NULL, mac TEXT NOT NULL, "
    "bytes BIGINT NOT NULL DEFAULT 0, PRIMARY KEY (day, network, mac))",
    "CREATE TABLE IF NOT EXISTS batches (id TEXT PRIMARY KEY, received TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS agents (name TEXT PRIMARY KEY, last_seen TEXT NOT NULL, presence TEXT NOT NULL)",
)


class Tx:
    """One transaction: execute() returns rows as dicts; rowcount is that of the last statement."""

    def __init__(self, cursor, postgres):
        self._cur, self._postgres, self.rowcount = cursor, postgres, 0

    def execute(self, sql, params=()):
        self._cur.execute(sql.replace("?", "%s") if self._postgres else sql, params)
        self.rowcount = self._cur.rowcount
        return [dict(row) for row in self._cur.fetchall()] if self._cur.description else []


class DB:
    def __init__(self, url=None):
        url = url or os.environ.get("DATABASE_URL") or "sqlite:///server.db"
        self.postgres = url.startswith(("postgres://", "postgresql://"))
        if self.postgres:
            self._url = url
        else:
            path = url.removeprefix("sqlite:///")
            self._lock = threading.RLock()
            self._conn = sqlite3.connect(path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
        with self.tx() as tx:
            for statement in SCHEMA:
                tx.execute(statement)

    @property
    def kind(self):
        return "postgres" if self.postgres else "sqlite"

    @contextmanager
    def tx(self):
        if self.postgres:
            import psycopg
            from psycopg.rows import dict_row
            with psycopg.connect(self._url, row_factory=dict_row, connect_timeout=15) as conn:
                with conn.cursor() as cur:
                    yield Tx(cur, True)  # commits on success, rolls back on error
        else:
            with self._lock:
                cur = self._conn.cursor()
                try:
                    yield Tx(cur, False)
                    self._conn.commit()
                except Exception:
                    self._conn.rollback()
                    raise
