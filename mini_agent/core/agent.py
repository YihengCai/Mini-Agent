"""Session-owned conversation state and the UI-independent agent loop."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from types import MappingProxyType
from typing import Mapping
from uuid import uuid4

import tiktoken

from ..llm.protocol import ModelClient, ToolDefinition
from ..schema import Message
from ..tools.base import Tool, ToolResult
from .events import (
    AgentEvent,
    AgentEventEnvelope,
    AgentEventSink,
    CompactionFinished,
    CompactionRoundFinished,
    CompactionSkipped,
    CompactionStarted,
    ModelCallFailed,
    ModelRequest,
    ModelResponse,
    StepFinished,
    StepStatus,
    StepStarted,
    ToolFinished,
    ToolStarted,
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


@dataclass(frozen=True)
class _TurnContext:
    session_id: str
    turn_id: str
    interrupt_event: asyncio.Event
    llm: ModelClient
    tools: Mapping[str, Tool]
    max_steps: int
    token_limit: int | None


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
        token_limit: int | None = None,
        session_id: str | None = None,
    ):
        if token_limit is not None and token_limit <= 0:
            raise ValueError("token_limit must be greater than zero when enabled")
        self._session_id = session_id or uuid4().hex
        self._llm = llm_client
        self._tools = {tool.name: tool for tool in tools}
        self._max_steps = max_steps
        self._token_limit = token_limit
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
        # Flag to skip token check right after summary (avoid consecutive triggers)
        self._skip_next_token_check: bool = False

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
        """Expose the configured tools without exposing the mutable registry."""

        return MappingProxyType(self._tools)

    @property
    def max_steps(self) -> int:
        return self._max_steps

    @property
    def token_limit(self) -> int | None:
        return self._token_limit

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
            tools=MappingProxyType(dict(self._tools)),
            max_steps=self._max_steps,
            token_limit=self._token_limit,
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

    def _estimate_tokens(self) -> int:
        """Estimate history size for the opt-in local compaction heuristic.

        ``cl100k_base`` is a legacy local approximation, not a claim about the
        configured model's tokenizer or context window.
        """
        try:
            encoding = tiktoken.get_encoding("cl100k_base")
        except Exception:
            # Fallback: if tiktoken initialization fails, use simple estimation
            return self._estimate_tokens_fallback()

        total_tokens = 0

        for msg in self._messages:
            # Count text content
            if isinstance(msg.content, str):
                total_tokens += len(encoding.encode(msg.content))
            elif isinstance(msg.content, list):
                for block in msg.content:
                    if isinstance(block, dict):
                        # Convert dict to string for calculation
                        total_tokens += len(encoding.encode(str(block)))

            # Count thinking
            if msg.thinking:
                total_tokens += len(encoding.encode(msg.thinking))

            # Count tool_calls
            if msg.tool_calls:
                total_tokens += len(encoding.encode(str(msg.tool_calls)))

            # Metadata overhead per message (approximately 4 tokens)
            total_tokens += 4

        return total_tokens

    def _estimate_tokens_fallback(self) -> int:
        """Fallback token estimation method (when tiktoken is unavailable)"""
        total_chars = 0
        for msg in self._messages:
            if isinstance(msg.content, str):
                total_chars += len(msg.content)
            elif isinstance(msg.content, list):
                for block in msg.content:
                    if isinstance(block, dict):
                        total_chars += len(str(block))

            if msg.thinking:
                total_chars += len(msg.thinking)

            if msg.tool_calls:
                total_chars += len(str(msg.tool_calls))

        # Rough estimation: average 2.5 characters = 1 token
        return int(total_chars / 2.5)

    async def _summarize_messages(
        self,
        context: _TurnContext,
        emitter: _TurnEmitter,
    ) -> None:
        """Message history summarization: summarize conversations between user messages when tokens exceed limit

        Strategy (Agent mode):
        - Keep all user messages (these are user intents)
        - Summarize content between each user-user pair (agent execution process)
        - If last round is still executing (has agent/tool messages but no next user), also summarize
        - Structure: system -> user1 -> summary1 -> user2 -> summary2 -> user3 -> summary3 (if executing)

        Summary is triggered only when the opt-in local estimate exceeds its
        configured limit. Adapter-reported usage remains observation data.
        """
        if context.token_limit is None:
            return

        # Avoid immediately evaluating the rewritten history again.
        if self._skip_next_token_check:
            self._skip_next_token_check = False
            return

        estimated_tokens = self._estimate_tokens()

        should_summarize = estimated_tokens > context.token_limit

        # If the local estimate is within budget, no summary is needed.
        if not should_summarize:
            return

        emitter.emit(
            CompactionStarted(
                estimated_tokens=estimated_tokens,
                reported_tokens=self._api_total_tokens,
                token_limit=context.token_limit,
            ),
        )
        if emitter.error is not None:
            return

        # Find all user message indices (skip system prompt)
        user_indices = [
            i
            for i, msg in enumerate(self._messages)
            if msg.role == "user" and i > 0
        ]

        # Need at least 1 user message to perform summary
        if len(user_indices) < 1:
            emitter.emit(CompactionSkipped(reason="insufficient_messages"))
            return

        # Build new message list
        new_messages = [self._messages[0]]  # Keep system prompt
        summary_count = 0

        # Iterate through each user message and summarize the execution process after it
        for i, user_idx in enumerate(user_indices):
            # Add current user message
            new_messages.append(self._messages[user_idx])

            # Determine message range to summarize
            # If last user, go to end of message list; otherwise to before next user
            if i < len(user_indices) - 1:
                next_user_idx = user_indices[i + 1]
            else:
                next_user_idx = len(self._messages)

            # Extract execution messages for this round
            execution_messages = self._messages[user_idx + 1 : next_user_idx]

            # If there are execution messages in this round, summarize them
            if execution_messages:
                summary_text = await self._create_summary(
                    execution_messages,
                    i + 1,
                    context,
                    emitter,
                )
                if emitter.error is not None:
                    return
                if summary_text:
                    summary_message = Message(
                        role="user",
                        content=f"[Assistant Execution Summary]\n\n{summary_text}",
                    )
                    new_messages.append(summary_message)
                    summary_count += 1

        # Replace message list
        self._messages = new_messages

        # Skip one check to avoid consecutive summary triggers.
        self._skip_next_token_check = True

        new_tokens = self._estimate_tokens()
        emitter.emit(
            CompactionFinished(
                previous_tokens=estimated_tokens,
                current_tokens=new_tokens,
                user_message_count=len(user_indices),
                summary_count=summary_count,
            ),
        )

    async def _create_summary(
        self,
        messages: list[Message],
        round_num: int,
        context: _TurnContext,
        emitter: _TurnEmitter,
    ) -> str:
        """Create summary for one execution round

        Args:
            messages: List of messages to summarize
            round_num: Round number

        Returns:
            Summary text
        """
        if not messages:
            return ""

        # Build summary content
        summary_content = f"Round {round_num} execution process:\n\n"
        for msg in messages:
            if msg.role == "assistant":
                content_text = msg.content if isinstance(msg.content, str) else str(msg.content)
                summary_content += f"Assistant: {content_text}\n"
                if msg.tool_calls:
                    tool_names = [tc.function.name for tc in msg.tool_calls]
                    summary_content += f"  → Called tools: {', '.join(tool_names)}\n"
            elif msg.role == "tool":
                result_preview = msg.content if isinstance(msg.content, str) else str(msg.content)
                summary_content += f"  ← Tool returned: {result_preview}...\n"

        summary_prompt = f"""Please provide a concise summary of the following Agent execution process:

{summary_content}

Requirements:
1. Focus on what tasks were completed and which tools were called
2. Keep key execution results and important findings
3. Be concise and clear, within 1000 words
4. Use English
5. Do not include "user" related content, only summarize the Agent's execution process"""

        summary_messages = [
            Message(
                role="system",
                content="You are an assistant skilled at summarizing Agent execution processes.",
            ),
            Message(role="user", content=summary_prompt),
        ]
        emitter.emit(
            ModelRequest(
                purpose="summary",
                messages=tuple(
                    message.model_copy(deep=True) for message in summary_messages
                ),
                tools=(),
            )
        )
        if emitter.error is not None:
            return ""
        try:
            response = await context.llm.generate(messages=summary_messages)
        except Exception as e:
            error_msg = f"Summary generation failed for round {round_num}: {e}"
            emitter.emit(
                ModelCallFailed(
                    purpose="summary",
                    error=e,
                    result=error_msg,
                ),
            )
            emitter.emit(
                CompactionRoundFinished(
                    round_number=round_num,
                    used_fallback=True,
                    error=e,
                ),
            )
            # Use simple text summary on failure
            return summary_content

        emitter.emit(
            ModelResponse(
                purpose="summary",
                response=response.model_copy(deep=True),
            )
        )
        emitter.emit(
            CompactionRoundFinished(
                round_number=round_num,
                used_fallback=False,
            )
        )
        return response.content

    def get_history(self) -> list[Message]:
        """Return an owned snapshot of the current model-visible history."""

        return [message.model_copy(deep=True) for message in self._messages]


class _AgentLoop:
    """Drive one admitted Turn as ordered agent-purpose Steps."""

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

                # Compaction is Turn maintenance, not an agent-purpose Step.
                await session._summarize_messages(context, emitter)
                if emitter.error is not None:
                    return self._finish_turn(
                        emitter=emitter,
                        context=context,
                        stop_reason="failed",
                        last_assistant_message=last_assistant_message,
                        error=self._observer_error(emitter.error),
                    )
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

            tool_list = list(context.tools.values())
            model_tools = [
                ToolDefinition(
                    name=tool.name,
                    description=tool.description,
                    parameters=deepcopy(tool.parameters),
                )
                for tool in tool_list
            ]
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
                    purpose="agent",
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
                        purpose="agent",
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
                # Telemetry only; compaction must not depend on unprobed usage
                # semantics from a compatible endpoint.
                session._api_total_tokens = response.usage.total_tokens
            emitter.emit(
                ModelResponse(
                    purpose="agent",
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

            tool_messages: list[Message] = []
            for tool_index, tool_call in enumerate(response.tool_calls, start=1):
                function_name = tool_call.function.name
                arguments = tool_call.function.arguments
                emitter.emit(
                    ToolStarted(
                        index=tool_index,
                        call=tool_call.model_copy(deep=True),
                    ),
                    step=step_number,
                )

                if function_name not in context.tools:
                    result = ToolResult(
                        success=False,
                        content="",
                        error=f"Unknown tool: {function_name}",
                    )
                else:
                    try:
                        raw_result = await context.tools[function_name].execute(
                            **arguments
                        )
                        if not isinstance(raw_result, ToolResult):
                            result = ToolResult(
                                success=False,
                                content="",
                                error=(
                                    f"Tool contract violation: {function_name} "
                                    f"returned {type(raw_result).__name__}, expected "
                                    "ToolResult"
                                ),
                            )
                        else:
                            result = raw_result
                    except Exception as error:
                        import traceback

                        error_detail = f"{type(error).__name__}: {error}"
                        error_trace = traceback.format_exc()
                        result = ToolResult(
                            success=False,
                            content="",
                            error=(
                                f"Tool execution failed: {error_detail}\n\n"
                                f"Traceback:\n{error_trace}"
                            ),
                        )

                emitter.emit(
                    ToolFinished(
                        index=tool_index,
                        call=tool_call.model_copy(deep=True),
                        result=result.model_copy(deep=True),
                    ),
                    step=step_number,
                )
                tool_messages.append(
                    Message(
                        role="tool",
                        content=(
                            result.content
                            if result.success
                            else f"Error: {result.error}"
                        ),
                        tool_call_id=tool_call.id,
                        name=function_name,
                    )
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
