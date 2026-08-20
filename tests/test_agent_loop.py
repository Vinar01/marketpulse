"""Agent loop behaviour, with a stubbed Anthropic client.

The loop's correctness has nothing to do with the model's quality, so these
tests replace the API with scripted responses. That makes them free, fast,
deterministic, and runnable in CI without a key -- and it lets us assert the
things that actually break in tool loops: message-shape invariants, error
propagation back to the model, and the iteration cap.
"""
from types import SimpleNamespace

import pytest

from app.ai.agent import MarketAnalyst, estimate_cost


def _usage(inp=100, out=50, cache_read=0, cache_write=0):
    return SimpleNamespace(
        input_tokens=inp,
        output_tokens=out,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_write,
    )


def _text_block(text):
    return SimpleNamespace(type="text", text=text)


def _tool_block(name, args, block_id="toolu_1"):
    return SimpleNamespace(type="tool_use", name=name, input=args, id=block_id)


class StubMessages:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.script.pop(0) if self.script else self.script_exhausted()

    @staticmethod
    def script_exhausted():
        return SimpleNamespace(
            stop_reason="end_turn", content=[_text_block("done")], usage=_usage()
        )


class StubClient:
    def __init__(self, script):
        self.messages = StubMessages(script)


@pytest.fixture
def analyst(monkeypatch):
    monkeypatch.setattr("app.ai.agent.settings.anthropic_api_key", "test-key")
    # Auditing writes to Postgres; these tests are about the loop, not the log.
    async def noop(*a, **kw):
        return None

    monkeypatch.setattr(MarketAnalyst, "_audit", staticmethod(noop))
    return MarketAnalyst(api_key="test-key")


async def test_plain_answer_returns_without_tools(analyst):
    analyst.client = StubClient(
        [SimpleNamespace(stop_reason="end_turn", content=[_text_block("BTC is $70,000.")],
                         usage=_usage())]
    )
    result = await analyst.ask("what is btc?")
    assert result["answer"] == "BTC is $70,000."
    assert result["tool_calls"] == []
    assert result["blocked_by"] is None


async def test_tool_result_is_fed_back_and_answered(analyst):
    analyst.client = StubClient(
        [
            SimpleNamespace(
                stop_reason="tool_use",
                content=[_tool_block("list_symbols", {})],
                usage=_usage(),
            ),
            SimpleNamespace(
                stop_reason="end_turn",
                content=[_text_block("We track 10 symbols.")],
                usage=_usage(),
            ),
        ]
    )
    result = await analyst.ask("what symbols do you track?")

    assert result["answer"] == "We track 10 symbols."
    assert [t.tool for t in result["tool_calls"]] == ["list_symbols"]
    assert result["tool_calls"][0].ok

    # The second request must carry: original user turn, the assistant turn
    # echoed verbatim, then one user turn holding the tool_result.
    second = analyst.client.messages.calls[1]["messages"]
    assert [m["role"] for m in second] == ["user", "assistant", "user"]
    assert second[2]["content"][0]["type"] == "tool_result"


async def test_all_tool_results_go_back_in_one_user_message(analyst):
    """Splitting parallel results across messages trains the model out of
    batching, so the loop must pack them into a single turn."""
    analyst.client = StubClient(
        [
            SimpleNamespace(
                stop_reason="tool_use",
                content=[
                    _tool_block("list_symbols", {}, "t1"),
                    _tool_block("get_latest_price", {"symbol": "BTCUSDT"}, "t2"),
                ],
                usage=_usage(),
            ),
            SimpleNamespace(stop_reason="end_turn", content=[_text_block("ok")], usage=_usage()),
        ]
    )
    await analyst.ask("prices and symbols")

    follow_up = analyst.client.messages.calls[1]["messages"]
    tool_turns = [m for m in follow_up if m["role"] == "user" and isinstance(m["content"], list)]
    assert len(tool_turns) == 1
    assert len(tool_turns[0]["content"]) == 2


async def test_guardrail_violation_is_returned_to_the_model_as_an_error(analyst):
    analyst.client = StubClient(
        [
            SimpleNamespace(
                stop_reason="tool_use",
                content=[_tool_block("get_latest_price", {"symbol": "'; DROP TABLE ticks; --"})],
                usage=_usage(),
            ),
            SimpleNamespace(
                stop_reason="end_turn",
                content=[_text_block("That symbol is not valid.")],
                usage=_usage(),
            ),
        ]
    )
    result = await analyst.ask("price of '; DROP TABLE ticks; --")

    trace = result["tool_calls"][0]
    assert trace.ok is False
    assert "typed_arguments" in trace.error

    result_block = analyst.client.messages.calls[1]["messages"][2]["content"][0]
    assert result_block["is_error"] is True
    assert "Error:" in result_block["content"]


async def test_invented_tool_is_rejected_by_the_allowlist(analyst):
    analyst.client = StubClient(
        [
            SimpleNamespace(
                stop_reason="tool_use",
                content=[_tool_block("execute_sql", {"query": "DELETE FROM ticks"})],
                usage=_usage(),
            ),
            SimpleNamespace(
                stop_reason="end_turn",
                content=[_text_block("I can only read market data.")],
                usage=_usage(),
            ),
        ]
    )
    result = await analyst.ask("delete everything")
    assert result["tool_calls"][0].ok is False
    assert "tool_allowlist" in result["tool_calls"][0].error


async def test_refusal_is_handled_before_reading_content(analyst):
    """A refusal can come back with an empty content list; indexing it blindly
    would raise instead of surfacing the refusal."""
    analyst.client = StubClient(
        [
            SimpleNamespace(
                stop_reason="refusal",
                content=[],
                usage=_usage(),
                stop_details=SimpleNamespace(category="cyber"),
            )
        ]
    )
    result = await analyst.ask("something disallowed")
    assert result["blocked_by"] == "model_refusal"
    assert "can't answer" in result["answer"]


async def test_iteration_cap_stops_a_tool_call_loop(analyst, monkeypatch):
    monkeypatch.setattr("app.ai.agent.settings.ai_max_tool_iterations", 3)
    analyst.client = StubClient(
        [
            SimpleNamespace(
                stop_reason="tool_use", content=[_tool_block("list_symbols", {}, f"t{i}")],
                usage=_usage(),
            )
            for i in range(10)
        ]
    )
    result = await analyst.ask("loop forever")
    assert result["blocked_by"] == "iteration_limit"
    assert len(analyst.client.messages.calls) == 3


async def test_system_prompt_carries_a_cache_breakpoint(analyst):
    analyst.client = StubClient(
        [SimpleNamespace(stop_reason="end_turn", content=[_text_block("hi")], usage=_usage())]
    )
    await analyst.ask("hello")
    system = analyst.client.messages.calls[0]["system"]
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    # Volatile content must stay OUT of the cached prefix or it invalidates on
    # every request; the timestamp belongs in the user turn.
    assert "Current UTC time" not in system[0]["text"]
    assert "Current UTC time" in analyst.client.messages.calls[0]["messages"][0]["content"]


def test_cost_accounts_for_cache_tiers():
    plain = estimate_cost(_usage(inp=1_000_000, out=0))
    cached = estimate_cost(_usage(inp=0, out=0, cache_read=1_000_000))
    assert plain == pytest.approx(5.00)
    assert cached == pytest.approx(0.50)
    assert cached < plain  # cache reads must be the cheap path
