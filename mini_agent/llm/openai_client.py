"""OpenAI LLM client implementation."""

import json
from typing import Any

from openai import AsyncOpenAI

from ..schema import FunctionCall, LLMResponse, Message, TokenUsage, ToolCall
from .base import LLMAdapter
from .protocol import ToolDefinition

class OpenAIAdapter(LLMAdapter):
    """Adapter for the OpenAI-compatible chat completions protocol.

    The adapter uses the OpenAI SDK for transport, but does not enable
    unprobed vendor extensions or imply use of the OpenAI service.
    """

    def __init__(
        self,
        api_key: str,
        api_base: str,
        model: str,
        max_output_tokens: int,
    ):
        """Initialize OpenAI client.

        Args:
            api_key: API key for authentication
            api_base: Exact base URL for the API
            model: Model name to use
            max_output_tokens: Maximum output tokens requested from the API
        """
        super().__init__(api_key, api_base, model, max_output_tokens)

        # Initialize OpenAI client
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=api_base,
            max_retries=0,
        )

    async def _make_api_request(
        self,
        api_messages: list[dict[str, Any]],
        tools: list[ToolDefinition] | None = None,
    ) -> Any:
        """Execute one API request.

        Args:
            api_messages: List of messages in OpenAI format
            tools: Optional list of tools

        Returns:
            OpenAI ChatCompletion response (full response including usage)

        Raises:
            Exception: API call failed
        """
        params = {
            "model": self.model,
            "messages": api_messages,
            "max_tokens": self.max_output_tokens,
        }

        if tools:
            params["tools"] = self._convert_tools(tools)

        # Use OpenAI SDK's chat.completions.create
        response = await self.client.chat.completions.create(**params)
        # Return full response to access usage info
        return response

    def _convert_tools(self, tools: list[ToolDefinition]) -> list[dict[str, Any]]:
        """Convert tools to OpenAI format.

        Args:
            tools: List of Tool objects or dicts

        Returns:
            List of tools in OpenAI dict format
        """
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in tools
        ]

    def _convert_messages(self, messages: list[Message]) -> list[dict[str, Any]]:
        """Convert internal messages to OpenAI format.

        Args:
            messages: List of internal Message objects

        Returns:
            Messages in OpenAI format, including the system message
        """
        api_messages = []

        for msg in messages:
            if msg.role == "system":
                # OpenAI includes system message in messages array
                api_messages.append({"role": "system", "content": msg.content})
                continue

            # For user messages
            if msg.role == "user":
                api_messages.append({"role": "user", "content": msg.content})

            # For assistant messages
            elif msg.role == "assistant":
                assistant_msg = {"role": "assistant"}

                # Add content if present
                if msg.content:
                    assistant_msg["content"] = msg.content

                # Add tool calls if present
                if msg.tool_calls:
                    tool_calls_list = []
                    for tool_call in msg.tool_calls:
                        tool_calls_list.append(
                            {
                                "id": tool_call.id,
                                "type": "function",
                                "function": {
                                    "name": tool_call.function.name,
                                    "arguments": json.dumps(tool_call.function.arguments),
                                },
                            }
                        )
                    assistant_msg["tool_calls"] = tool_calls_list

                api_messages.append(assistant_msg)

            # For tool result messages
            elif msg.role == "tool":
                api_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": msg.tool_call_id,
                        "content": msg.content,
                    }
                )

        return api_messages

    def _parse_response(self, response: Any) -> LLMResponse:
        """Parse OpenAI response into LLMResponse.

        Args:
            response: OpenAI ChatCompletion response (full response object)

        Returns:
            LLMResponse object
        """
        choice = response.choices[0]
        message = choice.message

        # Extract text content
        text_content = message.content or ""

        # Extract tool calls
        tool_calls = []
        if message.tool_calls:
            for tool_call in message.tool_calls:
                # Parse arguments from JSON string
                arguments = json.loads(tool_call.function.arguments)

                tool_calls.append(
                    ToolCall(
                        id=tool_call.id,
                        type="function",
                        function=FunctionCall(
                            name=tool_call.function.name,
                            arguments=arguments,
                        ),
                    )
                )

        # Extract token usage from response
        usage = None
        if hasattr(response, "usage") and response.usage:
            usage = TokenUsage(
                prompt_tokens=response.usage.prompt_tokens or 0,
                completion_tokens=response.usage.completion_tokens or 0,
                total_tokens=response.usage.total_tokens or 0,
            )

        return LLMResponse(
            content=text_content,
            tool_calls=tool_calls if tool_calls else None,
            finish_reason=choice.finish_reason,
            usage=usage,
        )

    async def generate(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
    ) -> LLMResponse:
        """Generate a response through the OpenAI-compatible adapter.

        Args:
            messages: List of conversation messages
            tools: Optional list of available tools

        Returns:
            LLMResponse containing the generated content
        """
        api_messages = self._convert_messages(messages)
        response = await self._make_api_request(
            api_messages,
            tools,
        )
        return self._parse_response(response)
