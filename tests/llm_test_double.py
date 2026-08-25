"""Test support for deterministic, offline LLM calls."""

from collections import deque
from copy import deepcopy
from dataclasses import dataclass
from typing import Iterable, Literal

from mini_agent.llm.protocol import ToolDefinition
from mini_agent.schema import LLMResponse, Message


CallPurpose = Literal["agent", "summary"]


@dataclass(frozen=True)
class LLMRequestSnapshot:
    """Immutable-at-capture view of one internal LLM request."""

    purpose: CallPurpose
    messages: tuple[Message, ...]
    tools: tuple[ToolDefinition, ...] | None


ScriptedResult = LLMResponse | Exception


@dataclass(frozen=True)
class ScriptedCall:
    """One expected call in the global model-call sequence."""

    purpose: CallPurpose
    result: ScriptedResult


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

    def __init__(self, calls: Iterable[ScriptedCall]):
        self._calls = deque(calls)
        self._violation: str | None = None
        self.requests: list[LLMRequestSnapshot] = []

    async def generate(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
    ) -> LLMResponse:
        """Record the request and consume exactly one scripted result."""
        actual_purpose: CallPurpose = "summary" if tools is None else "agent"
        request = LLMRequestSnapshot(
            purpose=actual_purpose,
            messages=tuple(deepcopy(messages)),
            tools=self._snapshot_tools(tools),
        )
        self.requests.append(request)

        if self._violation is not None:
            raise AssertionError(self._violation)

        try:
            validate_tool_call_pairs(request.messages)
        except AssertionError as error:
            violation = f"Invalid LLM request #{len(self.requests)}: {error}"
            self._violation = violation
            raise AssertionError(violation) from error

        if not self._calls:
            violation = f"Unexpected LLM call #{len(self.requests)}: scripted calls exhausted"
            self._violation = violation
            raise AssertionError(violation)

        expected_call = self._calls[0]
        if actual_purpose != expected_call.purpose:
            violation = (
                f"Unexpected LLM call #{len(self.requests)}: expected "
                f"{expected_call.purpose!r}, got {actual_purpose!r}"
            )
            self._violation = violation
            raise AssertionError(violation)

        result = self._calls.popleft().result
        if isinstance(result, Exception):
            raise result
        return result.model_copy(deep=True)

    def assert_complete(self) -> None:
        """Fail if an unexpected call occurred or scripted results remain."""
        problems = [self._violation] if self._violation is not None else []
        if self._calls:
            problems.append(f"{len(self._calls)} scripted call(s) were not consumed")
        if problems:
            raise AssertionError("Scripted LLM verification failed:\n- " + "\n- ".join(problems))

    def __enter__(self) -> "ScriptedLLM":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        try:
            self.assert_complete()
        except AssertionError as verification_error:
            if exc_value is not None:
                raise verification_error from exc_value
            raise
        return False

    @staticmethod
    def _snapshot_tools(
        tools: list[ToolDefinition] | None,
    ) -> tuple[ToolDefinition, ...] | None:
        if tools is None:
            return None

        return tuple(deepcopy(tools))
