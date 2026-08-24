"""Offline regression tests for the agent loop and its LLM test double."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mini_agent.agent import AgentSession
from mini_agent.cli import parse_args, print_help, report_observer_failure
from mini_agent.cli_events import CliEventSink
from mini_agent.core import TurnError, TurnOutcome
from mini_agent.core.events import (
    AgentEventEnvelope,
    CompactionFinished,
    CompactionRoundFinished,
    CompactionStarted,
    ModelRequest,
    ModelResponse,
    StepFinished,
    StepStarted,
    ToolFinished,
    ToolStarted,
    TurnFinished,
    TurnStarted,
)
from mini_agent.schema import FunctionCall, LLMResponse, Message, TokenUsage, ToolCall
from mini_agent.tools.base import Tool, ToolResult
from tests.llm_test_double import ScriptedCall, ScriptedLLM, validate_tool_call_pairs


def response(
    content: str,
    *,
    tool_calls: list[ToolCall] | None = None,
    finish_reason: str = "stop",
    usage: TokenUsage | None = None,
) -> LLMResponse:
    return LLMResponse(
        content=content,
        thinking=None,
        tool_calls=tool_calls,
        finish_reason=finish_reason,
        usage=usage,
    )


class EchoTool(Tool):
    def __init__(self):
        self.calls: list[str] = []

    @property
    def name(self) -> str:
        return "echo"

    @property
    def description(self) -> str:
        return "Return the supplied text."

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        }

    async def execute(self, text: str) -> ToolResult:
        self.calls.append(text)
        return ToolResult(success=True, content=f"echo:{text}")


class ExplodingTool(EchoTool):
    @property
    def name(self) -> str:
        return "explode"

    async def execute(self, text: str) -> ToolResult:
        raise ValueError(f"boom:{text}")


def build_agent(
    monkeypatch,
    tmp_path,
    llm,
    tools,
    *,
    max_steps: int = 3,
    token_limit: int = 80_000,
) -> AgentSession:
    agent = AgentSession(
        llm_client=llm,
        system_prompt="You are a test agent.",
        tools=tools,
        max_steps=max_steps,
        workspace_dir=str(tmp_path),
        token_limit=token_limit,
    )
    monkeypatch.setattr(agent, "_estimate_tokens", lambda: 0)
    return agent


async def run_turn(agent, user_input: str, event_sink=None):
    return await agent.start_turn(user_input, event_sink=event_sink).wait()


def tool_call(call_id: str, name: str = "echo") -> ToolCall:
    return ToolCall(
        id=call_id,
        type="function",
        function=FunctionCall(name=name, arguments={"text": call_id}),
    )


@pytest.mark.asyncio
async def test_scripted_llm_records_stable_requests_and_returns_in_order():
    messages = [Message(role="user", content=[{"type": "text", "text": "before"}])]
    tools = [
        {
            "name": "echo",
            "input_schema": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
            },
        }
    ]
    llm = ScriptedLLM(
        [
            ScriptedCall("agent", response("first")),
            ScriptedCall("agent", response("second")),
        ]
    )

    first = await llm.generate(messages, tools=tools)
    assert isinstance(messages[0].content, list)
    messages[0].content[0]["text"] = "changed"
    messages.append(Message(role="assistant", content="after"))
    tools[0]["name"] = "changed"
    tools[0]["input_schema"]["properties"]["text"]["type"] = "number"
    second = await llm.generate(messages, tools=tools)

    assert first.content == "first"
    assert second.content == "second"
    assert llm.requests[0].messages[0].content == [{"type": "text", "text": "before"}]
    assert llm.requests[0].tools == (
        {
            "name": "echo",
            "input_schema": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
            },
        },
    )
    assert len(llm.requests[1].messages) == 2
    llm.assert_complete()


@pytest.mark.asyncio
async def test_response_exhaustion_remains_visible_after_caller_catches_error():
    llm = ScriptedLLM([])
    violation = "Unexpected LLM call #1: scripted calls exhausted"
    verification_error = f"Scripted LLM verification failed:\n- {violation}"

    with pytest.raises(AssertionError) as call_error:
        await llm.generate([Message(role="user", content="unexpected")], tools=[])
    assert str(call_error.value) == violation

    with pytest.raises(AssertionError) as completion_error:
        llm.assert_complete()
    assert str(completion_error.value) == verification_error


def test_unconsumed_response_fails_context_exit():
    verification_error = (
        "Scripted LLM verification failed:\n"
        "- 1 scripted call(s) were not consumed"
    )

    with pytest.raises(AssertionError) as error:
        with ScriptedLLM([ScriptedCall("agent", response("unused"))]):
            pass
    assert str(error.value) == verification_error


@pytest.mark.asyncio
async def test_scripted_exception_is_consumed_without_becoming_a_violation():
    llm = ScriptedLLM([ScriptedCall("agent", RuntimeError("planned failure"))])

    with pytest.raises(RuntimeError) as error:
        await llm.generate([Message(role="user", content="fail")], tools=[])
    assert str(error.value) == "planned failure"

    llm.assert_complete()


@pytest.mark.asyncio
async def test_context_exit_checks_leftovers_when_scripted_exception_escapes():
    llm = ScriptedLLM(
        [
            ScriptedCall("agent", RuntimeError("planned failure")),
            ScriptedCall("agent", response("unused")),
        ]
    )
    verification_error = (
        "Scripted LLM verification failed:\n"
        "- 1 scripted call(s) were not consumed"
    )

    with pytest.raises(AssertionError) as error:
        with llm:
            await llm.generate([Message(role="user", content="fail")], tools=[])
    assert str(error.value) == verification_error


def test_tool_call_pair_check_allows_multiple_results_in_any_order():
    messages = [
        Message(role="assistant", content="", tool_calls=[tool_call("one"), tool_call("two")]),
        Message(role="tool", content="second", tool_call_id="two"),
        Message(role="tool", content="first", tool_call_id="one"),
    ]

    validate_tool_call_pairs(messages)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("messages", "expected_violation"),
    [
        (
            [Message(role="tool", content="orphan", tool_call_id="missing")],
            "Invalid LLM request #1: tool result at message 0 references unknown "
            "tool call 'missing'",
        ),
        (
            [
                Message(
                    role="assistant",
                    content="",
                    tool_calls=[tool_call("same"), tool_call("same")],
                )
            ],
            "Invalid LLM request #1: duplicate tool call ID 'same' at message 0; "
            "first declared at message 0",
        ),
        (
            [
                Message(role="assistant", content="", tool_calls=[tool_call("same")]),
                Message(role="tool", content="first", tool_call_id="same"),
                Message(role="tool", content="second", tool_call_id="same"),
            ],
            "Invalid LLM request #1: duplicate tool result for 'same' at message 2",
        ),
        (
            [
                Message(role="assistant", content="", tool_calls=[tool_call("reused")]),
                Message(role="tool", content="first", tool_call_id="reused"),
                Message(role="assistant", content="", tool_calls=[tool_call("reused")]),
            ],
            "Invalid LLM request #1: duplicate tool call ID 'reused' at message 2; "
            "first declared at message 0",
        ),
        (
            [Message(role="assistant", content="", tool_calls=[tool_call("pending")])],
            "Invalid LLM request #1: tool call(s) missing results: "
            "'pending' (message 0)",
        ),
    ],
)
async def test_scripted_llm_rejects_invalid_tool_call_pairs(
    messages,
    expected_violation,
):
    llm = ScriptedLLM([ScriptedCall("agent", response("unused"))])
    verification_error = (
        "Scripted LLM verification failed:\n"
        f"- {expected_violation}\n"
        "- 1 scripted call(s) were not consumed"
    )

    with pytest.raises(AssertionError) as request_error:
        await llm.generate(messages, tools=[])
    assert str(request_error.value) == expected_violation

    with pytest.raises(AssertionError) as completion_error:
        llm.assert_complete()
    assert str(completion_error.value) == verification_error


@pytest.mark.asyncio
async def test_real_agent_loop_executes_tool_and_sends_result_to_model(monkeypatch, tmp_path):
    call = ToolCall(
        id="call-1",
        type="function",
        function=FunctionCall(name="echo", arguments={"text": "ping"}),
    )
    llm = ScriptedLLM(
        [
            ScriptedCall(
                "agent",
                response("", tool_calls=[call], finish_reason="tool_use"),
            ),
            ScriptedCall("agent", response("finished")),
        ]
    )
    tool = EchoTool()
    agent = build_agent(monkeypatch, tmp_path, llm, [tool])
    events = []

    with llm:
        outcome = await run_turn(
            agent,
            "Echo ping, then finish.",
            event_sink=events.append,
        )

    assert outcome.last_assistant_message == "finished"
    assert tool.calls == ["ping"]
    assert [message.role for message in llm.requests[0].messages] == ["system", "user"]
    assert [message.role for message in llm.requests[1].messages] == [
        "system",
        "user",
        "assistant",
        "tool",
    ]
    assistant_message = llm.requests[1].messages[-2]
    tool_message = llm.requests[1].messages[-1]
    assert assistant_message.tool_calls == [call]
    assert tool_message.tool_call_id == "call-1"
    assert tool_message.name == "echo"
    assert tool_message.content == "echo:ping"
    assert llm.requests[0].tools == (
        {
            "name": "echo",
            "description": "Return the supplied text.",
            "input_schema": tool.parameters,
        },
    )
    assert [type(envelope.event) for envelope in events] == [
        TurnStarted,
        StepStarted,
        ModelRequest,
        ModelResponse,
        ToolStarted,
        ToolFinished,
        StepFinished,
        StepStarted,
        ModelRequest,
        ModelResponse,
        StepFinished,
        TurnFinished,
    ]
    assert [
        (envelope.step, envelope.event.index, envelope.event.call.id)
        for envelope in events
        if isinstance(envelope.event, ToolStarted)
    ] == [(1, 1, "call-1")]
    assert [
        envelope.event.purpose
        for envelope in events
        if isinstance(envelope.event, ModelRequest)
    ] == [
        "agent",
        "agent",
    ]
    assert isinstance(events[-1].event, TurnFinished)
    assert events[-1].event.outcome is outcome
    assert outcome.stop_reason == "end_turn"


@pytest.mark.asyncio
async def test_core_agent_run_is_silent_without_an_event_sink(monkeypatch, tmp_path):
    llm = ScriptedLLM([ScriptedCall("agent", response("quietly finished"))])
    agent = build_agent(monkeypatch, tmp_path, llm, [])

    def reject_print(*_args, **_kwargs):
        raise AssertionError("core agent loop attempted terminal output")

    monkeypatch.setattr("builtins.print", reject_print)
    with llm:
        outcome = await run_turn(agent, "Finish without a UI.")

    assert outcome.last_assistant_message == "quietly finished"
    assert not hasattr(agent, "logger")


@pytest.mark.asyncio
async def test_cli_event_sink_preserves_rendering_and_run_logging(
    monkeypatch,
    tmp_path,
    capsys,
):
    call = ToolCall(
        id="cli-call",
        type="function",
        function=FunctionCall(name="echo", arguments={"text": "visible"}),
    )
    llm = ScriptedLLM(
        [
            ScriptedCall(
                "agent",
                response("", tool_calls=[call], finish_reason="tool_use"),
            ),
            ScriptedCall("agent", response("visible finish")),
        ]
    )
    agent = build_agent(monkeypatch, tmp_path, llm, [EchoTool()])
    logger = MagicMock()
    logger.get_log_file_path.return_value = Path("/tmp/agent-run.log")

    with llm:
        outcome = await run_turn(
            agent,
            "Exercise the CLI adapter.",
            event_sink=CliEventSink(logger=logger),
        )

    output = capsys.readouterr().out
    assert outcome.last_assistant_message == "visible finish"
    assert "Log file: /tmp/agent-run.log" in output
    assert output.count("Turn started") == 1
    assert "Step budget: 3 agent model requests" in output
    assert "Step 1/3" in output
    assert "Tool Call:" in output
    assert "echo:visible" in output
    assert "Assistant:" in output
    assert "visible finish" in output
    assert "Step 1 finished; continuing the same Turn" in output
    assert "Step 2 finished; model made no tool calls" in output
    assert output.count("Turn ended; control returned to the client") == 1
    assert "✓ Turn" not in output
    assert "Step 2 completed" not in output
    logger.start_new_run.assert_called_once_with()
    assert logger.log_request.call_count == 2
    assert logger.log_response.call_count == 2
    logger.log_tool_result.assert_called_once()


def test_cli_event_sink_renders_step_and_turn_stop_facts(capsys):
    logger = MagicMock()
    logger.get_log_file_path.return_value = Path("/tmp/agent-run.log")
    sink = CliEventSink(logger=logger)

    def emit(event, *, step=None):
        sink(
            AgentEventEnvelope(
                session_id="session",
                turn_id="session:turn-1",
                step=step,
                event=event,
            )
        )

    emit(TurnStarted(max_steps=3))
    emit(StepFinished("interrupted", 0.1, 0.1), step=1)
    emit(StepFinished("max_steps", 0.2, 0.3), step=2)
    emit(StepFinished("failed", 0.3, 0.6), step=3)
    emit(
        TurnFinished(
            TurnOutcome(
                session_id="session",
                turn_id="session:turn-1",
                stop_reason="max_steps",
            )
        )
    )
    emit(
        TurnFinished(
            TurnOutcome(
                session_id="session",
                turn_id="session:turn-1",
                stop_reason="failed",
                error=TurnError("internal_error", "broken invariant"),
            )
        )
    )

    output = capsys.readouterr().out
    assert "Step 1 finished at the interruption boundary" in output
    assert "Step 2 finished; this Turn's Step budget is exhausted" in output
    assert "Step 3 failed" in output
    assert "Turn stopped after 3 Steps; agent model-request budget exhausted" in output
    assert "Turn ended (internal_error):" in output
    assert "broken invariant" in output


@pytest.mark.asyncio
async def test_cli_event_sink_does_not_repeat_model_failure_details(
    monkeypatch,
    tmp_path,
    capsys,
):
    llm = ScriptedLLM([ScriptedCall("agent", RuntimeError("model unavailable"))])
    agent = build_agent(monkeypatch, tmp_path, llm, [])
    logger = MagicMock()
    logger.get_log_file_path.return_value = Path("/tmp/agent-run.log")

    with llm:
        outcome = await run_turn(
            agent,
            "Fail the model call.",
            event_sink=CliEventSink(logger=logger),
        )

    output = capsys.readouterr().out
    assert outcome.stop_reason == "failed"
    assert output.count("model unavailable") == 1
    assert "Step 1 failed" in output
    assert "Turn ended (model_error)." in output


def test_cli_help_uses_session_turn_and_interruption_semantics(
    monkeypatch,
    capsys,
):
    print_help()
    interactive_help = capsys.readouterr().out
    assert "Start a new Session with the same configuration" in interactive_help
    assert "Request interruption of the current Turn" in interactive_help
    assert "Cancel current agent execution" not in interactive_help

    monkeypatch.setattr("sys.argv", ["mini-agent", "--help"])
    with pytest.raises(SystemExit) as exit_info:
        parse_args()
    assert exit_info.value.code == 0
    argument_help = capsys.readouterr().out
    assert "Submit one Turn non-interactively" in argument_help


def test_cli_reports_an_event_observer_failure_without_the_broken_sink(capsys):
    outcome = TurnOutcome(
        session_id="session",
        turn_id="session:turn-1",
        stop_reason="failed",
        error=TurnError("model_error", "model unavailable"),
        observer_error=TurnError("observer_error", "logger unavailable"),
    )

    report_observer_failure(outcome)

    error_output = capsys.readouterr().err
    assert "Turn event observer failed" in error_output
    assert "logger unavailable" in error_output


@pytest.mark.asyncio
async def test_call_purpose_mismatch_remains_visible_after_caller_catches_error():
    llm = ScriptedLLM([ScriptedCall("summary", response("summary"))])
    violation = "Unexpected LLM call #1: expected 'summary', got 'agent'"
    verification_error = (
        "Scripted LLM verification failed:\n"
        f"- {violation}\n"
        "- 1 scripted call(s) were not consumed"
    )

    with pytest.raises(AssertionError) as call_error:
        await llm.generate([Message(role="user", content="main")], tools=[])
    assert str(call_error.value) == violation

    with pytest.raises(AssertionError) as completion_error:
        llm.assert_complete()
    assert str(completion_error.value) == verification_error


@pytest.mark.asyncio
async def test_first_violation_prevents_later_script_consumption():
    llm = ScriptedLLM([ScriptedCall("summary", response("summary"))])
    violation = "Unexpected LLM call #1: expected 'summary', got 'agent'"
    verification_error = (
        "Scripted LLM verification failed:\n"
        f"- {violation}\n"
        "- 1 scripted call(s) were not consumed"
    )

    with pytest.raises(AssertionError) as first_error:
        await llm.generate([Message(role="user", content="wrong")], tools=[])
    assert str(first_error.value) == violation

    with pytest.raises(AssertionError) as repeated_error:
        await llm.generate([Message(role="user", content="now correct")])
    assert str(repeated_error.value) == violation

    with pytest.raises(AssertionError) as completion_error:
        llm.assert_complete()
    assert str(completion_error.value) == verification_error


@pytest.mark.asyncio
async def test_summary_and_agent_calls_follow_one_global_sequence(monkeypatch, tmp_path):
    first_call = tool_call("first")
    llm = ScriptedLLM(
        [
            ScriptedCall(
                "agent",
                response("", tool_calls=[first_call], finish_reason="tool_use"),
            ),
            ScriptedCall(
                "agent",
                response("first finished", usage=TokenUsage(total_tokens=10)),
            ),
            ScriptedCall("summary", response("compressed first turn")),
            ScriptedCall("agent", response("second finished")),
        ]
    )
    agent = build_agent(
        monkeypatch,
        tmp_path,
        llm,
        [EchoTool()],
        token_limit=1,
    )
    second_run_events = []

    with llm:
        first_outcome = await run_turn(agent, "Complete the first turn.")
        second_outcome = await run_turn(
            agent,
            "Complete the second turn.",
            event_sink=second_run_events.append,
        )

    assert first_outcome.last_assistant_message == "first finished"
    assert second_outcome.last_assistant_message == "second finished"
    assert [request.purpose for request in llm.requests] == [
        "agent",
        "agent",
        "summary",
        "agent",
    ]
    assert llm.requests[2].tools is None
    assert [message.role for message in llm.requests[2].messages] == ["system", "user"]
    final_messages = llm.requests[3].messages
    assert any(
        "compressed first turn" in str(message.content)
        for message in final_messages
    )
    assert [type(envelope.event) for envelope in second_run_events] == [
        TurnStarted,
        CompactionStarted,
        ModelRequest,
        ModelResponse,
        CompactionRoundFinished,
        CompactionFinished,
        StepStarted,
        ModelRequest,
        ModelResponse,
        StepFinished,
        TurnFinished,
    ]
    assert [
        envelope.event.purpose
        for envelope in second_run_events
        if isinstance(envelope.event, ModelRequest)
    ] == ["summary", "agent"]
    assert [
        envelope.step
        for envelope in second_run_events
        if isinstance(envelope.event, ModelRequest)
    ] == [None, 1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "tool_name", "expected_error"),
    [
        ("unknown", "missing", "Error: Unknown tool: missing"),
        ("exception", "explode", "Error: Tool execution failed: ValueError: boom:failure"),
    ],
)
async def test_tool_failures_are_returned_to_the_next_model_call(
    monkeypatch,
    tmp_path,
    mode,
    tool_name,
    expected_error,
):
    call = tool_call("failure", name=tool_name)
    llm = ScriptedLLM(
        [
            ScriptedCall(
                "agent",
                response("", tool_calls=[call], finish_reason="tool_use"),
            ),
            ScriptedCall("agent", response("recovered")),
        ]
    )
    tools = [] if mode == "unknown" else [ExplodingTool()]
    agent = build_agent(monkeypatch, tmp_path, llm, tools)

    with llm:
        outcome = await run_turn(agent, "Exercise a failing tool.")

    assert outcome.last_assistant_message == "recovered"
    tool_result = llm.requests[1].messages[-1]
    assert tool_result.role == "tool"
    assert tool_result.tool_call_id == "failure"
    assert expected_error in tool_result.content


@pytest.mark.asyncio
async def test_max_steps_is_distinct_from_normal_completion(monkeypatch, tmp_path):
    call = tool_call("only-step")
    llm = ScriptedLLM(
        [
            ScriptedCall(
                "agent",
                response("", tool_calls=[call], finish_reason="tool_use"),
            )
        ]
    )
    tool = EchoTool()
    agent = build_agent(monkeypatch, tmp_path, llm, [tool], max_steps=1)
    events = []

    with llm:
        outcome = await run_turn(
            agent,
            "Keep going past the allowed step.",
            event_sink=events.append,
        )

    assert outcome.stop_reason == "max_steps"
    assert tool.calls == ["only-step"]
    assert [message.role for message in agent.get_history()[-2:]] == [
        "assistant",
        "tool",
    ]
    terminal_events = [
        envelope.event
        for envelope in events
        if isinstance(envelope.event, TurnFinished)
    ]
    assert len(terminal_events) == 1
    assert terminal_events[0].outcome is outcome
    assert events[-1].event is terminal_events[0]


@pytest.mark.asyncio
async def test_agent_cannot_hide_script_exhaustion(monkeypatch, tmp_path):
    call = tool_call("needs-another-call")
    llm = ScriptedLLM(
        [
            ScriptedCall(
                "agent",
                response("", tool_calls=[call], finish_reason="tool_use"),
            )
        ]
    )
    agent = build_agent(monkeypatch, tmp_path, llm, [EchoTool()])
    outcome = None
    verification_error = (
        "Scripted LLM verification failed:\n"
        "- Unexpected LLM call #2: scripted calls exhausted"
    )

    with pytest.raises(AssertionError) as error:
        with llm:
            outcome = await run_turn(agent, "Require another model call.")
    assert str(error.value) == verification_error

    assert outcome is not None
    assert outcome.stop_reason == "failed"
    assert outcome.error is not None
    assert outcome.error.message == (
        "LLM call failed: Unexpected LLM call #2: scripted calls exhausted"
    )


@pytest.mark.asyncio
async def test_summary_fallback_cannot_hide_call_order_violation(monkeypatch, tmp_path):
    llm = ScriptedLLM(
        [
            ScriptedCall(
                "agent",
                response("first finished", usage=TokenUsage(total_tokens=10)),
            ),
            ScriptedCall("agent", response("should not run")),
        ]
    )
    agent = build_agent(monkeypatch, tmp_path, llm, [], token_limit=1)
    outcome = None
    verification_error = (
        "Scripted LLM verification failed:\n"
        "- Unexpected LLM call #2: expected 'agent', got 'summary'\n"
        "- 1 scripted call(s) were not consumed"
    )

    with pytest.raises(AssertionError) as error:
        with llm:
            first = await run_turn(agent, "Complete the first turn.")
            assert first.last_assistant_message == "first finished"
            outcome = await run_turn(agent, "Trigger summary before the next turn.")
    assert str(error.value) == verification_error

    assert outcome is not None
    assert outcome.stop_reason == "failed"
    assert outcome.error is not None
    assert outcome.error.message == (
        "LLM call failed: Unexpected LLM call #2: expected 'agent', got 'summary'"
    )
