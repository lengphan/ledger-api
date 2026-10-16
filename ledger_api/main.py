"""HTTP layer: validates input, calls the ledger, maps its refusals to status codes."""

import os
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException, Query, Response
from pydantic import BaseModel, Field

from .db import Database
from .ledger import Entry, Ledger, LedgerError

STATUS = {"not_found": 404, "exists": 409, "conflict": 409, "insufficient_funds": 422, "invalid": 422}


class AccountIn(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)


class EntryIn(BaseModel):
    type: Literal["topup", "charge", "refund"]
    ref: str = Field(min_length=1, max_length=128, description="Idempotency key, unique per account and type")
    amount_cents: int = Field(gt=0)
    refund_of: Optional[int] = None


def create_app(db_path: Optional[str] = None) -> FastAPI:
    ledger = Ledger(Database(db_path or os.environ.get("LEDGER_DB", "ledger.db")))
    app = FastAPI(title="Ledger API", version="1.0.0")

    def fail(e: LedgerError) -> HTTPException:
        return HTTPException(status_code=STATUS[e.code], detail={"code": e.code, "message": str(e)})

    @app.post("/accounts", status_code=201)
    def create_account(body: AccountIn) -> dict:
        try:
            ledger.create_account(body.id, body.name)
        except LedgerError as e:
            raise fail(e)
        return {"id": body.id, "name": body.name}

    @app.post("/accounts/{account_id}/entries", status_code=201)
    def post_entry(account_id: str, body: EntryIn, response: Response) -> dict:
        try:
            result = ledger.post(account_id, body.type, body.ref, body.amount_cents, body.refund_of)
        except LedgerError as e:
            raise fail(e)
        if result.replayed:
            # 200, not 201: nothing new was created. The body is the original entry.
            response.status_code = 200
        return {"entry": result.entry.__dict__, "replayed": result.replayed}

    @app.get("/accounts/{account_id}/balance")
    def balance(account_id: str) -> dict:
        try:
            return {"account_id": account_id, "balance_cents": ledger.balance(account_id)}
        except LedgerError as e:
            raise fail(e)

    @app.get("/accounts/{account_id}/entries")
    def list_entries(account_id: str, after: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)) -> dict:
        try:
            page: list[Entry] = ledger.entries(account_id, after, limit)
        except LedgerError as e:
            raise fail(e)
        return {"entries": [e.__dict__ for e in page], "next_after": page[-1].id if len(page) == limit else None}

    return app


app = create_app()
