"""Terminal rendering and run logging for core agent events."""

import json

from .core.events import (
    AgentEvent,
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
from .logger import AgentLogger
from .retry import RetryExhaustedError
from .utils import Colors, calculate_display_width


class CliEventSink:
    """Render core events and preserve the existing human-readable run log."""

    def __init__(self, logger: AgentLogger | None = None):
        self.logger = logger or AgentLogger()

    def __call__(self, event: AgentEvent) -> None:
        if isinstance(event, RunStarted):
            self.logger.start_new_run()
            print(
                f"{Colors.DIM}📝 Log file: "
                f"{self.logger.get_log_file_path()}{Colors.RESET}"
            )
            return

        if isinstance(event, StepStarted):
            self._render_step_started(event)
            return

        if isinstance(event, ModelRequest):
            if event.purpose == "agent":
                self.logger.log_request(
                    messages=list(event.messages),
                    tools=list(event.tools),
                )
            return

        if isinstance(event, ModelResponse):
            if event.purpose == "agent":
                self._render_model_response(event)
            return

        if isinstance(event, ModelCallFailed):
            if event.purpose == "agent":
                if isinstance(event.error, RetryExhaustedError):
                    print(
                        f"\n{Colors.BRIGHT_RED}❌ Retry failed:{Colors.RESET} "
                        f"{event.result}"
                    )
                else:
                    print(
                        f"\n{Colors.BRIGHT_RED}❌ Error:{Colors.RESET} "
                        f"{event.result}"
                    )
            return

        if isinstance(event, ToolStarted):
            self._render_tool_started(event)
            return

        if isinstance(event, ToolFinished):
            self._render_tool_finished(event)
            return

        if isinstance(event, StepFinished):
            print(
                f"\n{Colors.DIM}⏱️  Step {event.step} completed in "
                f"{event.elapsed_seconds:.2f}s "
                f"(total: {event.total_elapsed_seconds:.2f}s){Colors.RESET}"
            )
            return

        if isinstance(event, HistoryCleaned):
            print(
                f"{Colors.DIM}   Cleaned up {event.removed_count} "
                f"incomplete message(s){Colors.RESET}"
            )
            return

        if isinstance(event, CompactionStarted):
            print(
                f"\n{Colors.BRIGHT_YELLOW}📊 Token usage - Local estimate: "
                f"{event.estimated_tokens}, API reported: {event.reported_tokens}, "
                f"Limit: {event.token_limit}{Colors.RESET}"
            )
            print(
                f"{Colors.BRIGHT_YELLOW}🔄 Triggering message history "
                f"summarization...{Colors.RESET}"
            )
            return

        if isinstance(event, CompactionSkipped):
            print(
                f"{Colors.BRIGHT_YELLOW}⚠️  Insufficient messages, "
                f"cannot summarize{Colors.RESET}"
            )
            return

        if isinstance(event, CompactionRoundFinished):
            if event.used_fallback:
                print(
                    f"{Colors.BRIGHT_RED}✗ Summary generation failed for round "
                    f"{event.round_number}: {event.error}{Colors.RESET}"
                )
            else:
                print(
                    f"{Colors.BRIGHT_GREEN}✓ Summary for round "
                    f"{event.round_number} generated successfully{Colors.RESET}"
                )
            return

        if isinstance(event, CompactionFinished):
            print(
                f"{Colors.BRIGHT_GREEN}✓ Summary completed, local tokens: "
                f"{event.previous_tokens} → {event.current_tokens}{Colors.RESET}"
            )
            print(
                f"{Colors.DIM}  Structure: system + {event.user_message_count} "
                f"user messages + {event.summary_count} summaries{Colors.RESET}"
            )
            print(
                f"{Colors.DIM}  Note: API token count will update on next "
                f"LLM call{Colors.RESET}"
            )
            return

        if isinstance(event, RunFinished):
            if event.reason in {"cancelled", "max_steps"}:
                print(
                    f"\n{Colors.BRIGHT_YELLOW}⚠️  "
                    f"{event.result}{Colors.RESET}"
                )
            return

        raise TypeError(f"Unsupported agent event: {type(event).__name__}")

    def _render_step_started(self, event: StepStarted) -> None:
        box_width = 58
        step_text = (
            f"{Colors.BOLD}{Colors.BRIGHT_CYAN}"
            f"💭 Step {event.step}/{event.max_steps}{Colors.RESET}"
        )
        step_display_width = calculate_display_width(step_text)
        padding = max(0, box_width - 1 - step_display_width)

        print(f"\n{Colors.DIM}╭{'─' * box_width}╮{Colors.RESET}")
        print(
            f"{Colors.DIM}│{Colors.RESET} {step_text}{' ' * padding}"
            f"{Colors.DIM}│{Colors.RESET}"
        )
        print(f"{Colors.DIM}╰{'─' * box_width}╯{Colors.RESET}")

    def _render_model_response(self, event: ModelResponse) -> None:
        response = event.response
        self.logger.log_response(
            content=response.content,
            thinking=response.thinking,
            tool_calls=response.tool_calls,
            finish_reason=response.finish_reason,
        )

        if response.thinking:
            print(f"\n{Colors.BOLD}{Colors.MAGENTA}🧠 Thinking:{Colors.RESET}")
            print(f"{Colors.DIM}{response.thinking}{Colors.RESET}")

        if response.content:
            print(
                f"\n{Colors.BOLD}{Colors.BRIGHT_BLUE}"
                f"🤖 Assistant:{Colors.RESET}"
            )
            print(response.content)

    @staticmethod
    def _render_tool_started(event: ToolStarted) -> None:
        function_name = event.call.function.name
        arguments = event.call.function.arguments
        print(
            f"\n{Colors.BRIGHT_YELLOW}🔧 Tool Call:{Colors.RESET} "
            f"{Colors.BOLD}{Colors.CYAN}{function_name}{Colors.RESET}"
        )
        print(f"{Colors.DIM}   Arguments:{Colors.RESET}")

        truncated_args = {}
        for key, value in arguments.items():
            value_str = str(value)
            truncated_args[key] = (
                value_str[:200] + "..." if len(value_str) > 200 else value
            )
        args_json = json.dumps(truncated_args, indent=2, ensure_ascii=False)
        for line in args_json.split("\n"):
            print(f"   {Colors.DIM}{line}{Colors.RESET}")

    def _render_tool_finished(self, event: ToolFinished) -> None:
        function_name = event.call.function.name
        arguments = event.call.function.arguments
        result = event.result
        self.logger.log_tool_result(
            tool_name=function_name,
            arguments=arguments,
            result_success=result.success,
            result_content=result.content if result.success else None,
            result_error=result.error if not result.success else None,
        )

        if result.success:
            result_text = result.content
            if len(result_text) > 300:
                result_text = result_text[:300] + f"{Colors.DIM}...{Colors.RESET}"
            print(f"{Colors.BRIGHT_GREEN}✓ Result:{Colors.RESET} {result_text}")
        else:
            print(
                f"{Colors.BRIGHT_RED}✗ Error:{Colors.RESET} "
                f"{Colors.RED}{result.error}{Colors.RESET}"
            )


__all__ = ["CliEventSink"]
