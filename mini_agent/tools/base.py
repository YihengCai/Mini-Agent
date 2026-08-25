"""Base tool classes."""

from typing import Any

from pydantic import BaseModel


class ToolResult(BaseModel):
    """Tool execution result."""

    success: bool
    content: str = ""
    error: str | None = None


class Tool:
    """Base class for all tools."""

    @property
    def name(self) -> str:
        """Tool name."""
        raise NotImplementedError

    @property
    def description(self) -> str:
        """Tool description."""
        raise NotImplementedError

    @property
    def parameters(self) -> dict[str, Any]:
        """Tool parameters schema (JSON Schema format)."""
        raise NotImplementedError

    async def execute(self, *args, **kwargs) -> ToolResult:  # type: ignore
        """Execute the tool with arbitrary arguments."""
        raise NotImplementedError
