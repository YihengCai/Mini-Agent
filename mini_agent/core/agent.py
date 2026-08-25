"""Session-owned conversation state and the UI-independent agent loop."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Mapping
from uuid import uuid4

from ..llm.protocol import ModelClient, ToolDefinition
from ..schema import Message
from ..tools.base import Tool
from .events import (
    AgentEvent,
    AgentEventEnvelope,
    AgentEventSink,
    ModelCallFailed,
    ModelRequest,
    ModelResponse,
    StepFinished,
    StepStatus,
    StepStarted,
    TurnFinished,
    TurnStarted,
)
from .turn import (
    TurnAlreadyActiveError,
    TurnError,
    TurnHandle,
    TurnOutcome,
    TurnStopReason,
)
from .tool_execution import InvalidToolBatchError, ToolBatchExecutor


@dataclass(frozen=True)
class _TurnContext:
    session_id: str
    turn_id: str
    interrupt_event: asyncio.Event
    llm: ModelClient
    tool_executor: ToolBatchExecutor
    max_steps: int


@dataclass(frozen=True)
class _StepResult:
    stop_reason: TurnStopReason | None
    last_assistant_message: str | None
    error: TurnError | None = None


class _TurnEmitter:
    """Bind all observations to one admitted Turn."""

    def __init__(
        self,
        context: _TurnContext,
        event_sink: AgentEventSink | None,
    ) -> None:
        self._context = context
        self._event_sink = event_sink
        self._error: Exception | None = None

    @property
    def error(self) -> Exception | None:
        return self._error

    def emit(self, event: AgentEvent, *, step: int | None = None) -> None:
        if self._event_sink is None or self._error is not None:
            return
        try:
            self._event_sink(
                AgentEventEnvelope(
                    session_id=self._context.session_id,
                    turn_id=self._context.turn_id,
                    step=step,
                    event=event,
                )
            )
        except Exception as error:
            self._error = error


class AgentSession:
    """One logical conversation containing an ordered sequence of Turns."""

    def __init__(
        self,
        llm_client: ModelClient,
        system_prompt: str,
        tools: list[Tool],
        max_steps: int = 50,
        workspace_dir: str = "./workspace",
        session_id: str | None = None,
    ):
        self._session_id = session_id or uuid4().hex
        self._llm = llm_client
        self._tool_executor = ToolBatchExecutor(tools)
        self._max_steps = max_steps
        self.workspace_dir = Path(workspace_dir)
        self._turn_counter = 0
        self._active_turn_id: str | None = None
        self._active_turn: TurnHandle | None = None
        self._loop = _AgentLoop()

        # Ensure workspace exists
        self.workspace_dir.mkdir(parents=True, exist_ok=True)

        # Inject workspace information into system prompt if not already present
        if "Current Workspace" not in system_prompt:
            workspace_info = f"\n\n## Current Workspace\nYou are currently working in: `{self.workspace_dir.absolute()}`\nAll relative paths will be resolved relative to this directory."
            system_prompt = system_prompt + workspace_info

        self.system_prompt = system_prompt

        # Initialize message history
        self._messages: list[Message] = [Message(role="system", content=system_prompt)]

        # Last adapter-reported usage is observation data only. It cannot drive
        # context policy until the configured endpoint's semantics are probed.
        self._api_total_tokens: int = 0

    @property
    def active_turn(self) -> TurnHandle | None:
        """Return the active Turn, if execution control is currently held."""

        if self._active_turn is not None and self._active_turn.done:
            self._release_turn(self._active_turn.turn_id)
        return self._active_turn

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def llm(self) -> ModelClient:
        return self._llm

    @property
    def tools(self) -> Mapping[str, Tool]:
        """Expose raw tools to trusted host code for inspection only.

        Every model-response call is dispatched through the Session-owned batch
        executor; this compatibility view is not that execution interface.
        """

        return self._tool_executor.tools

    @property
    def max_steps(self) -> int:
        return self._max_steps

    @property
    def api_total_tokens(self) -> int:
        return self._api_total_tokens

    def start_turn(
        self,
        user_input: str,
        *,
        event_sink: AgentEventSink | None = None,
    ) -> TurnHandle:
        """Atomically admit input and start one Turn for this conversation."""

        running_loop = asyncio.get_running_loop()
        if self._active_turn_id is not None:
            raise TurnAlreadyActiveError(self._active_turn_id)

        user_message = Message(role="user", content=user_input)
        next_turn_number = self._turn_counter + 1
        turn_id = f"{self.session_id}:turn-{next_turn_number}"
        interrupt_event = asyncio.Event()
        context = _TurnContext(
            session_id=self.session_id,
            turn_id=turn_id,
            interrupt_event=interrupt_event,
            llm=self._llm,
            tool_executor=self._tool_executor,
            max_steps=self._max_steps,
        )
        self._turn_counter = next_turn_number
        self._active_turn_id = turn_id
        self._messages.append(user_message)

        async def run_admitted_turn() -> TurnOutcome:
            try:
                return await self._loop.run_turn(
                    session=self,
                    context=context,
                    event_sink=event_sink,
                )
            finally:
                self._release_turn(turn_id)

        runner = run_admitted_turn()
        try:
            task = running_loop.create_task(runner)
        except BaseException:
            runner.close()
            self._messages.pop()
            self._turn_counter -= 1
            self._active_turn_id = None
            raise
        handle = TurnHandle(
            session_id=self.session_id,
            turn_id=turn_id,
            task=task,
            interrupt_event=interrupt_event,
        )
        if self._active_turn_id == turn_id:
            self._active_turn = handle
        return handle

    def _release_turn(self, turn_id: str) -> None:
        if self._active_turn_id == turn_id:
            self._active_turn_id = None
            self._active_turn = None

    def get_history(self) -> list[Message]:
        """Return an owned snapshot of the current model-visible history."""

        return [message.model_copy(deep=True) for message in self._messages]


class _AgentLoop:
    """Drive one admitted Turn as ordered Steps."""

    async def run_turn(
        self,
        *,
        session: AgentSession,
        context: _TurnContext,
        event_sink: AgentEventSink | None,
    ) -> TurnOutcome:
        emitter = _TurnEmitter(context, event_sink)
        turn_start_time = perf_counter()
        last_assistant_message: str | None = None
        emitter.emit(TurnStarted(max_steps=context.max_steps))

        try:
            if emitter.error is not None:
                return self._finish_turn(
                    emitter=emitter,
                    context=context,
                    stop_reason="failed",
                    last_assistant_message=None,
                    error=self._observer_error(emitter.error),
                )

            for step_number in range(1, context.max_steps + 1):
                if context.interrupt_event.is_set():
                    return self._finish_turn(
                        emitter=emitter,
                        context=context,
                        stop_reason="interrupted",
                        last_assistant_message=last_assistant_message,
                    )

                step_result = await self._run_step(
                    session=session,
                    context=context,
                    emitter=emitter,
                    step_number=step_number,
                    turn_start_time=turn_start_time,
                )
                if step_result.last_assistant_message is not None:
                    last_assistant_message = step_result.last_assistant_message
                if step_result.stop_reason is not None:
                    return self._finish_turn(
                        emitter=emitter,
                        context=context,
                        stop_reason=step_result.stop_reason,
                        last_assistant_message=last_assistant_message,
                        error=step_result.error,
                    )

            return self._finish_turn(
                emitter=emitter,
                context=context,
                stop_reason="max_steps",
                last_assistant_message=last_assistant_message,
            )
        except Exception as error:
            return self._finish_turn(
                emitter=emitter,
                context=context,
                stop_reason="failed",
                last_assistant_message=last_assistant_message,
                error=self._internal_error(error),
            )

    async def _run_step(
        self,
        *,
        session: AgentSession,
        context: _TurnContext,
        emitter: _TurnEmitter,
        step_number: int,
        turn_start_time: float,
    ) -> _StepResult:
        step_start_time = perf_counter()
        try:
            emitter.emit(
                StepStarted(max_steps=context.max_steps),
                step=step_number,
            )
            if emitter.error is not None:
                return self._observer_failed_step(emitter.error)

            model_tools = context.tool_executor.snapshot_definitions()
            event_tools = tuple(
                ToolDefinition(
                    name=tool.name,
                    description=tool.description,
                    parameters=deepcopy(tool.parameters),
                )
                for tool in model_tools
            )
            model_messages = [
                message.model_copy(deep=True) for message in session._messages
            ]
            event_messages = tuple(
                message.model_copy(deep=True) for message in model_messages
            )
            emitter.emit(
                ModelRequest(
                    messages=event_messages,
                    tools=event_tools,
                ),
                step=step_number,
            )
            if emitter.error is not None:
                return self._observer_failed_step(emitter.error)

            try:
                response = await context.llm.generate(
                    messages=model_messages,
                    tools=model_tools,
                )
            except Exception as error:
                from ..retry import RetryExhaustedError

                if isinstance(error, RetryExhaustedError):
                    error_message = (
                        f"LLM call failed after {error.attempts} retries\n"
                        f"Last error: {error.last_exception}"
                    )
                else:
                    error_message = f"LLM call failed: {error}"
                emitter.emit(
                    ModelCallFailed(
                        error=error,
                        result=error_message,
                    ),
                    step=step_number,
                )
                self._emit_step_finished(
                    emitter=emitter,
                    step_number=step_number,
                    status="failed",
                    step_start_time=step_start_time,
                    turn_start_time=turn_start_time,
                )
                return _StepResult(
                    stop_reason="failed",
                    last_assistant_message=None,
                    error=TurnError(kind="model_error", message=error_message),
                )

            if response.usage:
                # Telemetry only; no control policy depends on unprobed usage
                # semantics from a compatible endpoint.
                session._api_total_tokens = response.usage.total_tokens
            emitter.emit(
                ModelResponse(
                    response=response.model_copy(deep=True),
                ),
                step=step_number,
            )
            if emitter.error is not None:
                return self._observer_failed_step(emitter.error)

            assistant_message = Message(
                role="assistant",
                content=response.content,
                thinking=response.thinking,
                tool_calls=response.tool_calls,
            )
            if not response.tool_calls:
                session._messages.append(assistant_message)
                self._emit_step_finished(
                    emitter=emitter,
                    step_number=step_number,
                    status="end_turn",
                    step_start_time=step_start_time,
                    turn_start_time=turn_start_time,
                )
                return _StepResult(
                    stop_reason="end_turn",
                    last_assistant_message=response.content,
                )

            try:
                tool_messages = await context.tool_executor.execute_batch(
                    response.tool_calls,
                    emit=lambda event: emitter.emit(event, step=step_number),
                )
            except InvalidToolBatchError as error:
                self._emit_step_finished(
                    emitter=emitter,
                    step_number=step_number,
                    status="failed",
                    step_start_time=step_start_time,
                    turn_start_time=turn_start_time,
                )
                return _StepResult(
                    stop_reason="failed",
                    last_assistant_message=None,
                    error=TurnError(
                        kind="tool_protocol_error",
                        message=str(error),
                    ),
                )

            # Commit an assistant tool-call message and all corresponding results
            # together, so the next Turn never sees a half-written protocol pair.
            session._messages.extend([assistant_message, *tool_messages])

            if emitter.error is not None:
                status: StepStatus = "failed"
                stop_reason: TurnStopReason | None = "failed"
                step_error = self._observer_error(emitter.error)
            elif context.interrupt_event.is_set():
                status = "interrupted"
                stop_reason = "interrupted"
                step_error = None
            elif step_number == context.max_steps:
                status = "max_steps"
                stop_reason = "max_steps"
                step_error = None
            else:
                status = "continued"
                stop_reason = None
                step_error = None

            self._emit_step_finished(
                emitter=emitter,
                step_number=step_number,
                status=status,
                step_start_time=step_start_time,
                turn_start_time=turn_start_time,
            )
            if (
                emitter.error is not None
                and step_error is None
                and stop_reason is None
            ):
                stop_reason = "failed"
                step_error = self._observer_error(emitter.error)
            return _StepResult(
                stop_reason=stop_reason,
                last_assistant_message=response.content,
                error=step_error,
            )
        except Exception as error:
            self._emit_step_finished(
                emitter=emitter,
                step_number=step_number,
                status="failed",
                step_start_time=step_start_time,
                turn_start_time=turn_start_time,
            )
            return _StepResult(
                stop_reason="failed",
                last_assistant_message=None,
                error=self._internal_error(error),
            )

    @classmethod
    def _observer_failed_step(
        cls,
        error: Exception,
        *,
        last_assistant_message: str | None = None,
    ) -> _StepResult:
        return _StepResult(
            stop_reason="failed",
            last_assistant_message=last_assistant_message,
            error=cls._observer_error(error),
        )

    @staticmethod
    def _emit_step_finished(
        *,
        emitter: _TurnEmitter,
        step_number: int,
        status: StepStatus,
        step_start_time: float,
        turn_start_time: float,
    ) -> None:
        emitter.emit(
            StepFinished(
                status=status,
                elapsed_seconds=perf_counter() - step_start_time,
                total_elapsed_seconds=perf_counter() - turn_start_time,
            ),
            step=step_number,
        )

    @classmethod
    def _finish_turn(
        cls,
        *,
        emitter: _TurnEmitter,
        context: _TurnContext,
        stop_reason: TurnStopReason,
        last_assistant_message: str | None,
        error: TurnError | None = None,
    ) -> TurnOutcome:
        observer_error = None
        if emitter.error is not None and (
            error is None or error.kind != "observer_error"
        ):
            observer_error = cls._observer_error(emitter.error)

        outcome = TurnOutcome(
            session_id=context.session_id,
            turn_id=context.turn_id,
            stop_reason=stop_reason,
            last_assistant_message=last_assistant_message,
            error=error,
            observer_error=observer_error,
        )
        emitter.emit(TurnFinished(outcome=outcome))
        # Once a terminal outcome has been published, a delivery exception cannot
        # retroactively replace it without making event and waiter disagree.
        return outcome

    @staticmethod
    def _observer_error(error: Exception) -> TurnError:
        return TurnError(
            kind="observer_error",
            message=f"Agent event sink failed: {type(error).__name__}: {error}",
        )

    @staticmethod
    def _internal_error(error: Exception) -> TurnError:
        return TurnError(
            kind="internal_error",
            message=f"Internal agent loop error: {type(error).__name__}: {error}",
        )


__all__ = ["AgentSession"]
