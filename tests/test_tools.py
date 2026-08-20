"""Guardrail Layer 5: the tool allowlist has no escape hatch."""
import pytest

from app.ai.tools import TOOLS, anthropic_tool_specs, execute_tool
from app.ai.validation import GuardrailError


@pytest.mark.parametrize("invented", ["execute_sql", "run_query", "delete_ticks", "drop_table", ""])
async def test_unknown_tools_are_rejected(invented):
    with pytest.raises(GuardrailError) as exc:
        await execute_tool(invented, {})
    assert exc.value.layer == "tool_allowlist"


def test_no_tool_can_write():
    """The strongest statement this codebase makes: the surface is read-only.

    If someone adds a mutating tool later, this test is what fails first.
    """
    write_words = ("insert", "update", "delete", "drop", "create", "alter", "grant", "truncate")
    for tool in TOOLS:
        assert not any(w in tool.name.lower() for w in write_words), tool.name


def test_every_tool_advertises_a_strict_closed_schema():
    for spec in anthropic_tool_specs():
        assert spec["strict"] is True
        assert spec["input_schema"]["additionalProperties"] is False
        assert spec["description"], f"{spec['name']} has no description"


def test_schema_and_validator_agree_on_required_args():
    """The JSON schema shown to the model and the Pydantic model that enforces
    it must not drift; a required field in one must exist in the other."""
    for tool in TOOLS:
        fields = set(tool.args_model.model_fields)
        for required in tool.schema.get("required", []):
            assert required in fields, f"{tool.name}: schema requires {required!r}, model lacks it"
