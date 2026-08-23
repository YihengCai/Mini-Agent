"""Offline regression tests for the agent loop and its LLM test double."""

import pytest

from mini_agent.schema import LLMResponse, Message
from tests.llm_test_double import ScriptedLLM


def response(content: str) -> LLMResponse:
    return LLMResponse(
        content=content,
        thinking=None,
        tool_calls=None,
        finish_reason="stop",
    )


@pytest.mark.asyncio
async def test_scripted_llm_records_stable_requests_and_returns_in_order():
    messages = [Message(role="user", content="before")]
    tools = [{"name": "echo", "input_schema": {"type": "object"}}]
    llm = ScriptedLLM([response("first"), response("second")])

    first = await llm.generate(messages, tools=tools)
    messages.append(Message(role="assistant", content="after"))
    tools[0]["name"] = "changed"
    second = await llm.generate(messages, tools=tools)

    assert first.content == "first"
    assert second.content == "second"
    assert [message.content for message in llm.requests[0].messages] == ["before"]
    assert llm.requests[0].tools == ({"name": "echo", "input_schema": {"type": "object"}},)
    assert len(llm.requests[1].messages) == 2
    llm.assert_complete()


@pytest.mark.asyncio
async def test_response_exhaustion_remains_visible_after_caller_catches_error():
    llm = ScriptedLLM([])

    with pytest.raises(AssertionError, match="scripted responses exhausted"):
        await llm.generate([Message(role="user", content="unexpected")])

    with pytest.raises(AssertionError, match="Unexpected LLM call #1"):
        llm.assert_complete()


def test_unconsumed_response_fails_context_exit():
    with pytest.raises(AssertionError, match=r"1 scripted response\(s\) were not consumed"):
        with ScriptedLLM([response("unused")]):
            pass


@pytest.mark.asyncio
async def test_scripted_exception_is_consumed_without_becoming_a_violation():
    llm = ScriptedLLM([RuntimeError("planned failure")])

    with pytest.raises(RuntimeError, match="planned failure"):
        await llm.generate([Message(role="user", content="fail")])

    llm.assert_complete()
