"""Turn lifecycle values exposed by the core harness."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal, TypeAlias


TurnStopReason: TypeAlias = Literal[
    "end_turn",
    "interrupted",
    "max_steps",
    "failed",
]
TurnErrorKind: TypeAlias = Literal[
    "model_error",
    "internal_error",
    "observer_error",
]


@dataclass(frozen=True)
class TurnError:
    """Structured failure details for a turn that could not continue."""

    kind: TurnErrorKind
    message: str


@dataclass(frozen=True)
class TurnOutcome:
    """Why core stopped one turn; this does not judge task success."""

    session_id: str
    turn_id: str
    stop_reason: TurnStopReason
    last_assistant_message: str | None = None
    error: TurnError | None = None
    observer_error: TurnError | None = None

    def __post_init__(self) -> None:
        if self.stop_reason == "failed" and self.error is None:
            raise ValueError("A failed turn must include structured error details")
        if self.stop_reason != "failed" and self.error is not None:
            raise ValueError("Only a failed turn may include error details")
        if self.observer_error is not None and self.observer_error.kind != "observer_error":
            raise ValueError("observer_error must use the observer_error kind")


class TurnAlreadyActiveError(RuntimeError):
    """Raised when a session already has a turn holding execution control."""

    def __init__(self, active_turn_id: str):
        self.active_turn_id = active_turn_id
        super().__init__(f"Session already has an active turn: {active_turn_id}")


class TurnHandle:
    """Address and control one admitted turn without owning session state."""

    def __init__(
        self,
        *,
        session_id: str,
        turn_id: str,
        task: asyncio.Task[TurnOutcome],
        interrupt_event: asyncio.Event,
    ) -> None:
        self.session_id = session_id
        self.turn_id = turn_id
        self._task = task
        self._interrupt_event = interrupt_event

    @property
    def done(self) -> bool:
        return self._task.done()

    async def wait(self) -> TurnOutcome:
        """Wait without allowing caller cancellation to cancel the core runner."""

        return await asyncio.shield(self._task)

    def interrupt(self) -> bool:
        """Request cooperative interruption; the eventual reason may still win."""

        if self._task.done():
            return False
        self._interrupt_event.set()
        return True


__all__ = [
    "TurnAlreadyActiveError",
    "TurnError",
    "TurnErrorKind",
    "TurnHandle",
    "TurnOutcome",
    "TurnStopReason",
]
