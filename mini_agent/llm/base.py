"""Base class for LLM clients."""

from abc import ABC, abstractmethod

from ..schema import LLMResponse, Message
from .protocol import ToolDefinition


class LLMAdapter(ABC):
    """Shared mechanics for one concrete model API adapter.

    Each subclass owns one wire contract. The core agent loop depends only on
    ``ModelClient`` from ``protocol.py``.
    """

    def __init__(
        self,
        api_key: str,
        api_base: str,
        model: str,
        max_output_tokens: int,
    ):
        """Initialize the LLM client.

        Args:
            api_key: API key for authentication
            api_base: Base URL for the API
            model: Model name to use
            max_output_tokens: Maximum output tokens requested from the API
        """
        if max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be greater than zero")
        self.api_key = api_key
        self.api_base = api_base
        self.model = model
        self.max_output_tokens = max_output_tokens

    @abstractmethod
    async def generate(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
    ) -> LLMResponse:
        """Generate response from LLM.

        Args:
            messages: List of conversation messages
            tools: Optional list of Tool objects or dicts

        Returns:
            LLMResponse containing the generated content, thinking, and tool calls
        """
        pass
