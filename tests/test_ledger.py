import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from ledger_api.db import Database
from ledger_api.main import create_app


@pytest.fixture
def client(tmp_path):
    # A real SQLite file per test, not a mock: the rules live partly in the schema.
    return TestClient(create_app(str(tmp_path / "test.db")))


@pytest.fixture
def acct(client):
    assert client.post("/accounts", json={"id": "a1", "name": "Shop"}).status_code == 201
    return "a1"


def post(client, acct, **body):
    return client.post(f"/accounts/{acct}/entries", json=body)


def balance(client, acct):
    return client.get(f"/accounts/{acct}/balance").json()["balance_cents"]


def test_balance_is_the_sum_of_entries(client, acct):
    post(client, acct, type="topup", ref="t1", amount_cents=10_000)
    post(client, acct, type="charge", ref="c1", amount_cents=2_500)
    assert balance(client, acct) == 7_500


def test_retry_returns_the_original_entry(client, acct):
    first = post(client, acct, type="topup", ref="t1", amount_cents=500)
    again = post(client, acct, type="topup", ref="t1", amount_cents=500)
    assert first.status_code == 201 and again.status_code == 200
    assert again.json()["replayed"] is True
    assert again.json()["entry"]["id"] == first.json()["entry"]["id"]
    assert balance(client, acct) == 500  # counted once


def test_same_key_different_amount_is_refused(client, acct):
    post(client, acct, type="topup", ref="t1", amount_cents=500)
    r = post(client, acct, type="topup", ref="t1", amount_cents=900)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "conflict"


def test_charge_cannot_overdraw(client, acct):
    post(client, acct, type="topup", ref="t1", amount_cents=100)
    r = post(client, acct, type="charge", ref="c1", amount_cents=101)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "insufficient_funds"
    assert balance(client, acct) == 100


def test_refund_is_capped_at_the_charge(client, acct):
    post(client, acct, type="topup", ref="t1", amount_cents=1_000)
    charge = post(client, acct, type="charge", ref="c1", amount_cents=600).json()["entry"]["id"]
    assert post(client, acct, type="refund", ref="r1", amount_cents=400, refund_of=charge).status_code == 201
    over = post(client, acct, type="refund", ref="r2", amount_cents=201, refund_of=charge)
    assert over.status_code == 422
    assert post(client, acct, type="refund", ref="r3", amount_cents=200, refund_of=charge).status_code == 201
    assert balance(client, acct) == 1_000


def test_refund_must_point_at_a_charge(client, acct):
    topup = post(client, acct, type="topup", ref="t1", amount_cents=1_000).json()["entry"]["id"]
    assert post(client, acct, type="refund", ref="r1", amount_cents=10, refund_of=topup).status_code == 422
    assert post(client, acct, type="refund", ref="r2", amount_cents=10).status_code == 422


def test_entries_are_append_only_in_the_database(tmp_path):
    db = Database(str(tmp_path / "raw.db"))
    with db.write() as conn:
        conn.execute("INSERT INTO accounts (id, name) VALUES ('a', 'A')")
        conn.execute("INSERT INTO entries (account_id, type, ref, amount_cents) VALUES ('a', 'topup', 'x', 5)")
    for sql in ("UPDATE entries SET amount_cents = 999", "DELETE FROM entries"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            with db.write() as conn:
                conn.execute(sql)


def test_concurrent_charges_never_double_spend(client, acct):
    """50 threads each try to spend $10 from a $100 balance at the same moment.

    Exactly 10 may succeed. Without BEGIN IMMEDIATE several would read the same balance,
    all pass the check, and the account would go negative.
    """
    post(client, acct, type="topup", ref="t1", amount_cents=10_000)
    with ThreadPoolExecutor(max_workers=50) as pool:
        codes = list(pool.map(lambda i: post(client, acct, type="charge", ref=f"c{i}", amount_cents=1_000).status_code, range(50)))
    assert codes.count(201) == 10
    assert codes.count(422) == 40
    assert balance(client, acct) == 0


def test_paging_walks_every_entry_once(client, acct):
    for i in range(7):
        post(client, acct, type="topup", ref=f"t{i}", amount_cents=1)
    seen, after = [], 0
    while True:
        page = client.get(f"/accounts/{acct}/entries", params={"after": after, "limit": 3}).json()
        seen += [e["ref"] for e in page["entries"]]
        if page["next_after"] is None:
            break
        after = page["next_after"]
    assert seen == [f"t{i}" for i in range(7)]


def test_unknown_account_is_404(client):
    assert client.get("/accounts/nope/balance").status_code == 404
