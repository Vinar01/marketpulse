"""AI query endpoint."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from app.ai.agent import get_analyst
from app.ai.tools import TOOLS
from app.api.deps import Principal, require_api_key
from app.core.config import settings
from app.schemas import AskRequest, AskResponse

router = APIRouter(prefix="/api/v1", tags=["ai"])
Auth = Annotated[Principal, Depends(require_api_key)]


@router.post("/ask", response_model=AskResponse)
async def ask(body: AskRequest, principal: Auth):
    """Ask a question about the market data in plain English.

    Costs a real API call, so it sits behind the same API key as everything else
    and behind the shared rate limiter. The response includes the full tool trace
    -- which tools ran, with what arguments, and whether a guardrail rejected any
    of them -- so an answer can always be checked against the queries behind it.
    """
    if not settings.anthropic_api_key:
        raise HTTPException(
            status_code=503,
            detail="AI layer disabled: ANTHROPIC_API_KEY is not configured on the server",
        )

    result = await get_analyst().ask(body.question, role=principal.role)
    return AskResponse(**result)


@router.get("/ask/tools")
async def describe_tools(_: Auth):
    """The exact capability surface exposed to the model.

    Published deliberately: the security argument for this design is that the
    list is short, read-only and complete, and that argument only holds if the
    list is inspectable.
    """
    return {
        "tools": [
            {"name": t.name, "description": t.description, "input_schema": t.schema}
            for t in TOOLS
        ],
        "guardrails": {
            "layer_1_typed_arguments": "Pydantic models validate every argument before execution",
            "layer_2_query_limits": {
                "max_range_days": settings.ai_max_range_days,
                "max_rows": settings.ai_max_rows,
                "max_tool_iterations": settings.ai_max_tool_iterations,
            },
            "layer_3_statement_timeout_ms": settings.db_ro_statement_timeout_ms,
            "layer_4_database_role": "marketpulse_ai_ro (SELECT only, no DML, no DDL)",
            "layer_5_table_allowlist": ["ticks", "ohlcv_1m", "symbols"],
            "sql_generation": "disabled -- the model selects tools, it never writes SQL",
        },
    }
