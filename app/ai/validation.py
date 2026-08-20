"""Guardrails for the AI query layer.

The threat model is simple and worth stating plainly: the model's output is
untrusted input. It does not matter whether a bad tool call came from a genuine
misunderstanding, a hallucinated symbol, or text inside a user's question that
said "ignore your instructions". All three arrive at this module as arguments,
and all three are handled the same way -- validated, bounded, or rejected.

Five layers, each of which holds on its own:

  1. Typed arguments      Pydantic models below. Wrong shape -> never executes.
  2. Query limits         Range and row caps. Expensive -> never executes.
  3. Statement timeout    3s, set on the connection AND on the role.
  4. Read-only role       marketpulse_ai_ro holds SELECT and nothing else.
  5. Table allowlist      Grants cover ticks/ohlcv_1m/symbols. Nothing else exists.

The design choice underneath all of it: the model never writes SQL. It selects a
tool and fills in typed parameters; this application composes the query. There is
no code path from model output to a SQL string, so there is nothing to inject
into. Text-to-SQL would collapse layers 1, 2 and 5 into "hope the model behaves".
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from app.core.config import settings
from app.schemas import validate_symbol_format


class GuardrailError(Exception):
    """A tool call was rejected. Carries the layer for metrics and the audit log."""

    def __init__(self, message: str, layer: str):
        super().__init__(message)
        self.layer = layer
        self.message = message


def _parse_ts(value) -> datetime:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        raw = value.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise ValueError(
                f"invalid timestamp {value!r}: expected ISO-8601, e.g. 2026-08-19T00:00:00Z"
            ) from exc
    else:
        raise ValueError(f"invalid timestamp type: {type(value).__name__}")

    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


class SymbolArg(BaseModel):
    symbol: str

    @field_validator("symbol")
    @classmethod
    def _symbol(cls, v: str) -> str:
        return validate_symbol_format(v)


class WindowArgs(BaseModel):
    """Any tool that reads a time range inherits these bounds."""

    start: datetime
    end: datetime

    @field_validator("start", "end", mode="before")
    @classmethod
    def _ts(cls, v):
        return _parse_ts(v)

    @model_validator(mode="after")
    def _bounds(self):
        if self.start >= self.end:
            raise ValueError("start must be strictly before end")

        span = self.end - self.start
        if span > timedelta(days=settings.ai_max_range_days):
            # Layer 2. Without this, "summarise the last five years" is a full
            # scan of every partition, a 3-second timeout, and a wasted API call.
            raise ValueError(
                f"requested window is {span.days} days; the maximum is "
                f"{settings.ai_max_range_days} days. Narrow the range and try again."
            )

        # Nothing exists in the future, and a far-future `end` is the usual shape
        # of a hallucinated date.
        horizon = datetime.now(UTC) + timedelta(days=1)
        if self.start > horizon:
            raise ValueError("start is in the future; no data exists for that window")
        return self


class LatestPriceArgs(SymbolArg):
    pass


class OHLCVSummaryArgs(SymbolArg, WindowArgs):
    pass


class OHLCVSeriesArgs(SymbolArg, WindowArgs):
    max_points: int = Field(default=200, ge=1, le=1000)


class LargestMovesArgs(SymbolArg, WindowArgs):
    top_n: int = Field(default=5, ge=1, le=25)
    metric: Literal["pct_change", "pct_range"] = "pct_change"


class CompareSymbolsArgs(WindowArgs):
    symbols: list[str] = Field(min_length=1, max_length=10)

    @field_validator("symbols")
    @classmethod
    def _symbols(cls, v: list[str]) -> list[str]:
        return [validate_symbol_format(s) for s in v]


class NoArgs(BaseModel):
    pass


def validate_args(model: type[BaseModel], raw: dict) -> BaseModel:
    """Layer 1 entry point. Turns a ValidationError into a GuardrailError whose
    message is written for the model to read and correct on its next turn."""
    try:
        return model(**raw)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'argument'}: {e['msg']}"
            for e in exc.errors()
        )
        raise GuardrailError(f"invalid arguments -- {problems}", layer="typed_arguments") from exc
    except ValueError as exc:
        raise GuardrailError(str(exc), layer="typed_arguments") from exc


def enforce_row_cap(rows: list, tool: str) -> list:
    """Layer 2, output side.

    Even a valid window can return more rows than are useful to put in a context
    window. Truncating here bounds token spend and latency; the tool result says
    explicitly that truncation happened so the model never reports a partial
    answer as complete.
    """
    cap = settings.ai_max_rows
    if len(rows) > cap:
        raise GuardrailError(
            f"{tool} matched {len(rows)} rows, above the {cap}-row cap. "
            "Use a narrower window or a summary tool instead.",
            layer="row_cap",
        )
    return rows
