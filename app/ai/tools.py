"""The AI's entire capability surface.

Six read-only tools. This list is the security boundary: whatever the model
decides to do, it can only ever result in one of these six functions running,
against a connection that holds SELECT on three tables. There is no `run_sql`
tool, no escape hatch, and no dynamic tool registration.

Each tool is defined once as a (schema, argument model, executor) triple so the
JSON schema advertised to the model and the Pydantic model that validates the
call cannot drift apart.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel

from app import repository as repo
from app.ai.validation import (
    CompareSymbolsArgs,
    GuardrailError,
    LargestMovesArgs,
    LatestPriceArgs,
    NoArgs,
    OHLCVSeriesArgs,
    OHLCVSummaryArgs,
    validate_args,
)
from app.core.config import settings
from app.core.db import session_ro


def _jsonable(value: Any) -> Any:
    """Decimals and datetimes are not JSON. Decimal -> str keeps full precision;
    float() here would reintroduce exactly the rounding error the NUMERIC columns
    were chosen to avoid."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: dict
    args_model: type[BaseModel]
    executor: Callable


# ---------------------------------------------------------------------------
# Executors. Every one uses session_ro -- the read-only role.
# ---------------------------------------------------------------------------
async def _exec_list_symbols(_args: BaseModel) -> dict:
    async with session_ro() as db:
        rows = await repo.list_symbols(db)
    return {"symbols": [r["symbol"] for r in rows], "count": len(rows)}


async def _exec_latest_price(args: LatestPriceArgs) -> dict:
    async with session_ro() as db:
        row = await repo.latest_price(db, args.symbol)
    if row is None:
        raise GuardrailError(f"no tick data recorded for {args.symbol}", layer="no_data")
    return _jsonable(row)


async def _exec_ohlcv_summary(args: OHLCVSummaryArgs) -> dict:
    async with session_ro() as db:
        row = await repo.ohlcv_summary(db, args.symbol, args.start, args.end)
    if row is None:
        raise GuardrailError(
            f"no candles for {args.symbol} between {args.start.isoformat()} "
            f"and {args.end.isoformat()}",
            layer="no_data",
        )
    out = _jsonable(row)
    out["symbol"] = args.symbol
    if row["open"] and row["open"] > 0:
        out["pct_change"] = round(float((row["close"] - row["open"]) / row["open"]) * 100, 4)
    return out


async def _exec_ohlcv_series(args: OHLCVSeriesArgs) -> dict:
    async with session_ro() as db:
        rows = await repo.get_ohlcv(db, args.symbol, args.start, args.end, settings.ai_max_rows)

    if not rows:
        raise GuardrailError(f"no candles for {args.symbol} in that window", layer="no_data")

    # Downsample by striding rather than truncating. Handing the model the first
    # 200 of 10,000 candles would silently answer a question about the whole
    # window using only its first 3%; an evenly spaced sample preserves shape.
    stride = max(1, len(rows) // args.max_points)
    sampled = rows[::stride][: args.max_points]

    return {
        "symbol": args.symbol,
        "candles": _jsonable(sampled),
        "returned": len(sampled),
        "total_in_window": len(rows),
        "downsampled": stride > 1,
        "stride_minutes": stride,
    }


async def _exec_largest_moves(args: LargestMovesArgs) -> dict:
    async with session_ro() as db:
        rows = await repo.largest_moves(db, args.symbol, args.start, args.end, args.top_n)
    if not rows:
        raise GuardrailError(f"no candles for {args.symbol} in that window", layer="no_data")

    if args.metric == "pct_range":
        rows = sorted(rows, key=lambda r: abs(float(r["pct_range"])), reverse=True)

    return {"symbol": args.symbol, "metric": args.metric, "moves": _jsonable(rows)}


async def _exec_compare_symbols(args: CompareSymbolsArgs) -> dict:
    async with session_ro() as db:
        rows = await repo.compare_symbols(db, args.symbols, args.start, args.end)
    if not rows:
        raise GuardrailError("no candles for any of those symbols in that window", layer="no_data")
    return {
        "window": {"start": args.start.isoformat(), "end": args.end.isoformat()},
        "results": _jsonable(rows),
    }


# ---------------------------------------------------------------------------
# Schemas advertised to the model.
#
# strict: True makes the API guarantee the arguments validate against the schema
# before they ever reach us -- it moves the first validation layer server-side.
# Our Pydantic layer still runs, because `strict` guarantees shape, not policy:
# it cannot know that a 5-year window is too expensive.
# ---------------------------------------------------------------------------
_TS = {
    "type": "string",
    "description": "ISO-8601 UTC timestamp, e.g. 2026-08-19T00:00:00Z",
}


def _window_props() -> dict:
    return {"start": dict(_TS), "end": dict(_TS)}


TOOLS: list[Tool] = [
    Tool(
        name="list_symbols",
        description=(
            "List every trading symbol the platform tracks. Call this first when "
            "the user names an asset informally (\"bitcoin\", \"eth\") so you can map "
            "it to the exact symbol before calling another tool."
        ),
        schema={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        args_model=NoArgs,
        executor=_exec_list_symbols,
    ),
    Tool(
        name="get_latest_price",
        description=(
            "Most recent traded price for one symbol, with the exchange timestamp. "
            "Use for \"what is X trading at right now\". Does not answer historical "
            "questions."
        ),
        schema={
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Exact symbol, e.g. BTCUSDT"}
            },
            "required": ["symbol"],
            "additionalProperties": False,
        },
        args_model=LatestPriceArgs,
        executor=_exec_latest_price,
    ),
    Tool(
        name="get_ohlcv_summary",
        description=(
            "Aggregate one symbol over a time window into a single row: open, high, "
            "low, close, volume, trade count and percentage change. This is the right "
            "tool for \"what was the high yesterday\", \"how much did X move today\", "
            "or \"total volume between 10:00 and 12:00\". Prefer it over "
            "get_ohlcv_series whenever you need a statistic rather than a shape -- it "
            "is one row instead of hundreds. Maximum window: "
            f"{settings.ai_max_range_days} days."
        ),
        schema={
            "type": "object",
            "properties": {"symbol": {"type": "string"}, **_window_props()},
            "required": ["symbol", "start", "end"],
            "additionalProperties": False,
        },
        args_model=OHLCVSummaryArgs,
        executor=_exec_ohlcv_summary,
    ),
    Tool(
        name="get_ohlcv_series",
        description=(
            "1-minute candles for one symbol across a window, evenly downsampled to "
            "at most max_points rows. Use only when the answer depends on the shape of "
            "the series over time (trend, when something happened). For a single "
            "statistic use get_ohlcv_summary instead."
        ),
        schema={
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                **_window_props(),
                "max_points": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 1000,
                    "description": "Maximum candles to return (default 200)",
                },
            },
            "required": ["symbol", "start", "end"],
            "additionalProperties": False,
        },
        args_model=OHLCVSeriesArgs,
        executor=_exec_ohlcv_series,
    ),
    Tool(
        name="get_largest_moves",
        description=(
            "The biggest single-minute moves for one symbol in a window, ranked. "
            "metric=pct_change ranks by close-vs-open (directional move); "
            "metric=pct_range ranks by high-vs-low (intra-minute swing). Use for "
            "\"largest 1-minute move\", \"biggest spike\", \"most volatile minute\"."
        ),
        schema={
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                **_window_props(),
                "top_n": {"type": "integer", "minimum": 1, "maximum": 25},
                "metric": {"type": "string", "enum": ["pct_change", "pct_range"]},
            },
            "required": ["symbol", "start", "end"],
            "additionalProperties": False,
        },
        args_model=LargestMovesArgs,
        executor=_exec_largest_moves,
    ),
    Tool(
        name="compare_symbols",
        description=(
            "Rank up to 10 symbols over one window by percentage return, realised "
            "daily volatility and traded volume. Use for any cross-symbol question: "
            "\"which moved most\", \"which was most volatile\", \"compare BTC and ETH\". "
            "One call handles all the symbols -- do not call it once per symbol."
        ),
        schema={
            "type": "object",
            "properties": {
                "symbols": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 10,
                },
                **_window_props(),
            },
            "required": ["symbols", "start", "end"],
            "additionalProperties": False,
        },
        args_model=CompareSymbolsArgs,
        executor=_exec_compare_symbols,
    ),
]

TOOLS_BY_NAME: dict[str, Tool] = {t.name: t for t in TOOLS}


def anthropic_tool_specs() -> list[dict]:
    return [
        {
            "name": t.name,
            "description": t.description,
            "input_schema": t.schema,
            "strict": True,
        }
        for t in TOOLS
    ]


async def execute_tool(name: str, raw_args: dict) -> dict:
    """Dispatch one tool call through the full guardrail stack.

    An unknown name is a hard stop, not a fallback. If the model invents a tool
    (`delete_ticks`, `run_query`), there is nothing to dispatch to -- the lookup
    fails and the model is told so.
    """
    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        raise GuardrailError(
            f"unknown tool {name!r}. Available tools: {', '.join(TOOLS_BY_NAME)}",
            layer="tool_allowlist",
        )
    args = validate_args(tool.args_model, raw_args or {})
    return await tool.executor(args)
