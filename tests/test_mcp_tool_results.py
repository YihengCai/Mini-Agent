"""Offline contracts for MCP result conversion."""

import pytest
from mcp.types import CallToolResult, TextContent

from mini_agent.core.events import ToolFinished
from mini_agent.core.tool_execution import ToolBatchExecutor
from mini_agent.schema import FunctionCall, ToolCall
from mini_agent.tools.mcp_loader import MCPTool


class StubSession:
    def __init__(self, result: CallToolResult) -> None:
        self.result = result

    async def call_tool(self, name, arguments):
        return self.result


def mcp_result(*parts: str, is_error: bool) -> CallToolResult:
    return CallToolResult(
        isError=is_error,
        content=[TextContent(type="text", text=part) for part in parts],
    )


def mcp_tool(result: CallToolResult) -> MCPTool:
    return MCPTool(
        name="remote_tool",
        description="Return one prepared MCP result.",
        parameters={"type": "object"},
        session=StubSession(result),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "wire_result",
        "expected_success",
        "expected_content",
        "expected_error",
    ),
    [
        (mcp_result("first", "second", is_error=False), True, "first\nsecond", None),
        (
            mcp_result("permission denied: repository is read-only", is_error=True),
            False,
            "",
            "permission denied: repository is read-only",
        ),
        (mcp_result(is_error=True), False, "", "Tool returned error"),
    ],
)
async def test_mcp_tool_normalizes_wire_results(
    wire_result,
    expected_success,
    expected_content,
    expected_error,
):
    result = await mcp_tool(wire_result).execute(path="README.md")

    assert result.success is expected_success
    assert result.content == expected_content
    assert result.error == expected_error


@pytest.mark.asyncio
async def test_mcp_error_detail_reaches_event_and_model_message():
    detail = "permission denied: missing PROJECT_ID"
    executor = ToolBatchExecutor([mcp_tool(mcp_result(detail, is_error=True))])
    events = []
    call = ToolCall(
        id="mcp-error-1",
        type="function",
        function=FunctionCall(name="remote_tool", arguments={}),
    )

    messages = await executor.execute_batch([call], emit=events.append)

    finished = next(event for event in events if isinstance(event, ToolFinished))
    assert not finished.result.success
    assert finished.result.content == ""
    assert finished.result.error == detail
    assert messages[0].content == f"Error: {detail}"
