"""Core Table objects.

Only the tables the write path touches are modelled here. Reads go through
hand-written SQL in app/repository.py -- window functions, LATERAL joins and
partition-aware predicates are clearer as SQL than as ORM expressions, and this
codebase would rather have readable SQL than a leaky abstraction over it.
"""
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    func,
)

metadata = MetaData()

ticks_table = Table(
    "ticks",
    metadata,
    Column("symbol", String, primary_key=True),
    Column("trade_id", BigInteger, primary_key=True),
    Column("price", Numeric(20, 8), nullable=False),
    Column("qty", Numeric(20, 8), nullable=False),
    Column("quote_qty", Numeric(24, 8), nullable=False),
    Column("is_buyer_maker", Boolean, nullable=False),
    Column("ts", DateTime(timezone=True), primary_key=True),
    Column("ingested_at", DateTime(timezone=True), server_default=func.now()),
)

ohlcv_table = Table(
    "ohlcv_1m",
    metadata,
    Column("symbol", String, primary_key=True),
    Column("bucket", DateTime(timezone=True), primary_key=True),
    Column("open", Numeric(20, 8), nullable=False),
    Column("high", Numeric(20, 8), nullable=False),
    Column("low", Numeric(20, 8), nullable=False),
    Column("close", Numeric(20, 8), nullable=False),
    Column("volume", Numeric(28, 8), nullable=False),
    Column("quote_volume", Numeric(28, 8), nullable=False),
    Column("trade_count", Integer, nullable=False),
)
