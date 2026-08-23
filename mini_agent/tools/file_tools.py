"""Deterministic text-file tools for model-facing reads and edits."""

import os
import stat
import tempfile
from pathlib import Path
from typing import Any, BinaryIO

from .base import Tool, ToolResult


# These are model-facing output limits, not estimates of a vendor token count.
MAX_READ_LINES = 2000
MAX_READ_BYTES = 50 * 1024


def _require_string(name: str, value: object) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value


def _require_integer(name: str, value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    return value


def _resolve_path(workspace_dir: Path, requested_path: str) -> Path:
    """Resolve a user-supplied path without claiming to enforce permissions."""
    if not requested_path or not requested_path.strip():
        raise ValueError("path must not be empty")

    path = Path(requested_path).expanduser()
    if not path.is_absolute():
        path = workspace_dir / path
    return path.resolve(strict=False)


def _require_regular_file(path: Path, requested_path: str) -> None:
    if not path.exists():
        raise FileNotFoundError(f"File not found: {requested_path}")
    if not path.is_file():
        raise ValueError(f"Path is not a regular file: {requested_path}")


def _read_utf8(path: Path) -> str:
    """Read UTF-8 without universal-newline conversion."""
    return path.read_bytes().decode("utf-8")


def _first_newline_style(text: str) -> str | None:
    newline_index = text.find("\n")
    if newline_index < 0:
        return None
    if newline_index > 0 and text[newline_index - 1] == "\r":
        return "\r\n"
    return "\n"


def _use_newline_style(text: str, newline_style: str | None) -> str:
    """Adapt caller-provided line breaks to an existing file's convention."""
    if newline_style is None:
        return text
    normalized = text.replace("\r\n", "\n")
    if newline_style == "\r\n":
        return normalized.replace("\n", "\r\n")
    return normalized


def _parent_candidates(parent: Path) -> list[Path]:
    """Return currently missing parents from shallowest to deepest."""
    missing: list[Path] = []
    current = parent
    while not current.exists():
        missing.append(current)
        current = current.parent
    return list(reversed(missing))


def _atomic_write(path: Path, data: bytes, existing_mode: int | None) -> None:
    """Make one complete file replacement atomically visible."""
    parent_candidates = _parent_candidates(path.parent)
    created_directories: list[Path] = []
    temporary_path: Path | None = None
    file_descriptor: int | None = None

    try:
        for directory in parent_candidates:
            try:
                directory.mkdir()
            except FileExistsError:
                if not directory.is_dir():
                    raise
            else:
                created_directories.append(directory)
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        temporary_path = Path(temporary_name)
        temporary_file = os.fdopen(file_descriptor, "wb")
        file_descriptor = None
        with temporary_file as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        if existing_mode is not None:
            os.chmod(temporary_path, existing_mode)
        os.replace(temporary_path, path)
    except Exception:
        if file_descriptor is not None:
            try:
                os.close(file_descriptor)
            except OSError:
                pass
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except Exception:
                pass
        for directory in reversed(created_directories):
            try:
                directory.rmdir()
            except OSError:
                pass
        raise


def _read_bounded_binary_line(
    file: BinaryIO,
    *,
    consume_overflow: bool = False,
) -> tuple[bytes | None, int | None, bool]:
    """Read one logical line with bounded memory.

    The second item is the line-content byte count when known. The third reports
    whether the line exceeded the retained buffer. Oversized lines are consumed
    only while seeking to a later offset.
    """
    chunk_size = MAX_READ_BYTES + 3
    chunk = file.readline(chunk_size)
    if chunk == b"":
        return None, None, False

    retained = chunk if len(chunk) < chunk_size else None
    total_bytes = len(chunk)
    tail = chunk[-2:]

    while len(chunk) == chunk_size and not chunk.endswith(b"\n"):
        if not consume_overflow:
            return None, None, True
        chunk = file.readline(chunk_size)
        if chunk == b"":
            break
        total_bytes += len(chunk)
        tail = (tail + chunk)[-2:]

    line_ending_bytes = 0
    if tail.endswith(b"\r\n"):
        line_ending_bytes = 2
    elif tail.endswith(b"\n"):
        line_ending_bytes = 1
    content_bytes = total_bytes - line_ending_bytes

    if retained is None:
        return None, content_bytes, True
    if line_ending_bytes:
        retained = retained[:-line_ending_bytes]
    if content_bytes > MAX_READ_BYTES:
        return None, content_bytes, True
    return retained, content_bytes, False


def _normalize_crlf_with_offsets(text: str) -> tuple[str, list[int]]:
    """Normalize CRLF to LF and map normalized indices back to raw indices."""
    normalized: list[str] = []
    raw_offsets: list[int] = []
    raw_index = 0
    while raw_index < len(text):
        raw_offsets.append(raw_index)
        if text.startswith("\r\n", raw_index):
            normalized.append("\n")
            raw_index += 2
        else:
            normalized.append(text[raw_index])
            raw_index += 1
    raw_offsets.append(len(text))
    return "".join(normalized), raw_offsets


def _count_overlapping_matches(text: str, needle: str) -> tuple[int, int]:
    """Return the count of possible start positions and the first position."""
    count = 0
    first_position = -1
    search_from = 0
    while True:
        position = text.find(needle, search_from)
        if position < 0:
            return count, first_position
        if first_position < 0:
            first_position = position
        count += 1
        search_from = position + 1


def _format_read_error(requested_path: str, error: Exception) -> ToolResult:
    return ToolResult(
        success=False,
        content="",
        error=f"Failed to read file '{requested_path}': {error}",
    )


class ReadTool(Tool):
    """Read a bounded, numbered window from a UTF-8 text file."""

    def __init__(self, workspace_dir: str = "."):
        self.workspace_dir = Path(workspace_dir).expanduser().resolve()

    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return (
            "Read a UTF-8 text file with 1-based line numbers. Relative paths resolve "
            "against the workspace; absolute paths are accepted. Use offset and limit "
            f"for large files. Numbered file content is limited to {MAX_READ_LINES} "
            f"complete lines or {MAX_READ_BYTES} UTF-8 bytes and reports the next "
            "offset when truncated."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path or path relative to the workspace",
                },
                "offset": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "First line to return (1-based; defaults to 1)",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_READ_LINES,
                    "description": (
                        "Maximum complete lines to return before the byte limit "
                        f"(defaults to {MAX_READ_LINES})"
                    ),
                },
            },
            "required": ["path"],
        }

    async def execute(
        self,
        path: str,
        offset: int | None = None,
        limit: int | None = None,
    ) -> ToolResult:
        try:
            _require_string("path", path)
            requested_offset = (
                1 if offset is None else _require_integer("offset", offset)
            )
            requested_limit = (
                MAX_READ_LINES if limit is None else _require_integer("limit", limit)
            )
            if requested_offset < 1:
                raise ValueError("offset must be at least 1")
            if requested_limit < 1:
                raise ValueError("limit must be at least 1")
            if requested_limit > MAX_READ_LINES:
                raise ValueError(f"limit must not exceed {MAX_READ_LINES}")

            file_path = _resolve_path(self.workspace_dir, path)
            _require_regular_file(file_path, path)

            rendered_lines: list[str] = []
            rendered_bytes = 0
            line_number = 0
            reached_eof = False
            has_more = False
            stopped_by_bytes = False
            next_offset: int | None = None

            with file_path.open("rb") as file:
                while line_number < requested_offset - 1:
                    _, line_bytes, _ = _read_bounded_binary_line(
                        file, consume_overflow=True
                    )
                    if line_bytes is None:
                        raise ValueError(
                            f"offset {requested_offset} is beyond end of file "
                            f"({line_number} lines)"
                        )
                    line_number += 1

                while len(rendered_lines) < requested_limit:
                    raw_line, line_content_bytes, exceeded_line_limit = (
                        _read_bounded_binary_line(file)
                    )
                    if line_content_bytes is None and not exceeded_line_limit:
                        reached_eof = True
                        break

                    line_number += 1
                    if exceeded_line_limit:
                        if not rendered_lines:
                            return ToolResult(
                                success=True,
                                content=(
                                    f"Line {line_number} exceeds the {MAX_READ_BYTES}-byte "
                                    "numbered content limit. Use a byte-oriented tool "
                                    "to inspect it."
                                ),
                            )
                        has_more = True
                        stopped_by_bytes = True
                        next_offset = line_number
                        break

                    line_content = raw_line.decode("utf-8")
                    rendered_line = f"{line_number:6d}|{line_content}"
                    rendered_line_bytes = len(rendered_line.encode("utf-8"))
                    separator_bytes = 1 if rendered_lines else 0
                    if (
                        rendered_bytes + separator_bytes + rendered_line_bytes
                        > MAX_READ_BYTES
                    ):
                        if not rendered_lines:
                            return ToolResult(
                                success=True,
                                content=(
                                    f"Line {line_number} is {line_content_bytes} bytes, "
                                    "but its numbered form exceeds the "
                                    f"{MAX_READ_BYTES}-byte content limit. Use a "
                                    "byte-oriented tool to inspect it."
                                ),
                            )
                        has_more = True
                        stopped_by_bytes = True
                        next_offset = line_number
                        break

                    rendered_lines.append(rendered_line)
                    rendered_bytes += separator_bytes + rendered_line_bytes

                if (
                    len(rendered_lines) == requested_limit
                    and not reached_eof
                    and not has_more
                ):
                    if file.read(1) != b"":
                        has_more = True
                        next_offset = line_number + 1

            if not rendered_lines and reached_eof:
                if requested_offset != 1 or line_number != 0:
                    raise ValueError(
                        f"offset {requested_offset} is beyond end of file "
                        f"({line_number} lines)"
                    )
                return ToolResult(success=True, content="File is empty.")

            content = "\n".join(rendered_lines)
            if has_more:
                displayed_end = requested_offset + len(rendered_lines) - 1
                byte_note = (
                    f" ({MAX_READ_BYTES}-byte numbered content limit)"
                    if stopped_by_bytes
                    else ""
                )
                content += (
                    f"\n\n[Showing lines {requested_offset}-{displayed_end} of "
                    f"the requested file{byte_note}. Use offset={next_offset} to continue.]"
                )

            return ToolResult(success=True, content=content)
        except Exception as error:
            return _format_read_error(path, error)


class WriteTool(Tool):
    """Create or atomically replace a UTF-8 text file."""

    def __init__(self, workspace_dir: str = "."):
        self.workspace_dir = Path(workspace_dir).expanduser().resolve()

    @property
    def name(self) -> str:
        return "write_file"

    @property
    def description(self) -> str:
        return (
            "Create a UTF-8 text file or replace its complete contents. Relative paths "
            "resolve against the workspace; absolute paths are accepted. Existing CRLF "
            "line endings and permission bits are preserved. Prefer edit_file for a "
            "targeted change to an existing file."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path or path relative to the workspace",
                },
                "content": {
                    "type": "string",
                    "description": "Complete UTF-8 text content for the file",
                },
            },
            "required": ["path", "content"],
        }

    async def execute(self, path: str, content: str) -> ToolResult:
        try:
            _require_string("path", path)
            _require_string("content", content)
            file_path = _resolve_path(self.workspace_dir, path)
            existed = file_path.exists()
            existing_mode: int | None = None

            if existed:
                if not file_path.is_file():
                    raise ValueError(f"Path is not a regular file: {path}")
                current_text = _read_utf8(file_path)
                newline_style = _first_newline_style(current_text)
                final_content = _use_newline_style(content, newline_style)
                current_data = current_text.encode("utf-8")
                final_data = final_content.encode("utf-8")
                if final_data == current_data:
                    return ToolResult(
                        success=True,
                        content=(
                            f"No changes: {file_path} already contains the requested "
                            "content."
                        ),
                    )
                existing_mode = stat.S_IMODE(file_path.stat().st_mode)
            else:
                final_data = content.encode("utf-8")

            _atomic_write(file_path, final_data, existing_mode)
            action = "Wrote" if existed else "Created"
            return ToolResult(
                success=True,
                content=f"{action} {file_path}: {len(final_data)} bytes.",
            )
        except Exception as error:
            return ToolResult(
                success=False,
                content="",
                error=f"Failed to write file '{path}': {error}",
            )


class EditTool(Tool):
    """Atomically replace one unique text match in an existing UTF-8 file."""

    def __init__(self, workspace_dir: str = "."):
        self.workspace_dir = Path(workspace_dir).expanduser().resolve()

    @property
    def name(self) -> str:
        return "edit_file"

    @property
    def description(self) -> str:
        return (
            "Replace one unique, exact text match in an existing UTF-8 file. CRLF and "
            "LF are treated as the same logical line break, while unrelated bytes and "
            "permission bits are preserved. Include enough surrounding context to make "
            "old_str unique. Empty searches, missing or ambiguous matches, and no-op "
            "edits fail before the target file is replaced."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path or path relative to the workspace",
                },
                "old_str": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Exact logical text to replace; must be unique",
                },
                "new_str": {
                    "type": "string",
                    "description": "Exact replacement text",
                },
            },
            "required": ["path", "old_str", "new_str"],
        }

    async def execute(
        self,
        path: str,
        old_str: str,
        new_str: str,
    ) -> ToolResult:
        try:
            _require_string("path", path)
            _require_string("old_str", old_str)
            _require_string("new_str", new_str)
            if old_str == "":
                raise ValueError("old_str must not be empty")

            file_path = _resolve_path(self.workspace_dir, path)
            _require_regular_file(file_path, path)
            current_content = _read_utf8(file_path)
            normalized_content, raw_offsets = _normalize_crlf_with_offsets(
                current_content
            )
            search_text = old_str.replace("\r\n", "\n")
            replacement_text = new_str.replace("\r\n", "\n")

            if search_text == replacement_text:
                raise ValueError("old_str and new_str must differ")

            occurrences, first_match = _count_overlapping_matches(
                normalized_content, search_text
            )
            if occurrences == 0:
                raise ValueError(
                    f"Expected old_str to occur once in '{path}', but found 0 occurrences."
                )
            if occurrences != 1:
                raise ValueError(
                    f"Expected old_str to occur once in '{path}', but found "
                    f"{occurrences} possible match positions."
                )

            raw_start = raw_offsets[first_match]
            raw_end = raw_offsets[first_match + len(search_text)]
            raw_match = current_content[raw_start:raw_end]
            newline_style = _first_newline_style(raw_match) or _first_newline_style(
                current_content
            )
            replacement_text = _use_newline_style(replacement_text, newline_style)
            new_content = (
                current_content[:raw_start]
                + replacement_text
                + current_content[raw_end:]
            )
            existing_mode = stat.S_IMODE(file_path.stat().st_mode)
            _atomic_write(file_path, new_content.encode("utf-8"), existing_mode)

            line_number = normalized_content.count("\n", 0, first_match) + 1
            return ToolResult(
                success=True,
                content=f"Edited {file_path}: 1 replacement at line {line_number}.",
            )
        except Exception as error:
            return ToolResult(
                success=False,
                content="",
                error=f"Failed to edit file '{path}': {error}",
            )
