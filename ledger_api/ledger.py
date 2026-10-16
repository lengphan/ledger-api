"""The ledger's rules, with no HTTP in sight.

A balance is never stored. It is the sum of the account's entries, so there is no second
number that can drift away from the history.
"""

import sqlite3
from dataclasses import dataclass
from typing import Optional

from .db import Database

# What each entry type does to the balance. Callers always send a positive amount.
SIGN = {"topup": 1, "charge": -1, "refund": 1}


class LedgerError(Exception):
    """A request the ledger refuses. `code` says why, for the HTTP layer to map."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Entry:
    id: int
    account_id: str
    type: str
    ref: str
    amount_cents: int
    refund_of: Optional[int]
    created_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Entry":
        return cls(**{k: row[k] for k in row.keys()})


@dataclass(frozen=True)
class PostResult:
    entry: Entry
    replayed: bool  # True when this exact request had already been recorded


class Ledger:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create_account(self, account_id: str, name: str) -> None:
        with self.db.write() as conn:
            try:
                conn.execute("INSERT INTO accounts (id, name) VALUES (?, ?)", (account_id, name))
            except sqlite3.IntegrityError:
                raise LedgerError("exists", f"account {account_id} already exists") from None

    def balance(self, account_id: str) -> int:
        with self.db.read() as conn:
            self._require_account(conn, account_id)
            return self._balance(conn, account_id)

    def entries(self, account_id: str, after: int = 0, limit: int = 50) -> list[Entry]:
        """Oldest first, a page at a time. `after` is the last id the caller has seen.

        Keyset paging (WHERE id > ?) rather than OFFSET: a page stays correct while new
        entries are being added, and it costs the same on page 1 and page 10,000.
        """
        limit = max(1, min(limit, 100))
        with self.db.read() as conn:
            self._require_account(conn, account_id)
            rows = conn.execute(
                "SELECT * FROM entries WHERE account_id = ? AND id > ? ORDER BY id LIMIT ?",
                (account_id, after, limit),
            ).fetchall()
        return [Entry.from_row(r) for r in rows]

    def post(self, account_id: str, type_: str, ref: str, amount_cents: int,
             refund_of: Optional[int] = None) -> PostResult:
        """Record money moving. Safe to retry with the same (account, type, ref)."""
        if type_ not in SIGN:
            raise LedgerError("invalid", f"unknown entry type {type_!r}")
        if amount_cents <= 0:
            raise LedgerError("invalid", "amount_cents must be positive")
        if (type_ == "refund") != (refund_of is not None):
            raise LedgerError("invalid", "refund_of is required for a refund, and only for a refund")

        signed = SIGN[type_] * amount_cents
        with self.db.write() as conn:
            self._require_account(conn, account_id)

            # IDEMPOTENCY. A client that timed out does not know whether we recorded it, so it
            # sends the same request again. The same key with the same amount gets the original
            # entry back; the same key with a different amount is a client bug and is refused.
            existing = conn.execute(
                "SELECT * FROM entries WHERE account_id = ? AND type = ? AND ref = ?",
                (account_id, type_, ref),
            ).fetchone()
            if existing is not None:
                if existing["amount_cents"] != signed or existing["refund_of"] != refund_of:
                    raise LedgerError("conflict", f"{type_} {ref!r} was already recorded with different details")
                return PostResult(Entry.from_row(existing), replayed=True)

            if type_ == "charge" and self._balance(conn, account_id) < amount_cents:
                raise LedgerError("insufficient_funds", "balance is lower than the charge")
            if type_ == "refund":
                self._check_refund(conn, account_id, refund_of, amount_cents)

            cur = conn.execute(
                "INSERT INTO entries (account_id, type, ref, amount_cents, refund_of) VALUES (?, ?, ?, ?, ?)",
                (account_id, type_, ref, signed, refund_of),
            )
            row = conn.execute("SELECT * FROM entries WHERE id = ?", (cur.lastrowid,)).fetchone()
        return PostResult(Entry.from_row(row), replayed=False)

    # ── helpers; each runs inside the caller's connection ──────────────────────────────

    @staticmethod
    def _require_account(conn: sqlite3.Connection, account_id: str) -> None:
        if conn.execute("SELECT 1 FROM accounts WHERE id = ?", (account_id,)).fetchone() is None:
            raise LedgerError("not_found", f"no account {account_id}")

    @staticmethod
    def _balance(conn: sqlite3.Connection, account_id: str) -> int:
        return conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) FROM entries WHERE account_id = ?", (account_id,)
        ).fetchone()[0]

    @staticmethod
    def _check_refund(conn: sqlite3.Connection, account_id: str, charge_id: int, amount_cents: int) -> None:
        """A refund must point at a charge on the same account and never return more than it took."""
        charge = conn.execute(
            "SELECT amount_cents FROM entries WHERE id = ? AND account_id = ? AND type = 'charge'",
            (charge_id, account_id),
        ).fetchone()
        if charge is None:
            raise LedgerError("invalid", f"entry {charge_id} is not a charge on this account")
        refunded = conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) FROM entries WHERE refund_of = ?", (charge_id,)
        ).fetchone()[0]
        if refunded + amount_cents > -charge["amount_cents"]:
            raise LedgerError("invalid", "refunds would exceed the original charge")
