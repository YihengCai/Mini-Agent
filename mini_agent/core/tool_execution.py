"""Session-owned tool registration and batch execution boundary."""

from __future__ import annotations

import traceback
from collections.abc import Callable, Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType

from ..llm.protocol import ToolDefinition
from ..schema import Message, ToolCall
from ..tools.base import Tool, ToolResult
from .events import ToolFinished, ToolStarted


class InvalidToolBatchError(ValueError):
    """A model response contains tool calls that cannot be paired safely."""


@dataclass(frozen=True)
class _RegisteredTool:
    definition: ToolDefinition
    implementation: Tool


ToolEvent = ToolStarted | ToolFinished
ToolEventSink = Callable[[ToolEvent], None]


class ToolBatchExecutor:
    """Freeze one tool registry and execute validated model-requested batches."""

    def __init__(self, tools: Iterable[Tool]) -> None:
        registered: dict[str, _RegisteredTool] = {}
        for tool in tools:
            name = tool.name
            if not isinstance(name, str) or not name:
                raise ValueError("Tool name must be a non-empty string")
            if name in registered:
                raise ValueError(f"Duplicate tool name: {name!r}")

            description = tool.description
            if not isinstance(description, str):
                raise TypeError(f"Tool {name!r} description must be a string")
            parameters = tool.parameters
            if not isinstance(parameters, dict):
                raise TypeError(f"Tool {name!r} parameters must be a dictionary")

            registered[name] = _RegisteredTool(
                definition=ToolDefinition(
                    name=name,
                    description=description,
                    parameters=deepcopy(parameters),
                ),
                implementation=tool,
            )

        self._registered: Mapping[str, _RegisteredTool] = MappingProxyType(registered)
        self._tools: Mapping[str, Tool] = MappingProxyType(
            {
                name: registered_tool.implementation
                for name, registered_tool in registered.items()
            }
        )
        self._claimed_call_ids: set[str] = set()

    @property
    def tools(self) -> Mapping[str, Tool]:
        """Return raw implementations for trusted host inspection only.

        Model-response calls must use ``execute_batch`` so validation, call-ID
        ownership, events, and result normalization stay on one path.
        """

        return self._tools

    def snapshot_definitions(self) -> list[ToolDefinition]:
        """Return an owned model-facing snapshot of the frozen definitions."""

        return [
            ToolDefinition(
                name=registered_tool.definition.name,
                description=registered_tool.definition.description,
                parameters=deepcopy(registered_tool.definition.parameters),
            )
            for registered_tool in self._registered.values()
        ]

    async def execute_batch(
        self,
        tool_calls: list[ToolCall],
        *,
        emit: ToolEventSink,
    ) -> tuple[Message, ...]:
        """Validate a complete batch, then execute every call in model order."""

        self._validate_batch(tool_calls)

        messages: list[Message] = []
        for index, tool_call in enumerate(tool_calls, start=1):
            # Validation covers the complete batch before any side effect, while
            # ownership is claimed only when this individual call is about to
            # start. A cancelled earlier call therefore cannot consume IDs for
            # later calls that never started.
            self._claimed_call_ids.add(tool_call.id)
            emit(
                ToolStarted(
                    index=index,
                    call=tool_call.model_copy(deep=True),
                )
            )
            result = await self._execute_call(tool_call)
            emit(
                ToolFinished(
                    index=index,
                    call=tool_call.model_copy(deep=True),
                    result=result.model_copy(deep=True),
                )
            )
            messages.append(
                Message(
                    role="tool",
                    content=(
                        result.content if result.success else f"Error: {result.error}"
                    ),
                    tool_call_id=tool_call.id,
                    name=tool_call.function.name,
                )
            )

        return tuple(messages)

    def _validate_batch(self, tool_calls: list[ToolCall]) -> None:
        batch_ids: set[str] = set()
        for index, tool_call in enumerate(tool_calls, start=1):
            if tool_call.type != "function":
                raise InvalidToolBatchError(
                    "Invalid tool call batch: "
                    f"tool call {index} has unsupported type {tool_call.type!r}"
                )

            call_id = tool_call.id
            if not call_id:
                raise InvalidToolBatchError(
                    f"Invalid tool call batch: tool call {index} has an empty ID"
                )
            if call_id in batch_ids:
                raise InvalidToolBatchError(
                    "Invalid tool call batch: "
                    f"duplicate tool call ID {call_id!r} at index {index}"
                )
            if call_id in self._claimed_call_ids:
                raise InvalidToolBatchError(
                    "Invalid tool call batch: "
                    f"tool call ID {call_id!r} was already claimed in this Session"
                )
            batch_ids.add(call_id)

    async def _execute_call(self, tool_call: ToolCall) -> ToolResult:
        function_name = tool_call.function.name
        registered_tool = self._registered.get(function_name)
        if registered_tool is None:
            return ToolResult(
                success=False,
                content="",
                error=f"Unknown tool: {function_name}",
            )

        try:
            raw_result = await registered_tool.implementation.execute(
                **deepcopy(tool_call.function.arguments)
            )
            if isinstance(raw_result, ToolResult):
                return raw_result
            return ToolResult(
                success=False,
                content="",
                error=(
                    f"Tool contract violation: {function_name} returned "
                    f"{type(raw_result).__name__}, expected ToolResult"
                ),
            )
        except Exception as error:
            error_detail = f"{type(error).__name__}: {error}"
            return ToolResult(
                success=False,
                content="",
                error=(
                    f"Tool execution failed: {error_detail}\n\n"
                    f"Traceback:\n{traceback.format_exc()}"
                ),
            )


__all__ = ["InvalidToolBatchError", "ToolBatchExecutor"]
