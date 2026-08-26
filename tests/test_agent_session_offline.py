"""Offline contract tests for Session, Turn, and Step lifecycles."""

import asyncio

import pytest

from mini_agent.cli import wait_for_turn
from mini_agent.core import AgentSession, TurnAlreadyActiveError
from mini_agent.core.events import (
    ModelCallFailed,
    ModelRequest,
    ModelResponse,
    StepFinished,
    StepStarted,
    ToolFinished,
    TurnFinished,
    TurnStarted,
)
from mini_agent.schema import FunctionCall, LLMResponse, Message, TokenUsage, ToolCall
from mini_agent.tools.base import Tool, ToolResult
from tests.llm_test_double import ScriptedCall, ScriptedLLM


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


def tool_call(call_id: str) -> ToolCall:
    return ToolCall(
        id=call_id,
        type="function",
        function=FunctionCall(name="echo", arguments={"text": call_id}),
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


class InvalidResultTool(EchoTool):
    async def execute(self, text: str):
        self.calls.append(text)
        return None


class BlockingLLM:
    def __init__(self, llm_response: LLMResponse | Exception):
        self.response = llm_response
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.requests: list[list[Message]] = []

    async def generate(self, messages, tools=None):
        self.requests.append(list(messages))
        self.entered.set()
        await self.release.wait()
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def build_session(
    tmp_path,
    llm,
    tools,
    *,
    max_steps: int = 3,
) -> AgentSession:
    return AgentSession(
        llm_client=llm,
        system_prompt="You are a test agent.",
        tools=tools,
        max_steps=max_steps,
        workspace_dir=str(tmp_path),
        session_id="test-session",
    )


def workspace_fact(workspace) -> str:
    return (
        "## Current Workspace\n"
        f"You are currently working in: `{workspace.absolute()}`\n"
        "All relative paths will be resolved relative to this directory."
    )


@pytest.mark.parametrize("max_steps", [0, -1])
def test_session_rejects_nonpositive_max_steps_before_workspace_creation(
    tmp_path,
    max_steps,
):
    workspace = tmp_path / "must-not-exist"
    llm = ScriptedLLM([])

    with pytest.raises(ValueError, match="max_steps.*greater than zero"):
        AgentSession(
            llm_client=llm,
            system_prompt="You are a test agent.",
            tools=[],
            max_steps=max_steps,
            workspace_dir=str(workspace),
        )

    assert not workspace.exists()
    assert llm.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "base_prompt",
    [
        "Explain the phrase Current Workspace before answering.",
        (
            "Base prompt\n\n## Current Workspace\n"
            "You are currently working in: `/stale`\n"
            "All relative paths will be resolved relative to this directory."
        ),
    ],
)
async def test_session_appends_current_workspace_fact_to_model_request(
    tmp_path,
    base_prompt,
):
    workspace = tmp_path / "actual-workspace"
    llm = ScriptedLLM([ScriptedCall(response("done"))])
    session = AgentSession(
        llm_client=llm,
        system_prompt=base_prompt,
        tools=[],
        workspace_dir=str(workspace),
    )

    with llm:
        await session.start_turn("question").wait()

    system_content = llm.requests[0].messages[0].content
    assert isinstance(system_content, str)
    assert system_content.startswith(base_prompt)
    assert system_content.endswith(workspace_fact(workspace))


def test_session_does_not_duplicate_exact_workspace_fact(tmp_path):
    workspace = tmp_path / "actual-workspace"
    fact = workspace_fact(workspace)
    system_prompt = f"Base prompt\n\n{fact}"

    session = AgentSession(
        llm_client=ScriptedLLM([]),
        system_prompt=system_prompt,
        tools=[],
        workspace_dir=str(workspace),
    )

    assert session.get_history()[0].content == system_prompt


@pytest.mark.asyncio
async def test_session_keeps_history_across_distinct_turns(tmp_path):
    llm = ScriptedLLM(
        [
            ScriptedCall(response("first answer")),
            ScriptedCall(response("second answer")),
        ]
    )
    session = build_session(tmp_path, llm, [])
    first_events = []
    second_events = []

    with llm:
        first = session.start_turn("first question", event_sink=first_events.append)
        first_outcome = await first.wait()
        second = session.start_turn("second question", event_sink=second_events.append)
        second_outcome = await second.wait()

    assert first.session_id == second.session_id == session.session_id
    assert first.turn_id != second.turn_id
    assert first_outcome.stop_reason == second_outcome.stop_reason == "end_turn"
    assert [message.role for message in llm.requests[1].messages] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert llm.requests[1].messages[-1].content == "second question"
    assert [event.step for event in first_events if isinstance(event.event, StepStarted)] == [1]
    assert [event.step for event in second_events if isinstance(event.event, StepStarted)] == [1]
    assert {event.turn_id for event in first_events} == {first.turn_id}
    assert {event.turn_id for event in second_events} == {second.turn_id}


@pytest.mark.asyncio
async def test_session_rejects_a_second_active_turn_atomically(tmp_path):
    llm = BlockingLLM(response("released"))
    session = build_session(tmp_path, llm, [])
    events = []

    first = session.start_turn("first", event_sink=events.append)
    await llm.entered.wait()
    history_before_rejection = session.get_history()

    with pytest.raises(TurnAlreadyActiveError) as error:
        session.start_turn("must not be appended", event_sink=events.append)

    assert error.value.active_turn_id == first.turn_id
    assert session.get_history() == history_before_rejection
    assert sum(isinstance(event.event, TurnStarted) for event in events) == 1

    llm.release.set()
    assert (await first.wait()).stop_reason == "end_turn"

    next_turn = session.start_turn("now admitted")
    assert (await next_turn.wait()).stop_reason == "end_turn"
    assert next_turn.turn_id != first.turn_id


@pytest.mark.asyncio
async def test_turn_admission_reserves_before_reentrant_task_creation(
    monkeypatch,
    tmp_path,
):
    llm = ScriptedLLM([ScriptedCall(response("outer reply"))])
    session = build_session(tmp_path, llm, [])
    running_loop = asyncio.get_running_loop()
    create_task = running_loop.create_task
    reentry_checked = False

    def create_task_with_reentry(coroutine, *args, **kwargs):
        nonlocal reentry_checked
        if not reentry_checked:
            reentry_checked = True
            with pytest.raises(TurnAlreadyActiveError):
                session.start_turn("reentrant input")
        return create_task(coroutine, *args, **kwargs)

    monkeypatch.setattr(running_loop, "create_task", create_task_with_reentry)
    with llm:
        outcome = await session.start_turn("outer input").wait()

    assert outcome.stop_reason == "end_turn"
    assert reentry_checked
    assert [
        message.content for message in session.get_history() if message.role == "user"
    ] == ["outer input"]


@pytest.mark.asyncio
async def test_failed_task_creation_rolls_back_turn_admission(monkeypatch, tmp_path):
    llm = ScriptedLLM([ScriptedCall(response("admitted later"))])
    session = build_session(tmp_path, llm, [])
    running_loop = asyncio.get_running_loop()
    create_task = running_loop.create_task
    history_before = session.get_history()

    def reject_task_creation(coroutine, *args, **kwargs):
        raise RuntimeError("task factory rejected runner")

    monkeypatch.setattr(running_loop, "create_task", reject_task_creation)
    with pytest.raises(RuntimeError, match="task factory rejected"):
        session.start_turn("orphaned input")

    assert session.active_turn is None
    assert session.get_history() == history_before

    monkeypatch.setattr(running_loop, "create_task", create_task)
    with llm:
        handle = session.start_turn("accepted input")
        outcome = await handle.wait()
    assert handle.turn_id.endswith(":turn-1")
    assert outcome.stop_reason == "end_turn"


@pytest.mark.asyncio
async def test_caller_cancellation_does_not_cancel_the_active_turn(
    tmp_path,
):
    llm = BlockingLLM(response("finished after waiter cancellation"))
    session = build_session(tmp_path, llm, [])
    handle = session.start_turn("keep running")
    waiter = asyncio.create_task(handle.wait())
    await llm.entered.wait()

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert not handle.done

    llm.release.set()
    outcome = await handle.wait()
    assert outcome.stop_reason == "end_turn"
    assert session.active_turn is None
    assert handle.interrupt() is False


@pytest.mark.asyncio
async def test_cli_wait_settles_the_turn_before_propagating_cancellation(
    tmp_path,
):
    llm = BlockingLLM(response("finished at the safe boundary"))
    session = build_session(tmp_path, llm, [])
    handle = session.start_turn("keep session owned until settled")
    cli_waiter = asyncio.create_task(wait_for_turn(handle, poll_interval=0))
    await llm.entered.wait()

    cli_waiter.cancel()
    await asyncio.sleep(0)
    assert not cli_waiter.done()
    assert session.active_turn is handle

    llm.release.set()
    with pytest.raises(asyncio.CancelledError):
        await cli_waiter

    assert handle.done
    assert session.active_turn is None


@pytest.mark.asyncio
async def test_active_turn_uses_an_admission_time_tool_snapshot(tmp_path):
    call = tool_call("snapshotted-call")
    llm = BlockingLLM(response("", tool_calls=[call], finish_reason="tool_use"))
    tool = EchoTool()
    session = build_session(tmp_path, llm, [tool], max_steps=1)

    handle = session.start_turn("use the admitted tools")
    await llm.entered.wait()

    with pytest.raises(TypeError):
        session.tools["replacement"] = EchoTool()  # type: ignore[index]
    with pytest.raises(AttributeError):
        session.max_steps = 2  # type: ignore[misc]
    with pytest.raises(AttributeError):
        session.session_id = "replacement-session"  # type: ignore[misc]

    # Even an accidental private replacement cannot change this admitted Turn.
    session._tool_executor = None  # type: ignore[assignment]
    llm.release.set()
    outcome = await handle.wait()

    assert tool.calls == ["snapshotted-call"]
    assert outcome.stop_reason == "max_steps"


@pytest.mark.asyncio
async def test_tool_calls_continue_the_same_turn_across_steps(tmp_path):
    call = tool_call("call-1")
    llm = ScriptedLLM(
        [
            ScriptedCall(
                response("", tool_calls=[call], finish_reason="tool_use"),
            ),
            ScriptedCall(response("finished")),
        ]
    )
    tool = EchoTool()
    session = build_session(tmp_path, llm, [tool])
    events = []

    with llm:
        handle = session.start_turn("echo once", event_sink=events.append)
        outcome = await handle.wait()

    assert outcome.stop_reason == "end_turn"
    assert outcome.last_assistant_message == "finished"
    assert tool.calls == ["call-1"]
    assert sum(isinstance(event.event, TurnStarted) for event in events) == 1
    terminal_events = [event for event in events if isinstance(event.event, TurnFinished)]
    assert len(terminal_events) == 1
    assert terminal_events[0].event.outcome is outcome
    assert [event.step for event in events if isinstance(event.event, StepStarted)] == [1, 2]
    assert [event.step for event in events if isinstance(event.event, StepFinished)] == [1, 2]
    assert [message.role for message in llm.requests[1].messages[-2:]] == [
        "assistant",
        "tool",
    ]
    assert {event.session_id for event in events} == {session.session_id}
    assert {event.turn_id for event in events} == {handle.turn_id}
    assert all(
        event.step is not None
        for event in events
        if isinstance(event.event, (ModelRequest, ToolFinished))
    )


@pytest.mark.asyncio
async def test_event_observer_cannot_mutate_model_input_or_session_state(
    tmp_path,
):
    llm = ScriptedLLM([ScriptedCall(response("original answer"))])
    tool = EchoTool()
    session = build_session(tmp_path, llm, [tool])

    def mutating_observer(envelope):
        if isinstance(envelope.event, ModelRequest):
            envelope.event.messages[-1].content = "mutated input"
            envelope.event.tools[0].parameters["mutated"] = True
        if isinstance(envelope.event, ModelResponse):
            envelope.event.response.content = "mutated answer"

    with llm:
        outcome = await session.start_turn(
            "original input",
            event_sink=mutating_observer,
        ).wait()

    assert llm.requests[0].messages[-1].content == "original input"
    assert "mutated" not in llm.requests[0].tools[0].parameters
    assert "mutated" not in tool.parameters
    assert outcome.last_assistant_message == "original answer"
    assert session.get_history()[-1].content == "original answer"


@pytest.mark.asyncio
async def test_observer_failure_stops_at_a_valid_step_boundary(tmp_path):
    call = tool_call("observed-call")
    llm = ScriptedLLM(
        [
            ScriptedCall(
                response("", tool_calls=[call], finish_reason="tool_use"),
            ),
            ScriptedCall(response("next turn still works")),
        ]
    )
    tool = EchoTool()
    session = build_session(tmp_path, llm, [tool])
    observed = []

    def failing_observer(envelope):
        observed.append(envelope)
        if isinstance(envelope.event, ToolFinished):
            raise OSError("logger unavailable")

    with llm:
        first = await session.start_turn(
            "run one tool",
            event_sink=failing_observer,
        ).wait()
        second = await session.start_turn("continue safely").wait()

    assert first.stop_reason == "failed"
    assert first.error is not None
    assert first.error.kind == "observer_error"
    assert "logger unavailable" in first.error.message
    assert tool.calls == ["observed-call"]
    assert [message.role for message in session.get_history()[1:4]] == [
        "user",
        "assistant",
        "tool",
    ]
    assert second.stop_reason == "end_turn"
    assert session.active_turn is None
    assert not any(isinstance(item.event, TurnFinished) for item in observed)


@pytest.mark.asyncio
async def test_terminal_delivery_failure_cannot_rewrite_the_published_outcome(
    tmp_path,
):
    llm = ScriptedLLM([ScriptedCall(response("finished"))])
    session = build_session(tmp_path, llm, [])
    observed = []

    def record_then_fail(envelope):
        observed.append(envelope)
        if isinstance(envelope.event, TurnFinished):
            raise OSError("terminal consumer failed")

    with llm:
        outcome = await session.start_turn(
            "finish normally",
            event_sink=record_then_fail,
        ).wait()

    terminal = [
        envelope.event.outcome
        for envelope in observed
        if isinstance(envelope.event, TurnFinished)
    ]
    assert terminal == [outcome]
    assert terminal[0] is outcome
    assert outcome.stop_reason == "end_turn"


@pytest.mark.asyncio
async def test_model_failure_preserves_original_exception(tmp_path):
    model_error = OSError("endpoint unavailable")
    llm = ScriptedLLM([ScriptedCall(model_error)])
    session = build_session(tmp_path, llm, [])
    observed = []

    with llm:
        outcome = await session.start_turn(
            "call the model",
            event_sink=observed.append,
        ).wait()

    failures = [
        envelope.event
        for envelope in observed
        if isinstance(envelope.event, ModelCallFailed)
    ]
    expected_message = f"LLM call failed: {model_error}"
    assert failures == [ModelCallFailed(error=model_error, result=expected_message)]
    assert failures[0].error is model_error
    assert outcome.stop_reason == "failed"
    assert outcome.error is not None
    assert outcome.error.kind == "model_error"
    assert outcome.error.message == expected_message


@pytest.mark.asyncio
async def test_reporting_failure_does_not_hide_the_model_failure(
    tmp_path,
):
    llm = ScriptedLLM([ScriptedCall(RuntimeError("model unavailable"))])
    session = build_session(tmp_path, llm, [])

    def fail_while_reporting(envelope):
        if isinstance(envelope.event, ModelCallFailed):
            raise OSError("renderer failed")

    with llm:
        outcome = await session.start_turn(
            "call the model",
            event_sink=fail_while_reporting,
        ).wait()

    assert outcome.stop_reason == "failed"
    assert outcome.error is not None
    assert outcome.error.kind == "model_error"
    assert "model unavailable" in outcome.error.message
    assert outcome.observer_error is not None
    assert "renderer failed" in outcome.observer_error.message


@pytest.mark.asyncio
async def test_invalid_tool_return_becomes_a_paired_failed_observation(
    tmp_path,
):
    call = tool_call("invalid-result")
    llm = ScriptedLLM(
        [
            ScriptedCall(
                response("", tool_calls=[call], finish_reason="tool_use"),
            ),
            ScriptedCall(response("handled invalid result")),
        ]
    )
    tool = InvalidResultTool()
    session = build_session(tmp_path, llm, [tool])

    with llm:
        outcome = await session.start_turn("run invalid tool").wait()

    assert outcome.stop_reason == "end_turn"
    assert llm.requests[1].messages[-1].role == "tool"
    assert "Tool contract violation" in llm.requests[1].messages[-1].content


@pytest.mark.asyncio
async def test_internal_failure_returns_an_outcome_and_releases_the_session(
    monkeypatch,
    tmp_path,
):
    llm = ScriptedLLM([ScriptedCall(response("recovered"))])
    session = build_session(tmp_path, llm, [])
    run_step = session._loop._run_step
    fail_once = True

    async def failing_step(**kwargs):
        nonlocal fail_once
        if fail_once:
            fail_once = False
            raise RuntimeError("step invariant failed")
        return await run_step(**kwargs)

    monkeypatch.setattr(session._loop, "_run_step", failing_step)
    first_events = []

    with llm:
        first = await session.start_turn(
            "first input",
            event_sink=first_events.append,
        ).wait()
        second = await session.start_turn("second input").wait()

    assert first.stop_reason == "failed"
    assert first.error is not None
    assert first.error.kind == "internal_error"
    assert "step invariant failed" in first.error.message
    assert [
        event.event.outcome
        for event in first_events
        if isinstance(event.event, TurnFinished)
    ] == [first]
    assert second.stop_reason == "end_turn"
    assert session.active_turn is None


@pytest.mark.asyncio
async def test_turn_outcomes_report_control_flow_not_task_success(tmp_path):
    call = tool_call("only-step")
    cases = [
        (
            ScriptedLLM([ScriptedCall(response("I cannot finish this task."))]),
            [],
            3,
            "end_turn",
            None,
        ),
        (
            ScriptedLLM(
                [
                    ScriptedCall(
                        response("", tool_calls=[call], finish_reason="tool_use"),
                    )
                ]
            ),
            [EchoTool()],
            1,
            "max_steps",
            None,
        ),
        (
            ScriptedLLM([ScriptedCall(RuntimeError("model unavailable"))]),
            [],
            3,
            "failed",
            "model_error",
        ),
    ]

    for llm, tools, max_steps, reason, error_kind in cases:
        session = build_session(
            tmp_path,
            llm,
            tools,
            max_steps=max_steps,
        )
        events = []
        with llm:
            outcome = await session.start_turn(
                "run case",
                event_sink=events.append,
            ).wait()

        assert outcome.stop_reason == reason
        assert not hasattr(outcome, "success")
        assert not hasattr(outcome, "completed")
        assert (outcome.error.kind if outcome.error else None) == error_kind
        terminal = [event.event for event in events if isinstance(event.event, TurnFinished)]
        assert terminal == [TurnFinished(outcome=outcome)]
        step_statuses = [
            event.event.status
            for event in events
            if isinstance(event.event, StepFinished)
        ]
        assert step_statuses[-1] == reason


@pytest.mark.asyncio
async def test_interrupt_finishes_the_current_step_without_starting_another(
    tmp_path,
):
    call = tool_call("cancelled-call")
    llm = BlockingLLM(response("", tool_calls=[call], finish_reason="tool_use"))
    tool = EchoTool()
    session = build_session(tmp_path, llm, [tool])
    events = []

    handle = session.start_turn("interrupt me", event_sink=events.append)
    await llm.entered.wait()
    handle.interrupt()
    llm.release.set()
    outcome = await handle.wait()

    assert outcome.stop_reason == "interrupted"
    assert outcome.error is None
    assert tool.calls == ["cancelled-call"]
    assert [message.role for message in session.get_history()[-2:]] == [
        "assistant",
        "tool",
    ]
    assert [event.step for event in events if isinstance(event.event, StepStarted)] == [1]
    assert [event.event.status for event in events if isinstance(event.event, StepFinished)] == [
        "interrupted"
    ]


@pytest.mark.asyncio
async def test_terminal_response_and_model_failure_win_over_pending_interrupt(
    tmp_path,
):
    terminal_llm = BlockingLLM(response("terminal reply"))
    terminal_session = build_session(tmp_path, terminal_llm, [])
    terminal_handle = terminal_session.start_turn("finish while interrupted")
    await terminal_llm.entered.wait()
    assert terminal_handle.interrupt() is True
    terminal_llm.release.set()
    terminal_outcome = await terminal_handle.wait()

    assert terminal_outcome.stop_reason == "end_turn"
    assert terminal_outcome.last_assistant_message == "terminal reply"

    error_llm = BlockingLLM(RuntimeError("provider failed"))
    error_session = build_session(tmp_path, error_llm, [])
    error_handle = error_session.start_turn("fail while interrupted")
    await error_llm.entered.wait()
    assert error_handle.interrupt() is True
    error_llm.release.set()
    error_outcome = await error_handle.wait()

    assert error_outcome.stop_reason == "failed"
    assert error_outcome.error is not None
    assert error_outcome.error.kind == "model_error"
