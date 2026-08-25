"""Vendor-neutral model call contract used by the agent loop."""

from dataclasses import dataclass
from typing import Any, Protocol

from ..schema import LLMResponse, Message


@dataclass(frozen=True)
class ToolDefinition:
    """One JSON Schema tool definition without protocol-specific wrapping."""

    name: str
    description: str
    parameters: dict[str, Any]


class ModelClient(Protocol):
    """The only model behavior required by the core agent loop."""

    async def generate(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
    ) -> LLMResponse: ...
