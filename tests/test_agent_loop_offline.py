"""Offline regression tests for the agent loop and its LLM test double."""

from unittest.mock import MagicMock

import pytest

from mini_agent.agent import Agent
from mini_agent.schema import FunctionCall, LLMResponse, Message, ToolCall
from mini_agent.tools.base import Tool, ToolResult
from tests.llm_test_double import ScriptedLLM


def response(
    content: str,
    *,
    tool_calls: list[ToolCall] | None = None,
    finish_reason: str = "stop",
) -> LLMResponse:
    return LLMResponse(
        content=content,
        thinking=None,
        tool_calls=tool_calls,
        finish_reason=finish_reason,
    )


class EchoTool(Tool):
    def __init__(self):
        self.calls: list[str] = []

    @property
    def name(self) -> str:
        return "echo"

    @property
    def description(self) -> str:
        return "Return the supplied text."

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        }

    async def execute(self, text: str) -> ToolResult:
        self.calls.append(text)
        return ToolResult(success=True, content=f"echo:{text}")


def build_agent(monkeypatch, tmp_path, llm, tools, *, max_steps: int = 3) -> Agent:
    logger = MagicMock()
    monkeypatch.setattr("mini_agent.agent.AgentLogger", lambda: logger)
    agent = Agent(
        llm_client=llm,
        system_prompt="You are a test agent.",
        tools=tools,
        max_steps=max_steps,
        workspace_dir=str(tmp_path),
    )
    monkeypatch.setattr(agent, "_estimate_tokens", lambda: 0)
    return agent


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


@pytest.mark.asyncio
async def test_real_agent_loop_executes_tool_and_sends_result_to_model(monkeypatch, tmp_path):
    call = ToolCall(
        id="call-1",
        type="function",
        function=FunctionCall(name="echo", arguments={"text": "ping"}),
    )
    llm = ScriptedLLM(
        [
            response("", tool_calls=[call], finish_reason="tool_use"),
            response("finished"),
        ]
    )
    tool = EchoTool()
    agent = build_agent(monkeypatch, tmp_path, llm, [tool])
    agent.add_user_message("Echo ping, then finish.")

    with llm:
        result = await agent.run()

    assert result == "finished"
    assert tool.calls == ["ping"]
    assert [message.role for message in llm.requests[0].messages] == ["system", "user"]
    assert [message.role for message in llm.requests[1].messages] == [
        "system",
        "user",
        "assistant",
        "tool",
    ]
    assistant_message = llm.requests[1].messages[-2]
    tool_message = llm.requests[1].messages[-1]
    assert assistant_message.tool_calls == [call]
    assert tool_message.tool_call_id == "call-1"
    assert tool_message.name == "echo"
    assert tool_message.content == "echo:ping"
    assert llm.requests[0].tools == (
        {
            "name": "echo",
            "description": "Return the supplied text.",
            "input_schema": tool.parameters,
        },
    )
