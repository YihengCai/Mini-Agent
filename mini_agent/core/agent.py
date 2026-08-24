"""UI-independent agent loop implementation."""

import asyncio
from pathlib import Path
from time import perf_counter
from typing import Optional

import tiktoken

from ..llm import LLMClient
from ..schema import Message
from ..tools.base import Tool, ToolResult
from .events import (
    AgentEvent,
    AgentEventSink,
    CompactionFinished,
    CompactionRoundFinished,
    CompactionSkipped,
    CompactionStarted,
    HistoryCleaned,
    ModelCallFailed,
    ModelRequest,
    ModelResponse,
    RunFinished,
    RunStarted,
    StepFinished,
    StepStarted,
    ToolFinished,
    ToolStarted,
)


class Agent:
    """Single agent with basic tools and MCP support."""

    def __init__(
        self,
        llm_client: LLMClient,
        system_prompt: str,
        tools: list[Tool],
        max_steps: int = 50,
        workspace_dir: str = "./workspace",
        token_limit: int = 80000,  # Summary triggered when tokens exceed this value
    ):
        self.llm = llm_client
        self.tools = {tool.name: tool for tool in tools}
        self.max_steps = max_steps
        self.token_limit = token_limit
        self.workspace_dir = Path(workspace_dir)
        # Cancellation event for interrupting agent execution (set externally, e.g., by Esc key)
        self.cancel_event: Optional[asyncio.Event] = None

        # Ensure workspace exists
        self.workspace_dir.mkdir(parents=True, exist_ok=True)

        # Inject workspace information into system prompt if not already present
        if "Current Workspace" not in system_prompt:
            workspace_info = f"\n\n## Current Workspace\nYou are currently working in: `{self.workspace_dir.absolute()}`\nAll relative paths will be resolved relative to this directory."
            system_prompt = system_prompt + workspace_info

        self.system_prompt = system_prompt

        # Initialize message history
        self.messages: list[Message] = [Message(role="system", content=system_prompt)]

        # Token usage from last API response (updated after each LLM call)
        self.api_total_tokens: int = 0
        # Flag to skip token check right after summary (avoid consecutive triggers)
        self._skip_next_token_check: bool = False

    def add_user_message(self, content: str):
        """Add a user message to history."""
        self.messages.append(Message(role="user", content=content))

    def clear_history(self) -> int:
        """Clear conversation history while retaining the system prompt."""
        removed_count = len(self.messages) - 1
        self.messages = [self.messages[0]]
        return removed_count

    @staticmethod
    def _emit(event_sink: AgentEventSink | None, event: AgentEvent) -> None:
        if event_sink is not None:
            event_sink(event)

    def _check_cancelled(self) -> bool:
        """Check if agent execution has been cancelled.

        Returns:
            True if cancelled, False otherwise.
        """
        if self.cancel_event is not None and self.cancel_event.is_set():
            return True
        return False

    def _cleanup_incomplete_messages(
        self,
        event_sink: AgentEventSink | None = None,
    ) -> None:
        """Remove the incomplete assistant message and its partial tool results.

        This ensures message consistency after cancellation by removing
        only the current step's incomplete messages, preserving completed steps.
        """
        # Find the index of the last assistant message
        last_assistant_idx = -1
        for i in range(len(self.messages) - 1, -1, -1):
            if self.messages[i].role == "assistant":
                last_assistant_idx = i
                break

        if last_assistant_idx == -1:
            # No assistant message found, nothing to clean
            return

        # Remove the last assistant message and all tool results after it
        removed_count = len(self.messages) - last_assistant_idx
        if removed_count > 0:
            self.messages = self.messages[:last_assistant_idx]
            self._emit(event_sink, HistoryCleaned(removed_count=removed_count))

    def _estimate_tokens(self) -> int:
        """Accurately calculate token count for message history using tiktoken

        Uses cl100k_base encoder (GPT-4/Claude/M2 compatible)
        """
        try:
            # Use cl100k_base encoder (used by GPT-4 and most modern models)
            encoding = tiktoken.get_encoding("cl100k_base")
        except Exception:
            # Fallback: if tiktoken initialization fails, use simple estimation
            return self._estimate_tokens_fallback()

        total_tokens = 0

        for msg in self.messages:
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
        for msg in self.messages:
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
        event_sink: AgentEventSink | None = None,
    ) -> None:
        """Message history summarization: summarize conversations between user messages when tokens exceed limit

        Strategy (Agent mode):
        - Keep all user messages (these are user intents)
        - Summarize content between each user-user pair (agent execution process)
        - If last round is still executing (has agent/tool messages but no next user), also summarize
        - Structure: system -> user1 -> summary1 -> user2 -> summary2 -> user3 -> summary3 (if executing)

        Summary is triggered when EITHER:
        - Local token estimation exceeds limit
        - API reported total_tokens exceeds limit
        """
        # Skip check if we just completed a summary (wait for next LLM call to update api_total_tokens)
        if self._skip_next_token_check:
            self._skip_next_token_check = False
            return

        estimated_tokens = self._estimate_tokens()

        # Check both local estimation and API reported tokens
        should_summarize = estimated_tokens > self.token_limit or self.api_total_tokens > self.token_limit

        # If neither exceeded, no summary needed
        if not should_summarize:
            return

        self._emit(
            event_sink,
            CompactionStarted(
                estimated_tokens=estimated_tokens,
                reported_tokens=self.api_total_tokens,
                token_limit=self.token_limit,
            ),
        )

        # Find all user message indices (skip system prompt)
        user_indices = [i for i, msg in enumerate(self.messages) if msg.role == "user" and i > 0]

        # Need at least 1 user message to perform summary
        if len(user_indices) < 1:
            self._emit(event_sink, CompactionSkipped(reason="insufficient_messages"))
            return

        # Build new message list
        new_messages = [self.messages[0]]  # Keep system prompt
        summary_count = 0

        # Iterate through each user message and summarize the execution process after it
        for i, user_idx in enumerate(user_indices):
            # Add current user message
            new_messages.append(self.messages[user_idx])

            # Determine message range to summarize
            # If last user, go to end of message list; otherwise to before next user
            if i < len(user_indices) - 1:
                next_user_idx = user_indices[i + 1]
            else:
                next_user_idx = len(self.messages)

            # Extract execution messages for this round
            execution_messages = self.messages[user_idx + 1 : next_user_idx]

            # If there are execution messages in this round, summarize them
            if execution_messages:
                summary_text = await self._create_summary(
                    execution_messages,
                    i + 1,
                    event_sink,
                )
                if summary_text:
                    summary_message = Message(
                        role="user",
                        content=f"[Assistant Execution Summary]\n\n{summary_text}",
                    )
                    new_messages.append(summary_message)
                    summary_count += 1

        # Replace message list
        self.messages = new_messages

        # Skip next token check to avoid consecutive summary triggers
        # (api_total_tokens will be updated after next LLM call)
        self._skip_next_token_check = True

        new_tokens = self._estimate_tokens()
        self._emit(
            event_sink,
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
        event_sink: AgentEventSink | None = None,
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

        # Call LLM to generate concise summary
        try:
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
            self._emit(
                event_sink,
                ModelRequest(
                    purpose="summary",
                    messages=tuple(summary_messages),
                    tools=(),
                ),
            )
            response = await self.llm.generate(messages=summary_messages)
            self._emit(
                event_sink,
                ModelResponse(purpose="summary", response=response),
            )

            summary_text = response.content
            self._emit(
                event_sink,
                CompactionRoundFinished(
                    round_number=round_num,
                    used_fallback=False,
                ),
            )
            return summary_text

        except Exception as e:
            error_msg = f"Summary generation failed for round {round_num}: {e}"
            self._emit(
                event_sink,
                ModelCallFailed(
                    purpose="summary",
                    error=e,
                    result=error_msg,
                ),
            )
            self._emit(
                event_sink,
                CompactionRoundFinished(
                    round_number=round_num,
                    used_fallback=True,
                    error=e,
                ),
            )
            # Use simple text summary on failure
            return summary_content

    async def run(
        self,
        cancel_event: Optional[asyncio.Event] = None,
        event_sink: AgentEventSink | None = None,
    ) -> str:
        """Execute agent loop until task is complete or max steps reached.

        Args:
            cancel_event: Optional asyncio.Event that can be set to cancel execution.
                          When set, the agent will stop at the next safe checkpoint
                          (after completing the current step to keep messages consistent).
            event_sink: Optional synchronous observer for execution events. Event
                        payloads are borrowed for the duration of the callback.

        Returns:
            The final response content, or error message (including cancellation message).
        """
        # Set cancellation event (can also be set via self.cancel_event before calling run())
        if cancel_event is not None:
            self.cancel_event = cancel_event

        self._emit(event_sink, RunStarted(max_steps=self.max_steps))

        step = 0
        run_start_time = perf_counter()

        while step < self.max_steps:
            # Check for cancellation at start of each step
            if self._check_cancelled():
                self._cleanup_incomplete_messages(event_sink)
                cancel_msg = "Task cancelled by user."
                self._emit(
                    event_sink,
                    RunFinished(reason="cancelled", result=cancel_msg),
                )
                return cancel_msg

            step_start_time = perf_counter()
            # Check and summarize message history to prevent context overflow
            await self._summarize_messages(event_sink)

            step_number = step + 1
            self._emit(
                event_sink,
                StepStarted(step=step_number, max_steps=self.max_steps),
            )

            # Get tool list for LLM call
            tool_list = list(self.tools.values())

            self._emit(
                event_sink,
                ModelRequest(
                    purpose="agent",
                    messages=tuple(self.messages),
                    tools=tuple(tool_list),
                ),
            )

            try:
                response = await self.llm.generate(messages=self.messages, tools=tool_list)
            except Exception as e:
                # Check if it's a retry exhausted error
                from ..retry import RetryExhaustedError

                if isinstance(e, RetryExhaustedError):
                    error_msg = f"LLM call failed after {e.attempts} retries\nLast error: {str(e.last_exception)}"
                else:
                    error_msg = f"LLM call failed: {str(e)}"
                self._emit(
                    event_sink,
                    ModelCallFailed(
                        purpose="agent",
                        error=e,
                        result=error_msg,
                    ),
                )
                self._emit(
                    event_sink,
                    RunFinished(reason="model_error", result=error_msg),
                )
                return error_msg

            # Accumulate API reported token usage
            if response.usage:
                self.api_total_tokens = response.usage.total_tokens

            self._emit(
                event_sink,
                ModelResponse(purpose="agent", response=response),
            )

            # Add assistant message
            assistant_msg = Message(
                role="assistant",
                content=response.content,
                thinking=response.thinking,
                tool_calls=response.tool_calls,
            )
            self.messages.append(assistant_msg)

            # Check if task is complete (no tool calls)
            if not response.tool_calls:
                step_elapsed = perf_counter() - step_start_time
                total_elapsed = perf_counter() - run_start_time
                self._emit(
                    event_sink,
                    StepFinished(
                        step=step_number,
                        elapsed_seconds=step_elapsed,
                        total_elapsed_seconds=total_elapsed,
                    ),
                )
                self._emit(
                    event_sink,
                    RunFinished(reason="completed", result=response.content),
                )
                return response.content

            # Check for cancellation before executing tools
            if self._check_cancelled():
                self._cleanup_incomplete_messages(event_sink)
                cancel_msg = "Task cancelled by user."
                self._emit(
                    event_sink,
                    RunFinished(reason="cancelled", result=cancel_msg),
                )
                return cancel_msg

            # Execute tool calls
            for tool_index, tool_call in enumerate(response.tool_calls, start=1):
                tool_call_id = tool_call.id
                function_name = tool_call.function.name
                arguments = tool_call.function.arguments

                self._emit(
                    event_sink,
                    ToolStarted(
                        step=step_number,
                        index=tool_index,
                        call=tool_call,
                    ),
                )

                # Execute tool
                if function_name not in self.tools:
                    result = ToolResult(
                        success=False,
                        content="",
                        error=f"Unknown tool: {function_name}",
                    )
                else:
                    try:
                        tool = self.tools[function_name]
                        result = await tool.execute(**arguments)
                    except Exception as e:
                        # Catch all exceptions during tool execution, convert to failed ToolResult
                        import traceback

                        error_detail = f"{type(e).__name__}: {str(e)}"
                        error_trace = traceback.format_exc()
                        result = ToolResult(
                            success=False,
                            content="",
                            error=f"Tool execution failed: {error_detail}\n\nTraceback:\n{error_trace}",
                        )

                self._emit(
                    event_sink,
                    ToolFinished(
                        step=step_number,
                        index=tool_index,
                        call=tool_call,
                        result=result,
                    ),
                )

                # Add tool result message
                tool_msg = Message(
                    role="tool",
                    content=result.content if result.success else f"Error: {result.error}",
                    tool_call_id=tool_call_id,
                    name=function_name,
                )
                self.messages.append(tool_msg)

                # Check for cancellation after each tool execution
                if self._check_cancelled():
                    self._cleanup_incomplete_messages(event_sink)
                    cancel_msg = "Task cancelled by user."
                    self._emit(
                        event_sink,
                        RunFinished(reason="cancelled", result=cancel_msg),
                    )
                    return cancel_msg

            step_elapsed = perf_counter() - step_start_time
            total_elapsed = perf_counter() - run_start_time
            self._emit(
                event_sink,
                StepFinished(
                    step=step_number,
                    elapsed_seconds=step_elapsed,
                    total_elapsed_seconds=total_elapsed,
                ),
            )

            step += 1

        # Max steps reached
        error_msg = f"Task couldn't be completed after {self.max_steps} steps."
        self._emit(
            event_sink,
            RunFinished(reason="max_steps", result=error_msg),
        )
        return error_msg

    def get_history(self) -> list[Message]:
        """Get message history."""
        return self.messages.copy()
