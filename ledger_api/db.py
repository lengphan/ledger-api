"""SQLite storage for the ledger.

The schema enforces the two rules the rest of the code relies on, so a bug in Python
cannot quietly break them:

* entries are append-only: triggers reject UPDATE and DELETE;
* one entry per (account, type, ref): a UNIQUE constraint, which is what makes a retried
  request safe.
"""

import sqlite3
from contextlib import contextmanager
from typing import Iterator

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS accounts (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS entries (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id   TEXT    NOT NULL REFERENCES accounts(id),
    type         TEXT    NOT NULL CHECK (type IN ('topup', 'charge', 'refund')),
    ref          TEXT    NOT NULL,
    amount_cents INTEGER NOT NULL,           -- signed: money in > 0, money out < 0
    refund_of    INTEGER REFERENCES entries(id),
    created_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (account_id, type, ref)
);

CREATE INDEX IF NOT EXISTS entries_by_account ON entries (account_id, id);

CREATE TRIGGER IF NOT EXISTS entries_no_update BEFORE UPDATE ON entries
BEGIN SELECT RAISE(ABORT, 'entries are append-only'); END;

CREATE TRIGGER IF NOT EXISTS entries_no_delete BEFORE DELETE ON entries
BEGIN SELECT RAISE(ABORT, 'entries are append-only'); END;
"""


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    def connect(self) -> sqlite3.Connection:
        # One connection per unit of work. isolation_level=None means Python does not open
        # transactions behind our back; `write()` opens them explicitly.
        conn = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """A write transaction that takes SQLite's write lock up front.

        BEGIN IMMEDIATE is the important part. With a plain BEGIN, two requests could both
        read the same balance, both decide a charge fits, and both write it: a double spend.
        IMMEDIATE makes the second request wait until the first commits, so the balance it
        reads already includes the first charge.
        """
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
