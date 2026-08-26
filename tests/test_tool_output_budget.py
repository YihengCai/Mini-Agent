"""Offline contracts for model-facing tool output budgets."""

from __future__ import annotations

import re

import pytest

from mini_agent.core import AgentSession
from mini_agent.core.events import ToolFinished, ToolStarted
from mini_agent.core.tool_output import (
    MAX_TOOL_MESSAGE_BYTES,
    truncate_tool_message,
)
from mini_agent.schema import FunctionCall, LLMResponse, ToolCall
from mini_agent.tools.base import Tool, ToolResult
from tests.llm_test_double import ScriptedCall, ScriptedLLM


TRUNCATION_MARKER = re.compile(
    r"\n\n\[Tool output truncated: "
    r"original_bytes=(?P<original>\d+); "
    r"retained_bytes=(?P<retained>\d+); "
    r"omitted_bytes=(?P<omitted>\d+); "
    r"limit_bytes=(?P<limit>\d+)\]\n\n"
)


def tool_response(*calls: ToolCall) -> LLMResponse:
    return LLMResponse(
        content="",
        tool_calls=list(calls),
        finish_reason="tool_use",
        usage=None,
    )


def final_response(content: str = "done") -> LLMResponse:
    return LLMResponse(
        content=content,
        tool_calls=[],
        finish_reason="stop",
        usage=None,
    )


def result_call(call_id: str, key: str) -> ToolCall:
    return ToolCall(
        id=call_id,
        type="function",
        function=FunctionCall(name="result", arguments={"key": key}),
    )


def assert_truncation_metadata(output: str, original: str) -> None:
    match = TRUNCATION_MARKER.search(output)
    assert match is not None

    marker_bytes = len(match.group(0).encode("utf-8"))
    output_bytes = len(output.encode("utf-8"))
    original_bytes = len(original.encode("utf-8"))
    retained_bytes = output_bytes - marker_bytes

    assert output_bytes <= MAX_TOOL_MESSAGE_BYTES
    assert int(match.group("original")) == original_bytes
    assert int(match.group("retained")) == retained_bytes
    assert int(match.group("omitted")) == original_bytes - retained_bytes
    assert int(match.group("limit")) == MAX_TOOL_MESSAGE_BYTES


class ResultTool(Tool):
    def __init__(self, results: dict[str, ToolResult]) -> None:
        self.results = results
        self.calls: list[str] = []

    @property
    def name(self) -> str:
        return "result"

    @property
    def description(self) -> str:
        return "Return one prepared result."

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
        }

    async def execute(self, key: str) -> ToolResult:
        self.calls.append(key)
        return self.results[key]


def test_tool_message_budget_preserves_boundary_and_marks_first_overflow() -> None:
    exact = "x" * MAX_TOOL_MESSAGE_BYTES
    overflow = "HEAD|" + ("x" * MAX_TOOL_MESSAGE_BYTES) + "|TAIL"

    assert truncate_tool_message(exact) == exact
    assert TRUNCATION_MARKER.search(exact) is None

    projected = truncate_tool_message(overflow)

    assert projected.startswith("HEAD|")
    assert projected.endswith("|TAIL")
    assert_truncation_metadata(projected, overflow)


def test_tool_message_budget_counts_utf8_without_splitting_characters() -> None:
    overflow = "开头|" + ("界🙂é" * MAX_TOOL_MESSAGE_BYTES) + "|结尾"

    projected = truncate_tool_message(overflow)

    assert projected.startswith("开头|")
    assert projected.endswith("|结尾")
    assert "\ufffd" not in projected
    assert_truncation_metadata(projected, overflow)


@pytest.mark.asyncio
async def test_batch_keeps_raw_events_but_bounds_history_and_next_request() -> None:
    success = "success-head|" + ("s" * MAX_TOOL_MESSAGE_BYTES) + "|success-tail"
    exact = "e" * MAX_TOOL_MESSAGE_BYTES
    failure = "failure-head|" + ("f" * MAX_TOOL_MESSAGE_BYTES) + "|failure-tail"
    prepared = {
        "success": ToolResult(success=True, content=success),
        "exact": ToolResult(success=True, content=exact),
        "failure": ToolResult(success=False, error=failure),
    }
    calls = [
        result_call("success-call", "success"),
        result_call("exact-call", "exact"),
        result_call("failure-call", "failure"),
    ]
    llm = ScriptedLLM(
        [ScriptedCall(tool_response(*calls)), ScriptedCall(final_response())]
    )
    tool = ResultTool(prepared)
    session = AgentSession(
        llm_client=llm,
        system_prompt="You are a test agent.",
        tools=[tool],
        max_steps=2,
        session_id="tool-output-budget-session",
    )
    raw_events: list[ToolResult] = []
    event_order: list[tuple[str, int]] = []

    def observe(envelope) -> None:
        if isinstance(envelope.event, ToolStarted):
            event_order.append(("started", envelope.event.index))
        elif isinstance(envelope.event, ToolFinished):
            event_order.append(("finished", envelope.event.index))
            raw_events.append(envelope.event.result.model_copy(deep=True))
            envelope.event.result.content = "observer-mutated-content"
            envelope.event.result.error = "observer-mutated-error"

    with llm:
        outcome = await session.start_turn(
            "exercise model-facing output limits",
            event_sink=observe,
        ).wait()

    assert outcome.stop_reason == "end_turn"
    assert tool.calls == ["success", "exact", "failure"]
    assert event_order == [
        ("started", 1),
        ("finished", 1),
        ("started", 2),
        ("finished", 2),
        ("started", 3),
        ("finished", 3),
    ]
    assert [result.content for result in raw_events] == [success, exact, ""]
    assert [result.error for result in raw_events] == [None, None, failure]
    assert prepared["success"].content == success
    assert prepared["failure"].error == failure

    history = [message for message in session.get_history() if message.role == "tool"]
    next_request = [
        message for message in llm.requests[1].messages if message.role == "tool"
    ]
    assert next_request == history
    assert [message.tool_call_id for message in history] == [
        "success-call",
        "exact-call",
        "failure-call",
    ]
    assert [message.name for message in history] == ["result", "result", "result"]

    success_projection, exact_projection, failure_projection = [
        message.content for message in history
    ]
    assert success_projection.startswith("success-head|")
    assert success_projection.endswith("|success-tail")
    assert_truncation_metadata(success_projection, success)
    assert exact_projection == exact
    assert failure_projection.startswith("Error: failure-head|")
    assert failure_projection.endswith("|failure-tail")
    assert_truncation_metadata(failure_projection, f"Error: {failure}")
