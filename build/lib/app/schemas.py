"""Wire contracts. These are the only shapes that cross the HTTP boundary."""
from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator

SYMBOL_RE = re.compile(r"^[A-Z0-9]{4,20}$")


def validate_symbol_format(value: str) -> str:
    """Guardrail Layer 1. A symbol is uppercase alphanumeric, 4-20 chars.

    Anything containing a quote, semicolon, comment marker or whitespace is
    rejected here, before it reaches any query builder. Note that the queries
    themselves are parameterised regardless -- this is defence in depth, not the
    primary injection defence.
    """
    v = value.strip().upper()
    if not SYMBOL_RE.match(v):
        raise ValueError(
            f"invalid symbol {value!r}: expected 4-20 uppercase alphanumeric characters"
        )
    return v


class SymbolOut(BaseModel):
    symbol: str
    base_asset: str
    quote_asset: str
    is_active: bool


class TickOut(BaseModel):
    symbol: str
    trade_id: int
    price: Decimal
    qty: Decimal
    quote_qty: Decimal
    is_buyer_maker: bool
    ts: datetime


class LatestPriceOut(BaseModel):
    symbol: str
    price: Decimal
    qty: Decimal
    ts: datetime
    source: str = Field(description="cache | database")


class OHLCVOut(BaseModel):
    symbol: str
    bucket: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    quote_volume: Decimal
    trade_count: int


class CursorPage(BaseModel):
    """Keyset pagination.

    OFFSET pagination makes the database walk and discard every skipped row, so
    page 5000 costs 5000 pages of work and the cost grows with depth. Worse, rows
    inserted between requests shift the window and the client silently sees
    duplicates or gaps. A cursor encodes "resume after this (ts, trade_id)",
    which is an index seek at constant cost and is stable under concurrent writes.
    """

    items: list
    next_cursor: str | None = None
    has_more: bool = False


class HealthOut(BaseModel):
    status: str
    version: str
    checks: dict[str, str]
    ingest: dict


class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=1000)

    @field_validator("question")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


class ToolCallTrace(BaseModel):
    tool: str
    arguments: dict
    ok: bool
    error: str | None = None
    row_count: int | None = None
    duration_ms: int


class AskResponse(BaseModel):
    answer: str
    tool_calls: list[ToolCallTrace]
    blocked_by: str | None = None
    usage: dict
    latency_ms: int
