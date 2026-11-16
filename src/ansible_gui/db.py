"""One SQLite connection shared by a store's threads (web requests, the executor, the scheduler).

sqlite3 connections aren't safe to use from two threads at once, even with check_same_thread=False:
a read racing a write can return a half-built row. Every statement here runs under one re-entrant lock
and its rows are read before the lock is released. A store takes the same lock around a transaction
of several statements, so another thread can't commit or read halfway through it.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path


class Result:
    def __init__(self, cur: sqlite3.Cursor):
        self.rows = cur.fetchall()
        self.lastrowid = cur.lastrowid
        self.rowcount = cur.rowcount

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows

    def __iter__(self):
        return iter(self.rows)


class Database:
    def __init__(self, path: Path):
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()

    def execute(self, sql: str, params=()) -> Result:
        with self.lock:
            return Result(self._conn.execute(sql, params))

    def executescript(self, sql: str) -> None:
        with self.lock:
            self._conn.executescript(sql)

    def commit(self) -> None:
        with self.lock:
            self._conn.commit()
