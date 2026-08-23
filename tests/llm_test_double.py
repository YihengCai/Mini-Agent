"""Test support for deterministic, offline LLM calls."""

from collections import deque
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Iterable

from mini_agent.schema import LLMResponse, Message


@dataclass(frozen=True)
class LLMRequestSnapshot:
    """Immutable-at-capture view of one internal LLM request."""

    messages: tuple[Message, ...]
    tools: tuple[Any, ...] | None


ScriptedResult = LLMResponse | Exception


def validate_tool_call_pairs(messages: tuple[Message, ...] | list[Message]) -> None:
    """Check that every tool call has exactly one later result with the same ID."""
    seen_calls: dict[str, int] = {}
    pending_calls: dict[str, int] = {}

    for message_index, message in enumerate(messages):
        for tool_call in message.tool_calls or []:
            call_id = tool_call.id
            if not call_id:
                raise AssertionError(f"tool call at message {message_index} has an empty ID")
            if call_id in seen_calls:
                first_index = seen_calls[call_id]
                raise AssertionError(
                    f"duplicate tool call ID {call_id!r} at message {message_index}; "
                    f"first declared at message {first_index}"
                )
            seen_calls[call_id] = message_index
            pending_calls[call_id] = message_index

        if message.role != "tool":
            continue

        call_id = message.tool_call_id
        if not call_id:
            raise AssertionError(f"tool result at message {message_index} has no tool_call_id")
        if call_id not in seen_calls:
            raise AssertionError(
                f"tool result at message {message_index} references unknown tool call {call_id!r}"
            )
        if call_id not in pending_calls:
            raise AssertionError(
                f"duplicate tool result for {call_id!r} at message {message_index}"
            )
        del pending_calls[call_id]

    if pending_calls:
        missing = ", ".join(
            f"{call_id!r} (message {message_index})"
            for call_id, message_index in pending_calls.items()
        )
        raise AssertionError(f"tool call(s) missing results: {missing}")


class ScriptedLLM:
    """Return scripted results while recording every internal LLM request."""

    def __init__(self, results: Iterable[ScriptedResult]):
        self._results = deque(results)
        self._violations: list[str] = []
        self.requests: list[LLMRequestSnapshot] = []

    async def generate(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
    ) -> LLMResponse:
        """Record the request and consume exactly one scripted result."""
        request = LLMRequestSnapshot(
            messages=tuple(deepcopy(messages)),
            tools=self._snapshot_tools(tools),
        )
        self.requests.append(request)

        try:
            validate_tool_call_pairs(request.messages)
        except AssertionError as error:
            violation = f"Invalid LLM request #{len(self.requests)}: {error}"
            self._violations.append(violation)
            raise AssertionError(violation) from error

        if not self._results:
            violation = f"Unexpected LLM call #{len(self.requests)}: scripted responses exhausted"
            self._violations.append(violation)
            raise AssertionError(violation)

        result = self._results.popleft()
        if isinstance(result, Exception):
            raise result
        return result.model_copy(deep=True)

    def assert_complete(self) -> None:
        """Fail if an unexpected call occurred or scripted results remain."""
        problems = list(self._violations)
        if self._results:
            problems.append(f"{len(self._results)} scripted response(s) were not consumed")
        if problems:
            raise AssertionError("Scripted LLM verification failed:\n- " + "\n- ".join(problems))

    def __enter__(self) -> "ScriptedLLM":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        if exc_type is None:
            self.assert_complete()
        return False

    @staticmethod
    def _snapshot_tools(tools: list[Any] | None) -> tuple[Any, ...] | None:
        if tools is None:
            return None

        snapshots: list[Any] = []
        for tool in tools:
            to_schema = getattr(tool, "to_schema", None)
            snapshot = to_schema() if callable(to_schema) else tool
            snapshots.append(deepcopy(snapshot))
        return tuple(snapshots)
