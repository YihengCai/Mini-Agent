"""Synchronous, turn-scoped observation events emitted by the agent loop."""

from dataclasses import dataclass
from typing import Any, Literal, Protocol, TypeAlias

from ..schema import LLMResponse, Message, ToolCall
from ..tools.base import ToolResult
from .turn import TurnOutcome

ModelCallPurpose: TypeAlias = Literal["agent", "summary"]
StepStatus: TypeAlias = Literal[
    "continued",
    "end_turn",
    "interrupted",
    "max_steps",
    "failed",
]


@dataclass(frozen=True)
class TurnStarted:
    max_steps: int


@dataclass(frozen=True)
class TurnFinished:
    outcome: TurnOutcome


@dataclass(frozen=True)
class StepStarted:
    max_steps: int


@dataclass(frozen=True)
class StepFinished:
    status: StepStatus
    elapsed_seconds: float
    total_elapsed_seconds: float


@dataclass(frozen=True)
class ToolDefinition:
    """Owned snapshot of one tool definition exposed to the model."""

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class ModelRequest:
    purpose: ModelCallPurpose
    messages: tuple[Message, ...]
    tools: tuple[ToolDefinition, ...]


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
    index: int
    call: ToolCall


@dataclass(frozen=True)
class ToolFinished:
    index: int
    call: ToolCall
    result: ToolResult


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
    TurnStarted
    | TurnFinished
    | StepStarted
    | StepFinished
    | ModelRequest
    | ModelResponse
    | ModelCallFailed
    | ToolStarted
    | ToolFinished
    | CompactionStarted
    | CompactionSkipped
    | CompactionRoundFinished
    | CompactionFinished
)


@dataclass(frozen=True)
class AgentEventEnvelope:
    """Attach one observation to its Session, Turn, and optional Step."""

    session_id: str
    turn_id: str
    step: int | None
    event: AgentEvent


class AgentEventSink(Protocol):
    """Consume one scoped event synchronously.

    The loop captures the first callback exception and disables that sink. If it
    prevents execution from continuing, the Turn fails at a safe boundary; if a
    terminal cause already exists, the observer failure is secondary metadata.
    """

    def __call__(self, event: AgentEventEnvelope) -> None: ...


__all__ = [
    "AgentEvent",
    "AgentEventEnvelope",
    "AgentEventSink",
    "CompactionFinished",
    "CompactionRoundFinished",
    "CompactionSkipped",
    "CompactionStarted",
    "ModelCallFailed",
    "ModelCallPurpose",
    "ModelRequest",
    "ModelResponse",
    "StepStatus",
    "StepFinished",
    "StepStarted",
    "ToolDefinition",
    "ToolFinished",
    "ToolStarted",
    "TurnFinished",
    "TurnStarted",
]
