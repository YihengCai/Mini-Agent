"""Offline contract tests for the session note tools."""

import pytest

from mini_agent.tools.note_tool import RecallNoteTool, SessionNoteTool


@pytest.mark.asyncio
async def test_record_and_recall_notes(tmp_path):
    note_file = tmp_path / "notes.json"
    record_tool = SessionNoteTool(memory_file=str(note_file))
    recall_tool = RecallNoteTool(memory_file=str(note_file))

    first = await record_tool.execute(
        content="User prefers concise responses",
        category="user_preference",
    )
    second = await record_tool.execute(
        content="Project uses Python 3.12",
        category="project_info",
    )

    assert first.success
    assert second.success

    all_notes = await recall_tool.execute()
    assert all_notes.success
    assert "User prefers concise responses" in all_notes.content
    assert "Python 3.12" in all_notes.content

    filtered_notes = await recall_tool.execute(category="user_preference")
    assert filtered_notes.success
    assert "User prefers concise responses" in filtered_notes.content
    assert "Python 3.12" not in filtered_notes.content


@pytest.mark.asyncio
async def test_recall_without_storage_returns_empty_state(tmp_path):
    note_file = tmp_path / "missing-notes.json"

    result = await RecallNoteTool(memory_file=str(note_file)).execute()

    assert result.success
    assert "No notes recorded yet" in result.content


@pytest.mark.asyncio
async def test_note_persists_across_tool_instances(tmp_path):
    note_file = tmp_path / "notes.json"

    recorded = await SessionNoteTool(memory_file=str(note_file)).execute(
        content="Important fact to remember",
        category="test",
    )
    recalled = await RecallNoteTool(memory_file=str(note_file)).execute()

    assert recorded.success
    assert recalled.success
    assert "Important fact to remember" in recalled.content
