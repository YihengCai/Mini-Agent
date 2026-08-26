"""Anthropic LLM client implementation."""

from typing import Any

import anthropic

from ..schema import FunctionCall, LLMResponse, Message, TokenUsage, ToolCall
from .protocol import ToolDefinition


class AnthropicAdapter:
    """Adapter for the Anthropic-compatible messages protocol.

    The adapter uses the Anthropic SDK for transport, but does not enable
    unprobed vendor extensions.
    """

    def __init__(
        self,
        api_key: str,
        api_base: str,
        model: str,
        max_output_tokens: int,
    ):
        """Initialize Anthropic client.

        Args:
            api_key: API key for authentication
            api_base: Exact base URL for the API
            model: Model name to use
            max_output_tokens: Maximum output tokens requested from the API
        """
        if max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be greater than zero")
        self.model = model
        self.max_output_tokens = max_output_tokens

        self.client = anthropic.AsyncAnthropic(
            base_url=api_base,
            api_key=api_key,
            max_retries=0,
        )

    def _convert_tools(self, tools: list[ToolDefinition]) -> list[dict[str, Any]]:
        """Convert tools to Anthropic format.

        Anthropic tool format:
        {
            "name": "tool_name",
            "description": "Tool description",
            "input_schema": {
                "type": "object",
                "properties": {...},
                "required": [...]
            }
        }

        Args:
            tools: List of Tool objects or dicts

        Returns:
            List of tools in Anthropic dict format
        """
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.parameters,
            }
            for tool in tools
        ]

    def _convert_messages(self, messages: list[Message]) -> tuple[str | None, list[dict[str, Any]]]:
        """Convert internal messages to Anthropic format.

        Args:
            messages: List of internal Message objects

        Returns:
            Tuple of (system_message, api_messages)
        """
        system_message = None
        api_messages = []

        for msg in messages:
            if msg.role == "system":
                system_message = msg.content
                continue

            # For user and assistant messages
            if msg.role in ["user", "assistant"]:
                if msg.role == "assistant" and msg.tool_calls:
                    content_blocks = []

                    if msg.content:
                        content_blocks.append({"type": "text", "text": msg.content})

                    for tool_call in msg.tool_calls:
                        content_blocks.append(
                            {
                                "type": "tool_use",
                                "id": tool_call.id,
                                "name": tool_call.function.name,
                                "input": tool_call.function.arguments,
                            }
                        )

                    api_messages.append({"role": "assistant", "content": content_blocks})
                else:
                    api_messages.append({"role": msg.role, "content": msg.content})

            # For tool result messages
            elif msg.role == "tool":
                # Anthropic uses user role with tool_result content blocks
                api_messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": msg.tool_call_id,
                                "content": msg.content,
                            }
                        ],
                    }
                )

        return system_message, api_messages

    def _parse_response(self, response: anthropic.types.Message) -> LLMResponse:
        """Parse Anthropic response into LLMResponse.

        Args:
            response: Anthropic Message response

        Returns:
            LLMResponse object
        """
        # Extract text content and tool calls. Thinking continuation requires
        # opaque signed blocks, which are outside this adapter's contract.
        text_content = ""
        tool_calls = []

        for block in response.content:
            if block.type == "text":
                text_content += block.text
            elif block.type == "tool_use":
                # Parse Anthropic tool_use block
                tool_calls.append(
                    ToolCall(
                        id=block.id,
                        type="function",
                        function=FunctionCall(
                            name=block.name,
                            arguments=block.input,
                        ),
                    )
                )

        # Only map the base protocol fields. Cache accounting is vendor-specific.
        usage = None
        if hasattr(response, "usage") and response.usage:
            input_tokens = response.usage.input_tokens or 0
            output_tokens = response.usage.output_tokens or 0
            usage = TokenUsage(
                prompt_tokens=input_tokens,
                completion_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
            )

        return LLMResponse(
            content=text_content,
            tool_calls=tool_calls if tool_calls else None,
            finish_reason=response.stop_reason,
            usage=usage,
        )

    async def generate(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
    ) -> LLMResponse:
        """Generate a response through the Anthropic-compatible adapter.

        Args:
            messages: List of conversation messages
            tools: Optional list of available tools

        Returns:
            LLMResponse containing the generated content
        """
        system_message, api_messages = self._convert_messages(messages)
        params = {
            "model": self.model,
            "max_tokens": self.max_output_tokens,
            "messages": api_messages,
        }
        if system_message:
            params["system"] = system_message
        if tools:
            params["tools"] = self._convert_tools(tools)

        response = await self.client.messages.create(**params)
        return self._parse_response(response)
