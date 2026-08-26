"""Terminal rendering and run logging for core agent events."""

import json

from .core.events import (
    AgentEventEnvelope,
    ModelCallFailed,
    ModelRequest,
    ModelResponse,
    StepFinished,
    StepStarted,
    ToolFinished,
    ToolStarted,
    TurnFinished,
    TurnStarted,
)
from .logger import AgentLogger
from .utils import Colors, calculate_display_width


class CliEventSink:
    """Render core events and preserve the existing human-readable run log."""

    def __init__(self, logger: AgentLogger | None = None):
        self.logger = logger or AgentLogger()
        self._max_steps = 0

    def __call__(self, envelope: AgentEventEnvelope) -> None:
        event = envelope.event
        if isinstance(event, TurnStarted):
            self._max_steps = event.max_steps
            self.logger.start_new_run()
            print(
                f"\n{Colors.BOLD}{Colors.BRIGHT_CYAN}▶ Turn started{Colors.RESET}"
                f"{Colors.DIM} — Step budget: {event.max_steps} agent model "
                f"requests; each Step includes its tool batch.{Colors.RESET}"
            )
            print(
                f"{Colors.DIM}📝 Log file: "
                f"{self.logger.get_log_file_path()}{Colors.RESET}"
            )
            return

        if isinstance(event, StepStarted):
            assert envelope.step is not None
            self._render_step_started(envelope.step, event)
            return

        if isinstance(event, ModelRequest):
            self.logger.log_request(
                messages=list(event.messages),
                tools=list(event.tools),
            )
            return

        if isinstance(event, ModelResponse):
            self._render_model_response(event)
            return

        if isinstance(event, ModelCallFailed):
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
            assert envelope.step is not None
            status_text = {
                "continued": "finished; continuing the same Turn",
                "end_turn": "finished; model made no tool calls",
                "interrupted": "finished at the interruption boundary",
                "max_steps": "finished; this Turn's Step budget is exhausted",
                "failed": "failed",
            }[event.status]
            print(
                f"\n{Colors.DIM}⏱️  Step {envelope.step} {status_text} in "
                f"{event.elapsed_seconds:.2f}s "
                f"(total: {event.total_elapsed_seconds:.2f}s){Colors.RESET}"
            )
            return

        if isinstance(event, TurnFinished):
            if event.outcome.stop_reason == "end_turn":
                print(
                    f"\n{Colors.BRIGHT_CYAN}↩ "
                    f"Turn ended; control returned to the client."
                    f"{Colors.RESET}"
                )
            elif event.outcome.stop_reason == "interrupted":
                print(
                    f"\n{Colors.BRIGHT_YELLOW}⚠️  "
                    f"Turn interrupted at a Step boundary.{Colors.RESET}"
                )
            elif event.outcome.stop_reason == "max_steps":
                print(
                    f"\n{Colors.BRIGHT_YELLOW}⚠️  "
                    f"Turn stopped after {self._max_steps} Steps; "
                    f"agent model-request budget exhausted."
                    f"{Colors.RESET}"
                )
            elif event.outcome.stop_reason == "failed":
                assert event.outcome.error is not None
                if event.outcome.error.kind in {
                    "internal_error",
                    "tool_protocol_error",
                }:
                    print(
                        f"\n{Colors.BRIGHT_RED}❌ Turn ended "
                        f"({event.outcome.error.kind}):{Colors.RESET} "
                        f"{event.outcome.error.message}"
                    )
                else:
                    print(
                        f"\n{Colors.BRIGHT_RED}❌ Turn ended "
                        f"({event.outcome.error.kind}).{Colors.RESET}"
                    )
            return

        raise TypeError(f"Unsupported agent event: {type(event).__name__}")

    def _render_step_started(self, step_number: int, event: StepStarted) -> None:
        box_width = 58
        step_text = (
            f"{Colors.BOLD}{Colors.BRIGHT_CYAN}"
            f"💭 Step {step_number}/{event.max_steps}{Colors.RESET}"
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
