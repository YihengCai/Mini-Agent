"""Synchronous observation events emitted by the agent loop.

Event payloads borrow the core objects for the duration of the callback. A sink
that needs to retain an event must copy or serialize the payload synchronously.
"""

from dataclasses import dataclass
from typing import Literal, Protocol, TypeAlias

from ..schema import LLMResponse, Message, ToolCall
from ..tools.base import Tool, ToolResult

ModelCallPurpose: TypeAlias = Literal["agent", "summary"]
RunFinishReason: TypeAlias = Literal[
    "completed",
    "cancelled",
    "model_error",
    "max_steps",
]


@dataclass(frozen=True)
class RunStarted:
    max_steps: int


@dataclass(frozen=True)
class RunFinished:
    reason: RunFinishReason
    result: str


@dataclass(frozen=True)
class StepStarted:
    step: int
    max_steps: int


@dataclass(frozen=True)
class StepFinished:
    step: int
    elapsed_seconds: float
    total_elapsed_seconds: float


@dataclass(frozen=True)
class ModelRequest:
    purpose: ModelCallPurpose
    messages: tuple[Message, ...]
    tools: tuple[Tool, ...]


@dataclass(frozen=True)
class ModelResponse:
    purpose: ModelCallPurpose
    response: LLMResponse


@dataclass(frozen=True)
class ModelCallFailed:
    purpose: ModelCallPurpose
    error: Exception
    result: str


@dataclass(frozen=True)
class ToolStarted:
    step: int
    index: int
    call: ToolCall


@dataclass(frozen=True)
class ToolFinished:
    step: int
    index: int
    call: ToolCall
    result: ToolResult


@dataclass(frozen=True)
class HistoryCleaned:
    removed_count: int


@dataclass(frozen=True)
class CompactionStarted:
    estimated_tokens: int
    reported_tokens: int
    token_limit: int


@dataclass(frozen=True)
class CompactionSkipped:
    reason: Literal["insufficient_messages"]


@dataclass(frozen=True)
class CompactionRoundFinished:
    round_number: int
    used_fallback: bool
    error: Exception | None = None


@dataclass(frozen=True)
class CompactionFinished:
    previous_tokens: int
    current_tokens: int
    user_message_count: int
    summary_count: int


AgentEvent: TypeAlias = (
    RunStarted
    | RunFinished
    | StepStarted
    | StepFinished
    | ModelRequest
    | ModelResponse
    | ModelCallFailed
    | ToolStarted
    | ToolFinished
    | HistoryCleaned
    | CompactionStarted
    | CompactionSkipped
    | CompactionRoundFinished
    | CompactionFinished
)


class AgentEventSink(Protocol):
    """Consume one event synchronously.

    Exceptions intentionally propagate to the caller. Best-effort handling is a
    policy for an adapter or a composed sink, not for the core loop.
    """

    def __call__(self, event: AgentEvent) -> None: ...


__all__ = [
    "AgentEvent",
    "AgentEventSink",
    "CompactionFinished",
    "CompactionRoundFinished",
    "CompactionSkipped",
    "CompactionStarted",
    "HistoryCleaned",
    "ModelCallFailed",
    "ModelCallPurpose",
    "ModelRequest",
    "ModelResponse",
    "RunFinished",
    "RunFinishReason",
    "RunStarted",
    "StepFinished",
    "StepStarted",
    "ToolFinished",
    "ToolStarted",
]
