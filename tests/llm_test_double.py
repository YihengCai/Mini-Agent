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
        self.requests.append(
            LLMRequestSnapshot(
                messages=tuple(deepcopy(messages)),
                tools=self._snapshot_tools(tools),
            )
        )

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
