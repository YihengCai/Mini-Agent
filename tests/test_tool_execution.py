"""Offline contract tests for tool registration and batch execution."""

import asyncio
from copy import deepcopy
from unittest.mock import MagicMock

import pytest

from mini_agent.cli_events import CliEventSink
from mini_agent.core import AgentSession
from mini_agent.core.events import StepFinished, ToolFinished, ToolStarted
from mini_agent.llm.protocol import ToolDefinition
from mini_agent.schema import FunctionCall, LLMResponse, Message, ToolCall
from mini_agent.tools.base import Tool, ToolResult
from tests.llm_test_double import ScriptedCall, ScriptedLLM


def response(*tool_calls: ToolCall) -> LLMResponse:
    return LLMResponse(
        content="",
        tool_calls=list(tool_calls),
        finish_reason="tool_use",
        usage=None,
    )


def tool_call(
    call_id: str,
    text: str,
    *,
    name: str = "echo",
    call_type: str = "function",
) -> ToolCall:
    return ToolCall(
        id=call_id,
        type=call_type,
        function=FunctionCall(name=name, arguments={"text": text}),
    )


class MutableEchoTool(Tool):
    def __init__(self, name: str = "echo") -> None:
        self.current_name = name
        self.current_description = "Return the supplied text."
        self.current_parameters = {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        }
        self.calls: list[str] = []

    @property
    def name(self) -> str:
        return self.current_name

    @property
    def description(self) -> str:
        return self.current_description

    @property
    def parameters(self) -> dict:
        return self.current_parameters

    async def execute(self, text: str) -> ToolResult:
        self.calls.append(text)
        return ToolResult(success=True, content=f"echo:{text}")


class ExplodingTool(MutableEchoTool):
    async def execute(self, text: str) -> ToolResult:
        self.calls.append(text)
        raise ValueError(f"boom:{text}")


class InvalidResultTool(MutableEchoTool):
    async def execute(self, text: str):
        self.calls.append(text)
        return None


class BlockingEchoTool(MutableEchoTool):
    def __init__(self, name: str = "echo", *, block_text: str) -> None:
        super().__init__(name)
        self.block_text = block_text
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(self, text: str) -> ToolResult:
        self.calls.append(text)
        if text == self.block_text:
            self.entered.set()
            await self.release.wait()
        return ToolResult(success=True, content=f"echo:{text}")


class MutatingArgumentsTool(Tool):
    def __init__(self) -> None:
        self.received_payload: dict | None = None

    @property
    def name(self) -> str:
        return "mutate"

    @property
    def description(self) -> str:
        return "Mutate a nested argument to test snapshot ownership."

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {"payload": {"type": "object"}},
            "required": ["payload"],
        }

    async def execute(self, payload: dict) -> ToolResult:
        self.received_payload = payload
        payload["items"].append("mutated")
        return ToolResult(success=True, content="mutated private input")


class RetainingResultTool(MutableEchoTool):
    def __init__(self) -> None:
        super().__init__()
        self.retained_result: ToolResult | None = None

    async def execute(self, text: str) -> ToolResult:
        self.calls.append(text)
        self.retained_result = ToolResult(success=True, content="original")
        return self.retained_result


class DefinitionDrivenLLM:
    def __init__(self) -> None:
        self.tool_snapshots: list[list[ToolDefinition]] = []

    async def generate(self, messages, tools=None) -> LLMResponse:
        assert tools is not None
        self.tool_snapshots.append(deepcopy(tools))
        return response(tool_call("metadata-call", "metadata", name=tools[0].name))


class RetainingResponseLLM:
    def __init__(self, retained_response: LLMResponse) -> None:
        self.retained_response = retained_response

    async def generate(self, messages, tools=None) -> LLMResponse:
        return self.retained_response


def build_session(tmp_path, llm, tools, *, max_steps: int = 1) -> AgentSession:
    return AgentSession(
        llm_client=llm,
        system_prompt="You are a test agent.",
        tools=tools,
        max_steps=max_steps,
        workspace_dir=str(tmp_path),
        session_id="tool-test-session",
    )


def test_duplicate_tool_names_are_rejected_at_registration(tmp_path) -> None:
    with pytest.raises(ValueError, match="Duplicate tool name: 'echo'"):
        build_session(
            tmp_path,
            DefinitionDrivenLLM(),
            [MutableEchoTool(), MutableEchoTool()],
        )


def test_empty_tool_name_is_rejected_at_registration(tmp_path) -> None:
    with pytest.raises(ValueError, match="Tool name must be a non-empty string"):
        build_session(tmp_path, DefinitionDrivenLLM(), [MutableEchoTool("")])


@pytest.mark.parametrize(
    ("attribute", "invalid_value", "expected_message"),
    [
        (
            "current_description",
            None,
            "Tool 'echo' description must be a string",
        ),
        (
            "current_parameters",
            [],
            "Tool 'echo' parameters must be a dictionary",
        ),
    ],
    ids=["description", "parameters"],
)
def test_invalid_tool_metadata_is_rejected_at_registration(
    tmp_path,
    attribute,
    invalid_value,
    expected_message,
) -> None:
    tool = MutableEchoTool()
    setattr(tool, attribute, invalid_value)

    with pytest.raises(TypeError, match=expected_message):
        build_session(tmp_path, DefinitionDrivenLLM(), [tool])


@pytest.mark.asyncio
async def test_registered_metadata_and_dispatch_name_are_frozen(tmp_path) -> None:
    llm = DefinitionDrivenLLM()
    tool = MutableEchoTool()
    session = build_session(tmp_path, llm, [tool])

    tool.current_name = "renamed"
    tool.current_description = "mutated description"
    tool.current_parameters["properties"]["text"]["type"] = "integer"
    outcome = await session.start_turn("use the registered tool").wait()

    assert outcome.stop_reason == "max_steps"
    assert tool.calls == ["metadata"]
    assert llm.tool_snapshots == [
        [
            ToolDefinition(
                name="echo",
                description="Return the supplied text.",
                parameters={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            )
        ]
    ]
    assert session.get_history()[-1].name == "echo"


@pytest.mark.asyncio
async def test_duplicate_ids_reject_the_batch_before_any_side_effect(tmp_path) -> None:
    llm = ScriptedLLM(
        [
            ScriptedCall(
                response(
                    tool_call("duplicate", "first"),
                    tool_call("duplicate", "second"),
                )
            )
        ]
    )
    tool = MutableEchoTool()
    session = build_session(tmp_path, llm, [tool])
    events = []

    with llm:
        outcome = await session.start_turn(
            "reject duplicate IDs",
            event_sink=events.append,
        ).wait()

    assert outcome.stop_reason == "failed"
    assert outcome.error is not None
    assert outcome.error.kind == "tool_protocol_error"
    assert "duplicate tool call ID 'duplicate'" in outcome.error.message
    assert tool.calls == []
    assert not any(
        isinstance(envelope.event, (ToolStarted, ToolFinished)) for envelope in events
    )
    assert [message.role for message in session.get_history()] == ["system", "user"]
    assert [
        envelope.event.status
        for envelope in events
        if isinstance(envelope.event, StepFinished)
    ] == ["failed"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_call",
    [
        tool_call("", "empty ID"),
        tool_call("wrong-type", "wrong type", call_type="computer"),
    ],
    ids=["empty-id", "wrong-type"],
)
async def test_invalid_call_structure_rejects_before_execution(
    tmp_path,
    invalid_call,
) -> None:
    llm = ScriptedLLM([ScriptedCall(response(invalid_call))])
    tool = MutableEchoTool()
    session = build_session(tmp_path, llm, [tool])

    with llm:
        outcome = await session.start_turn("reject invalid call").wait()

    assert outcome.stop_reason == "failed"
    assert outcome.error is not None
    assert outcome.error.kind == "tool_protocol_error"
    assert tool.calls == []
    assert [message.role for message in session.get_history()] == ["system", "user"]


@pytest.mark.asyncio
async def test_completed_call_id_can_be_reused_across_steps_and_turns(
    tmp_path,
) -> None:
    llm = ScriptedLLM(
        [
            ScriptedCall(response(tool_call("reused", "first"))),
            ScriptedCall(response(tool_call("reused", "second"))),
            ScriptedCall(response(tool_call("reused", "third"))),
            ScriptedCall(
                LLMResponse(
                    content="done",
                    tool_calls=[],
                    finish_reason="stop",
                    usage=None,
                )
            ),
        ]
    )
    tool = MutableEchoTool()
    session = build_session(tmp_path, llm, [tool], max_steps=2)

    with llm:
        first_turn = await session.start_turn("reuse within this turn").wait()
        second_turn = await session.start_turn("reuse in the next turn").wait()

    assert first_turn.stop_reason == "max_steps"
    assert second_turn.stop_reason == "end_turn"
    assert tool.calls == ["first", "second", "third"]
    assert [
        message.tool_call_id
        for message in session.get_history()
        if message.role == "tool"
    ] == ["reused", "reused", "reused"]


@pytest.mark.asyncio
async def test_mixed_batch_normalizes_every_result_in_model_order(tmp_path) -> None:
    llm = ScriptedLLM(
        [
            ScriptedCall(
                response(
                    tool_call("success", "ok"),
                    tool_call("unknown", "missing", name="missing"),
                    tool_call("exception", "explode", name="explode"),
                    tool_call("invalid", "invalid", name="invalid"),
                )
            )
        ]
    )
    echo = MutableEchoTool()
    exploding = ExplodingTool("explode")
    invalid = InvalidResultTool("invalid")
    session = build_session(tmp_path, llm, [echo, exploding, invalid])
    events = []

    with llm:
        outcome = await session.start_turn(
            "run a mixed batch",
            event_sink=events.append,
        ).wait()

    assert outcome.stop_reason == "max_steps"
    assert echo.calls == ["ok"]
    assert exploding.calls == ["explode"]
    assert invalid.calls == ["invalid"]
    tool_messages = session.get_history()[-4:]
    assert [message.tool_call_id for message in tool_messages] == [
        "success",
        "unknown",
        "exception",
        "invalid",
    ]
    assert tool_messages[0].content == "echo:ok"
    assert tool_messages[1].content == "Error: Unknown tool: missing"
    assert (
        "Error: Tool execution failed: ValueError: boom:explode"
        in tool_messages[2].content
    )
    assert (
        "Error: Tool contract violation: invalid returned NoneType"
        in tool_messages[3].content
    )
    assert [
        envelope.event.index
        for envelope in events
        if isinstance(envelope.event, ToolStarted)
    ] == [1, 2, 3, 4]
    assert [
        envelope.event.index
        for envelope in events
        if isinstance(envelope.event, ToolFinished)
    ] == [1, 2, 3, 4]


@pytest.mark.asyncio
async def test_observer_failure_isolated_from_tool_batch_and_history(
    tmp_path,
) -> None:
    llm = ScriptedLLM(
        [
            ScriptedCall(
                response(
                    tool_call("first", "first"),
                    tool_call("second", "second"),
                )
            )
        ]
    )
    tool = MutableEchoTool()
    session = build_session(tmp_path, llm, [tool])
    observed = []

    def fail_after_first_result(envelope) -> None:
        observed.append(envelope)
        if (
            isinstance(envelope.event, ToolFinished)
            and envelope.event.index == 1
        ):
            raise OSError("observer failed mid-batch")

    with llm:
        outcome = await session.start_turn(
            "finish the admitted batch",
            event_sink=fail_after_first_result,
        ).wait()

    assert outcome.stop_reason == "max_steps"
    assert outcome.error is None
    assert tool.calls == ["first", "second"]
    assert [message.role for message in session.get_history()[-3:]] == [
        "assistant",
        "tool",
        "tool",
    ]
    assert [
        message.tool_call_id for message in session.get_history()[-2:]
    ] == ["first", "second"]
    assert [
        (type(envelope.event), envelope.event.index)
        for envelope in observed
        if isinstance(envelope.event, (ToolStarted, ToolFinished))
    ] == [(ToolStarted, 1), (ToolFinished, 1)]


@pytest.mark.asyncio
async def test_history_is_unmodified_while_a_batch_is_in_progress(tmp_path) -> None:
    llm = ScriptedLLM(
        [
            ScriptedCall(
                response(
                    tool_call("first", "first"),
                    tool_call("blocked", "blocked", name="block"),
                )
            )
        ]
    )
    first = MutableEchoTool()
    blocked = BlockingEchoTool("block", block_text="blocked")
    session = build_session(tmp_path, llm, [first, blocked])

    handle = session.start_turn("commit only a complete batch")
    await blocked.entered.wait()

    assert first.calls == ["first"]
    assert blocked.calls == ["blocked"]
    assert [message.role for message in session.get_history()] == ["system", "user"]

    blocked.release.set()
    outcome = await handle.wait()
    llm.assert_complete()

    assert outcome.stop_reason == "max_steps"
    assert [message.role for message in session.get_history()[-3:]] == [
        "assistant",
        "tool",
        "tool",
    ]


@pytest.mark.asyncio
async def test_batch_is_serial_and_interrupts_only_after_all_calls_finish(
    tmp_path,
) -> None:
    llm = ScriptedLLM(
        [
            ScriptedCall(
                response(
                    tool_call("first", "first"),
                    tool_call("second", "second"),
                    tool_call("third", "third"),
                )
            )
        ]
    )
    tool = BlockingEchoTool(block_text="first")
    session = build_session(tmp_path, llm, [tool], max_steps=2)
    events = []

    handle = session.start_turn("interrupt inside the batch", event_sink=events.append)
    await tool.entered.wait()
    # Give an incorrectly parallel executor enough scheduling opportunities to
    # start later calls while the first one is still blocked.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert tool.calls == ["first"]
    assert handle.interrupt() is True
    tool.release.set()
    outcome = await handle.wait()
    llm.assert_complete()

    assert outcome.stop_reason == "interrupted"
    assert tool.calls == ["first", "second", "third"]
    assert [
        (
            "started"
            if isinstance(envelope.event, ToolStarted)
            else "finished",
            envelope.event.index,
        )
        for envelope in events
        if isinstance(envelope.event, (ToolStarted, ToolFinished))
    ] == [
        ("started", 1),
        ("finished", 1),
        ("started", 2),
        ("finished", 2),
        ("started", 3),
        ("finished", 3),
    ]
    assert [message.tool_call_id for message in session.get_history()[-3:]] == [
        "first",
        "second",
        "third",
    ]


@pytest.mark.asyncio
async def test_tool_argument_mutation_cannot_change_events_or_history(tmp_path) -> None:
    call = ToolCall(
        id="mutation",
        type="function",
        function=FunctionCall(
            name="mutate",
            arguments={"payload": {"items": ["original"]}},
        ),
    )
    llm = ScriptedLLM([ScriptedCall(response(call))])
    tool = MutatingArgumentsTool()
    session = build_session(tmp_path, llm, [tool])
    events = []

    with llm:
        outcome = await session.start_turn(
            "keep the model call immutable",
            event_sink=events.append,
        ).wait()

    assert outcome.stop_reason == "max_steps"
    assert tool.received_payload == {"items": ["original", "mutated"]}
    call_events = [
        envelope.event.call
        for envelope in events
        if isinstance(envelope.event, (ToolStarted, ToolFinished))
    ]
    assert [event.function.arguments for event in call_events] == [
        {"payload": {"items": ["original"]}},
        {"payload": {"items": ["original"]}},
    ]
    assistant_call = session.get_history()[-2].tool_calls[0]
    assert assistant_call.function.arguments == {
        "payload": {"items": ["original"]}
    }


@pytest.mark.asyncio
async def test_tool_result_alias_cannot_change_events_or_history(tmp_path) -> None:
    llm = ScriptedLLM([ScriptedCall(response(tool_call("result", "value")))])
    tool = RetainingResultTool()
    session = build_session(tmp_path, llm, [tool])
    finished_results: list[ToolResult] = []

    def mutate_retained_result(envelope) -> None:
        if not isinstance(envelope.event, ToolFinished):
            return
        finished_results.append(envelope.event.result)
        assert tool.retained_result is not None
        tool.retained_result.content = "mutated-after-event"

    with llm:
        outcome = await session.start_turn(
            "own the tool result",
            event_sink=mutate_retained_result,
        ).wait()

    assert outcome.stop_reason == "max_steps"
    assert [result.content for result in finished_results] == ["original"]
    assert tool.retained_result is not None
    assert tool.retained_result.content == "mutated-after-event"
    assert session.get_history()[-1].content == "original"


@pytest.mark.asyncio
async def test_model_client_cannot_mutate_an_admitted_tool_batch(tmp_path) -> None:
    retained_response = response(
        tool_call("first-call", "first", name="block"),
        tool_call("second-call", "second", name="block"),
    )
    llm = RetainingResponseLLM(retained_response)
    tool = BlockingEchoTool("block", block_text="first")
    session = build_session(tmp_path, llm, [tool])

    handle = session.start_turn("own the model response")
    await tool.entered.wait()
    try:
        retained_response.tool_calls[1].id = "first-call"
        retained_response.tool_calls[1].function.arguments["text"] = (
            "mutated-after-validation"
        )
    finally:
        tool.release.set()
    outcome = await handle.wait()

    assert outcome.stop_reason == "max_steps"
    assert tool.calls == ["first", "second"]
    assistant_message, *tool_messages = session.get_history()[-3:]
    assert [call.id for call in assistant_message.tool_calls] == [
        "first-call",
        "second-call",
    ]
    assert [call.function.arguments for call in assistant_message.tool_calls] == [
        {"text": "first"},
        {"text": "second"},
    ]
    assert [message.tool_call_id for message in tool_messages] == [
        "first-call",
        "second-call",
    ]


@pytest.mark.asyncio
async def test_agent_step_delegates_the_complete_batch_once(
    monkeypatch,
    tmp_path,
) -> None:
    calls = [
        tool_call("first", "first"),
        tool_call("second", "second"),
        tool_call("third", "third"),
    ]
    llm = ScriptedLLM([ScriptedCall(response(*calls))])
    tool = MutableEchoTool()
    session = build_session(tmp_path, llm, [tool])
    delegated_batches: list[list[ToolCall]] = []

    async def execute_batch(tool_calls, *, emit):
        delegated_batches.append(deepcopy(tool_calls))
        return tuple(
            Message(
                role="tool",
                content=f"delegated:{call.id}",
                tool_call_id=call.id,
                name=call.function.name,
            )
            for call in tool_calls
        )

    monkeypatch.setattr(session._tool_executor, "execute_batch", execute_batch)

    with llm:
        outcome = await session.start_turn("delegate one batch").wait()

    assert outcome.stop_reason == "max_steps"
    assert [[call.id for call in batch] for batch in delegated_batches] == [
        ["first", "second", "third"]
    ]
    assert tool.calls == []
    assert [message.content for message in session.get_history()[-3:]] == [
        "delegated:first",
        "delegated:second",
        "delegated:third",
    ]


@pytest.mark.asyncio
async def test_cli_renders_tool_protocol_failure_detail(tmp_path, capsys) -> None:
    llm = ScriptedLLM(
        [
            ScriptedCall(
                response(
                    tool_call("duplicate", "first"),
                    tool_call("duplicate", "second"),
                )
            )
        ]
    )
    session = build_session(tmp_path, llm, [MutableEchoTool()])

    with llm:
        outcome = await session.start_turn(
            "render the failure",
            event_sink=CliEventSink(logger=MagicMock()),
        ).wait()

    output = capsys.readouterr().out
    assert outcome.error is not None
    assert "(tool_protocol_error):" in output
    assert "duplicate tool call ID 'duplicate'" in output
