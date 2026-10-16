# ledger-api

A small HTTP service that records money moving in and out of accounts — top-ups, charges and refunds — and can never double-count, overdraw or rewrite history.

It is a cut-down version of the wallet I run in production for a print-on-demand platform, where sellers top up a balance and each order is charged against it. Python 3.11, FastAPI and SQLite; no other services needed.

## Run it

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn ledger_api.main:app --reload     # http://127.0.0.1:8000/docs
.venv/bin/python -m pytest -q
```

```bash
curl -X POST localhost:8000/accounts -H 'content-type: application/json' -d '{"id":"a1","name":"Shop"}'
curl -X POST localhost:8000/accounts/a1/entries -H 'content-type: application/json' \
     -d '{"type":"topup","ref":"bank-7731","amount_cents":10000}'
curl localhost:8000/accounts/a1/balance
```

## API

| Method | Path | |
|---|---|---|
| `POST` | `/accounts` | create an account |
| `POST` | `/accounts/{id}/entries` | record a `topup`, `charge` or `refund` (`refund_of` = the charge's entry id) |
| `GET` | `/accounts/{id}/balance` | the sum of the account's entries |
| `GET` | `/accounts/{id}/entries?after=&limit=` | history, oldest first, a page at a time |

## Design decisions

**The balance is not stored.** It is `SUM(amount_cents)` over the account's entries. A stored balance is a second copy of the truth, and the two drift apart the first time an update and an insert are not in the same transaction.

**Entries are append-only, enforced by the database.** SQLite triggers reject `UPDATE` and `DELETE` on `entries`. A mistake is corrected by a new entry (a refund), so the history always explains the balance. Putting the rule in the schema means a bug in the Python can't break it.

**Every write is idempotent.** Each entry carries a `ref` chosen by the caller, and `(account, type, ref)` is unique. A client whose request timed out doesn't know whether it was recorded, so it sends it again: the same `ref` and amount return the original entry with `200` and `"replayed": true`. The same `ref` with a *different* amount is a client bug and gets `409`.

**No double spending under concurrency.** The overdraft check reads the balance and then writes the charge. If two requests do that at once, both can read the same balance and both pass. Writes therefore open with `BEGIN IMMEDIATE`, which takes SQLite's write lock before the read, so the second request waits and sees the first charge. `test_concurrent_charges_never_double_spend` fires 50 simultaneous $10 charges at a $100 balance and checks that exactly 10 succeed; with a plain `BEGIN` the same test fails.

**Refunds are tied to a charge.** A refund must name a charge on the same account, and the refunds against one charge can never add up to more than it took.

**Keyset paging.** History pages use `WHERE id > :after` rather than `OFFSET`, so a page stays correct while entries are being added and costs the same on page 1 as on page 10,000.

**Integer cents.** Amounts are integers in the smallest unit. Floating point can't represent 0.10 exactly, and those errors add up over a ledger.

## What it leaves out

Currencies (one per ledger), authentication, and a database that scales past one machine. Moving to Postgres would swap `BEGIN IMMEDIATE` for `SELECT … FOR UPDATE` on the account row; the rest of the design carries over.

## Tests

`pytest` runs against a real SQLite file per test rather than mocks, because half of the rules live in the schema. CI runs them on every push.

---

Built with the help of an AI coding assistant; the design follows the production wallet I maintain.
