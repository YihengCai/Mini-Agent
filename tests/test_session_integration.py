"""
Session integration tests - Testing multi-turn conversations and session management
"""

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mini_agent import LLMClient
from mini_agent.agent import AgentSession
from mini_agent.schema import FunctionCall, LLMResponse, Message, ToolCall
from mini_agent.tools.bash_tool import BashTool
from mini_agent.tools.file_tools import ReadTool, WriteTool
from mini_agent.tools.note_tool import RecallNoteTool, SessionNoteTool


def response(content: str, *, tool_calls=None, finish_reason: str = "stop"):
    return LLMResponse(
        content=content,
        thinking=None,
        tool_calls=tool_calls,
        finish_reason=finish_reason,
        usage=None,
    )


@pytest.fixture
def mock_llm_client():
    """Create mock LLM client"""
    client = MagicMock(spec=LLMClient)
    return client


@pytest.fixture
def temp_workspace():
    """Create temporary workspace directory"""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.mark.asyncio
async def test_multi_turn_conversation(mock_llm_client, temp_workspace):
    """Test multi-turn conversation and context sharing"""
    # Prepare test data
    system_prompt = "You are an intelligent assistant"
    tools = [
        ReadTool(workspace_dir=temp_workspace),
        WriteTool(workspace_dir=temp_workspace),
        SessionNoteTool(),
    ]

    # Create agent
    mock_llm_client.generate = AsyncMock(
        side_effect=[response("Hello back"), response("Ready to help")]
    )
    agent = AgentSession(
        llm_client=mock_llm_client,
        system_prompt=system_prompt,
        tools=tools,
        workspace_dir=temp_workspace,
    )

    # Verify initial state
    assert len(agent.get_history()) == 1  # Only system prompt
    assert agent.get_history()[0].role == "system"
    # Agent automatically adds workspace info to system prompt
    assert system_prompt in agent.get_history()[0].content
    assert "Current Workspace" in agent.get_history()[0].content

    # Run two distinct Turns in one conversation Session.
    first = await agent.start_turn("Hello").wait()
    second = await agent.start_turn("Help me create a file").wait()
    assert first.turn_id != second.turn_id

    # Verify all messages are retained in the shared Session history.
    user_messages = [m for m in agent.get_history() if m.role == "user"]
    assert len(user_messages) == 2
    assert user_messages[0].content == "Hello"
    assert user_messages[1].content == "Help me create a file"


@pytest.mark.asyncio
async def test_new_conversation_uses_a_new_session(mock_llm_client, temp_workspace):
    """A Session is one conversation; starting over creates another Session."""
    mock_llm_client.generate = AsyncMock(
        side_effect=[response(f"Reply {i}") for i in range(5)]
    )
    agent = AgentSession(
        llm_client=mock_llm_client,
        system_prompt="System prompt",
        tools=[],
        workspace_dir=temp_workspace,
    )

    # Complete multiple Turns in one conversation.
    for i in range(5):
        await agent.start_turn(f"Message {i}").wait()

    # Verify message count (1 system + 5 user + 5 assistant)
    assert len(agent.get_history()) == 11

    next_conversation = AgentSession(
        llm_client=mock_llm_client,
        system_prompt="System prompt",
        tools=[],
        workspace_dir=temp_workspace,
    )
    assert next_conversation.session_id != agent.session_id
    assert [message.role for message in next_conversation.get_history()] == ["system"]
    assert len(agent.get_history()) == 11


@pytest.mark.asyncio
async def test_get_history(mock_llm_client, temp_workspace):
    """Test getting session history"""
    mock_llm_client.generate = AsyncMock(return_value=response("Assistant reply"))
    agent = AgentSession(
        llm_client=mock_llm_client,
        system_prompt="System",
        tools=[],
        workspace_dir=temp_workspace,
    )

    await agent.start_turn("Test message").wait()

    # Get history
    history = agent.get_history()

    # Verify history is a copy (doesn't affect original messages)
    assert len(history) == len(agent.get_history())
    assert history is not agent.get_history()

    # Modifying copy should not affect original messages
    history.append(Message(role="user", content="New message"))
    assert len(agent.get_history()) == 3  # Original messages unchanged
    assert len(history) == 4  # Copy changed


@pytest.mark.asyncio
async def test_session_note_persistence(temp_workspace):
    """Test SessionNoteTool persistence functionality"""
    memory_file = Path(temp_workspace) / "memory.json"

    # Create first tool instance and record note
    record_tool = SessionNoteTool(memory_file=str(memory_file))
    result1 = await record_tool.execute(content="Test note", category="test")
    assert result1.success

    # Create second tool instance (simulating new session)
    recall_tool = RecallNoteTool(memory_file=str(memory_file))

    # Verify ability to read previous notes
    result2 = await recall_tool.execute()
    assert result2.success
    assert "Test note" in result2.content


@pytest.mark.asyncio
async def test_message_statistics(mock_llm_client, temp_workspace):
    """Test message statistics functionality"""
    tool_call = ToolCall(
        id="note-1",
        type="function",
        function=FunctionCall(
            name="record_note",
            arguments={"content": "remember this", "category": "test"},
        ),
    )
    mock_llm_client.generate = AsyncMock(
        side_effect=[
            response("", tool_calls=[tool_call], finish_reason="tool_use"),
            response("First reply"),
            response("Second reply"),
        ]
    )
    agent = AgentSession(
        llm_client=mock_llm_client,
        system_prompt="System",
        tools=[
            SessionNoteTool(
                memory_file=str(Path(temp_workspace) / "stats-memory.json")
            )
        ],
        workspace_dir=temp_workspace,
    )

    await agent.start_turn("User message 1").wait()
    await agent.start_turn("User message 2").wait()

    # Count different types of messages
    history = agent.get_history()
    user_msgs = sum(1 for m in history if m.role == "user")
    assistant_msgs = sum(1 for m in history if m.role == "assistant")
    tool_msgs = sum(1 for m in history if m.role == "tool")

    assert user_msgs == 2
    assert assistant_msgs == 3
    assert tool_msgs == 1
    assert len(history) == 7  # 1 system + 2 user + 3 assistant + 1 tool
