"""Offline regression tests for built-in tools."""

import io
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from mini_agent.tools import BashTool, EditTool, ReadTool, WriteTool
from mini_agent.tools.file_tools import MAX_READ_BYTES, MAX_READ_LINES


@pytest.mark.asyncio
async def test_read_file_returns_numbered_window_and_continuation(tmp_path: Path):
    path = tmp_path / "sample.txt"
    path.write_text("line 1\nline 2\nline 3\nline 4\nline 5\n", encoding="utf-8")

    result = await ReadTool(workspace_dir=str(tmp_path)).execute(
        path="sample.txt",
        offset=2,
        limit=2,
    )

    assert result.success is True
    assert result.error is None
    assert result.content == (
        "     2|line 2\n"
        "     3|line 3\n\n"
        "[Showing lines 2-3 of the requested file. Use offset=4 to continue.]"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "expected_error"),
    [
        ({"offset": 0}, "offset must be at least 1"),
        ({"limit": 0}, "limit must be at least 1"),
        (
            {"limit": MAX_READ_LINES + 1},
            f"limit must not exceed {MAX_READ_LINES}",
        ),
    ],
)
async def test_read_file_rejects_invalid_ranges(
    tmp_path: Path,
    arguments: dict[str, int],
    expected_error: str,
):
    (tmp_path / "sample.txt").write_text("content\n", encoding="utf-8")

    result = await ReadTool(workspace_dir=str(tmp_path)).execute(
        path="sample.txt",
        **arguments,
    )

    assert result.success is False
    assert result.content == ""
    assert result.error == f"Failed to read file 'sample.txt': {expected_error}"


@pytest.mark.asyncio
async def test_read_file_rejects_offset_beyond_end(tmp_path: Path):
    (tmp_path / "sample.txt").write_text("first\nsecond\n", encoding="utf-8")

    result = await ReadTool(workspace_dir=str(tmp_path)).execute(
        path="sample.txt",
        offset=3,
    )

    assert result.success is False
    assert result.content == ""
    assert result.error == (
        "Failed to read file 'sample.txt': offset 3 is beyond end of file (2 lines)"
    )


@pytest.mark.asyncio
async def test_read_file_applies_line_limit_and_reports_next_offset(tmp_path: Path):
    path = tmp_path / "sample.txt"
    path.write_text("".join(f"line {number}\n" for number in range(1, MAX_READ_LINES + 2)))

    result = await ReadTool(workspace_dir=str(tmp_path)).execute(path="sample.txt")

    assert result.success is True
    assert f"{MAX_READ_LINES:6d}|line {MAX_READ_LINES}" in result.content
    assert f"{MAX_READ_LINES + 1:6d}|line {MAX_READ_LINES + 1}" not in result.content
    assert result.content.endswith(
        f"[Showing lines 1-{MAX_READ_LINES} of the requested file. "
        f"Use offset={MAX_READ_LINES + 1} to continue.]"
    )


@pytest.mark.asyncio
async def test_read_file_applies_byte_limit_without_returning_partial_lines(tmp_path: Path):
    path = tmp_path / "sample.txt"
    path.write_text("".join(f"{number}:" + "界" * 600 + "\n" for number in range(1, 80)))

    result = await ReadTool(workspace_dir=str(tmp_path)).execute(path="sample.txt")

    assert result.success is True
    rendered_content, notice = result.content.split("\n\n", maxsplit=1)
    assert len(rendered_content.encode("utf-8")) <= MAX_READ_BYTES
    assert notice.startswith("[Showing lines 1-")
    assert f"({MAX_READ_BYTES}-byte numbered content limit)" in notice
    assert "Use offset=" in notice


@pytest.mark.asyncio
async def test_read_file_reports_when_one_line_exceeds_byte_limit(tmp_path: Path):
    path = tmp_path / "sample.txt"
    path.write_text("x" * (MAX_READ_BYTES + 1), encoding="utf-8")

    result = await ReadTool(workspace_dir=str(tmp_path)).execute(path="sample.txt")

    assert result.success is True
    assert result.error is None
    assert result.content == (
        f"Line 1 exceeds the {MAX_READ_BYTES}-byte numbered content limit. Use a "
        "byte-oriented tool to inspect it."
    )


@pytest.mark.asyncio
async def test_read_file_does_not_consume_an_oversized_selected_line(tmp_path: Path):
    class TrackingBytesIO(io.BytesIO):
        bytes_read = 0

        def readline(self, size: int = -1) -> bytes:
            chunk = super().readline(size)
            self.bytes_read += len(chunk)
            return chunk

    path = tmp_path / "sample.txt"
    path.touch()
    stream = TrackingBytesIO(b"x" * (MAX_READ_BYTES * 3))

    with patch("mini_agent.tools.file_tools.Path.open", return_value=stream):
        result = await ReadTool(workspace_dir=str(tmp_path)).execute(path="sample.txt")

    assert result.success is True
    assert result.error is None
    assert result.content == (
        f"Line 1 exceeds the {MAX_READ_BYTES}-byte numbered content limit. Use a "
        "byte-oriented tool to inspect it."
    )
    assert stream.bytes_read == MAX_READ_BYTES + 3


@pytest.mark.asyncio
async def test_read_file_keeps_a_complete_line_at_the_byte_boundary(tmp_path: Path):
    path = tmp_path / "sample.txt"
    line = "x" * (MAX_READ_BYTES - len("     1|".encode("utf-8")))
    path.write_text(f"{line}\nnext\n", encoding="utf-8")

    result = await ReadTool(workspace_dir=str(tmp_path)).execute(path="sample.txt")

    assert result.success is True
    rendered_content, notice = result.content.split("\n\n", maxsplit=1)
    assert len(rendered_content.encode("utf-8")) == MAX_READ_BYTES
    assert rendered_content == f"     1|{line}"
    assert notice == (
        f"[Showing lines 1-1 of the requested file "
        f"({MAX_READ_BYTES}-byte numbered content limit). Use offset=2 to continue.]"
    )


@pytest.mark.asyncio
async def test_read_file_stops_after_one_lookahead_line(tmp_path: Path):
    path = tmp_path / "sample.txt"
    path.write_bytes(b"first\nsecond\n\xff\n")

    result = await ReadTool(workspace_dir=str(tmp_path)).execute(
        path="sample.txt",
        limit=1,
    )

    assert result.success is True
    assert result.error is None
    assert result.content == (
        "     1|first\n\n"
        "[Showing lines 1-1 of the requested file. Use offset=2 to continue.]"
    )


@pytest.mark.asyncio
async def test_write_file_creates_parent_directories(tmp_path: Path):
    path = tmp_path / "nested" / "sample.txt"

    result = await WriteTool(workspace_dir=str(tmp_path)).execute(
        path="nested/sample.txt",
        content="created\n",
    )

    assert result.success is True
    assert result.error is None
    assert result.content == f"Created {path.resolve()}: 8 bytes."
    assert path.read_bytes() == b"created\n"


@pytest.mark.asyncio
async def test_write_file_preserves_existing_crlf_and_permissions(tmp_path: Path):
    path = tmp_path / "sample.txt"
    path.write_bytes(b"first\r\nsecond\r\n")
    path.chmod(0o754)

    result = await WriteTool(workspace_dir=str(tmp_path)).execute(
        path="sample.txt",
        content="changed\nsecond\n",
    )

    assert result.success is True
    assert result.error is None
    assert result.content == f"Wrote {path.resolve()}: 17 bytes."
    assert path.read_bytes() == b"changed\r\nsecond\r\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o754


@pytest.mark.asyncio
async def test_write_file_noop_does_not_replace_existing_file(tmp_path: Path):
    path = tmp_path / "sample.txt"
    path.write_bytes(b"first\r\nsecond\r\n")

    with patch("mini_agent.tools.file_tools.os.replace") as replace:
        result = await WriteTool(workspace_dir=str(tmp_path)).execute(
            path="sample.txt",
            content="first\nsecond\n",
        )

    assert result.success is True
    assert result.error is None
    assert result.content == (
        f"No changes: {path.resolve()} already contains the requested content."
    )
    assert path.read_bytes() == b"first\r\nsecond\r\n"
    replace.assert_not_called()


@pytest.mark.asyncio
async def test_write_file_atomic_replace_failure_preserves_original(tmp_path: Path):
    path = tmp_path / "sample.txt"
    path.write_text("original\n", encoding="utf-8")

    with patch(
        "mini_agent.tools.file_tools.os.replace",
        side_effect=OSError("replace failed"),
    ):
        result = await WriteTool(workspace_dir=str(tmp_path)).execute(
            path="sample.txt",
            content="replacement\n",
        )

    assert result.success is False
    assert result.content == ""
    assert result.error == "Failed to write file 'sample.txt': replace failed"
    assert path.read_bytes() == b"original\n"
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.asyncio
async def test_write_file_has_no_fallible_cleanup_after_commit(tmp_path: Path):
    path = tmp_path / "sample.txt"
    path.write_text("original\n", encoding="utf-8")

    with patch(
        "mini_agent.tools.file_tools.Path.unlink",
        side_effect=RuntimeError("cleanup failed"),
    ) as unlink:
        result = await WriteTool(workspace_dir=str(tmp_path)).execute(
            path="sample.txt",
            content="replacement\n",
        )

    assert result.success is True
    assert result.error is None
    assert path.read_bytes() == b"replacement\n"
    unlink.assert_not_called()


@pytest.mark.asyncio
async def test_write_file_failure_removes_new_parent_directories(tmp_path: Path):
    path = tmp_path / "new" / "nested" / "sample.txt"

    with patch(
        "mini_agent.tools.file_tools.os.replace",
        side_effect=OSError("replace failed"),
    ):
        result = await WriteTool(workspace_dir=str(tmp_path)).execute(
            path="new/nested/sample.txt",
            content="replacement\n",
        )

    assert result.success is False
    assert result.content == ""
    assert result.error == (
        "Failed to write file 'new/nested/sample.txt': replace failed"
    )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_write_file_failure_keeps_parent_created_by_another_actor(
    tmp_path: Path,
):
    parent = tmp_path / "shared"
    original_mkdir = Path.mkdir

    def create_parent_before_tool(directory: Path, *args, **kwargs) -> None:
        if directory == parent and not directory.exists():
            original_mkdir(directory)
        original_mkdir(directory, *args, **kwargs)

    with (
        patch.object(Path, "mkdir", new=create_parent_before_tool),
        patch(
            "mini_agent.tools.file_tools.os.replace",
            side_effect=OSError("replace failed"),
        ),
    ):
        result = await WriteTool(workspace_dir=str(tmp_path)).execute(
            path="shared/sample.txt",
            content="replacement\n",
        )

    assert result.success is False
    assert result.content == ""
    assert result.error == "Failed to write file 'shared/sample.txt': replace failed"
    assert parent.is_dir()
    assert list(parent.iterdir()) == []


@pytest.mark.asyncio
async def test_edit_file_replaces_multiline_match_and_preserves_crlf(tmp_path: Path):
    path = tmp_path / "sample.txt"
    path.write_bytes(b"first\r\nold value\r\nold detail\r\nlast\r\n")

    result = await EditTool(workspace_dir=str(tmp_path)).execute(
        path="sample.txt",
        old_str="old value\nold detail",
        new_str="new value\nnew detail",
    )

    assert result.success is True
    assert result.error is None
    assert result.content == f"Edited {path.resolve()}: 1 replacement at line 2."
    assert path.read_bytes() == b"first\r\nnew value\r\nnew detail\r\nlast\r\n"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("old_str", "new_str", "expected_error"),
    [
        ("", "new", "old_str must not be empty"),
        ("same", "same", "old_str and new_str must differ"),
        (
            "missing",
            "new",
            "Expected old_str to occur once in 'sample.txt', but found 0 occurrences.",
        ),
        (
            "same",
            "new",
            "Expected old_str to occur once in 'sample.txt', but found 2 possible "
            "match positions.",
        ),
    ],
)
async def test_edit_file_rejects_ambiguous_or_empty_changes_without_mutation(
    tmp_path: Path,
    old_str: str,
    new_str: str,
    expected_error: str,
):
    path = tmp_path / "sample.txt"
    original = b"same\nsame\n"
    path.write_bytes(original)

    result = await EditTool(workspace_dir=str(tmp_path)).execute(
        path="sample.txt",
        old_str=old_str,
        new_str=new_str,
    )

    assert result.success is False
    assert result.content == ""
    assert result.error == f"Failed to edit file 'sample.txt': {expected_error}"
    assert path.read_bytes() == original


@pytest.mark.asyncio
async def test_edit_file_rejects_overlapping_match_positions(tmp_path: Path):
    path = tmp_path / "sample.txt"
    path.write_text("aaa", encoding="utf-8")

    result = await EditTool(workspace_dir=str(tmp_path)).execute(
        path="sample.txt",
        old_str="aa",
        new_str="changed",
    )

    assert result.success is False
    assert result.content == ""
    assert result.error == (
        "Failed to edit file 'sample.txt': Expected old_str to occur once in "
        "'sample.txt', but found 2 possible match positions."
    )
    assert path.read_text(encoding="utf-8") == "aaa"


@pytest.mark.asyncio
async def test_edit_file_atomic_replace_failure_preserves_original(tmp_path: Path):
    path = tmp_path / "sample.txt"
    path.write_text("old\n", encoding="utf-8")

    with patch(
        "mini_agent.tools.file_tools.os.replace",
        side_effect=OSError("replace failed"),
    ):
        result = await EditTool(workspace_dir=str(tmp_path)).execute(
            path="sample.txt",
            old_str="old",
            new_str="new",
        )

    assert result.success is False
    assert result.content == ""
    assert result.error == "Failed to edit file 'sample.txt': replace failed"
    assert path.read_bytes() == b"old\n"
    assert list(tmp_path.iterdir()) == [path]


def test_file_tool_schemas_expose_enforced_limits():
    read_parameters = ReadTool().parameters["properties"]
    edit_parameters = EditTool().parameters["properties"]

    assert read_parameters["offset"]["minimum"] == 1
    assert read_parameters["limit"]["minimum"] == 1
    assert read_parameters["limit"]["maximum"] == MAX_READ_LINES
    assert "allow_multiple" not in edit_parameters


@pytest.mark.asyncio
async def test_bash_tool():
    tool = BashTool()

    result = await tool.execute(command="echo 'Hello from bash'")

    assert result.success is True
    assert result.error is None
    assert "Hello from bash" in result.content

    result = await tool.execute(command="exit 1")

    assert result.success is False
