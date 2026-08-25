"""Offline contract tests for configured model API adapters."""

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import mini_agent.llm.anthropic_client as anthropic_module
import mini_agent.llm.factory as adapter_factory
import mini_agent.llm.openai_client as openai_module
from mini_agent.config import Config
from mini_agent.llm import AdapterName, create_model_client
from mini_agent.llm.anthropic_client import AnthropicAdapter
from mini_agent.llm.openai_client import OpenAIAdapter
from mini_agent.llm.protocol import ToolDefinition
from mini_agent.retry import RetryConfig
from mini_agent.schema import (
    FunctionCall,
    LLMResponse,
    Message,
    TokenUsage,
    ToolCall,
)


def config_data() -> dict:
    return {
        "api_key": "test-key",
        "adapter": "anthropic",
        "api_base": "https://model.example.test/messages",
        "model": "test-model",
        "max_output_tokens": 37,
    }


def write_config(path, data: dict) -> None:
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


@pytest.mark.parametrize(
    "missing_field",
    ["adapter", "api_base", "model", "max_output_tokens"],
)
def test_config_requires_explicit_model_adapter_fields(tmp_path, missing_field):
    data = config_data()
    del data[missing_field]
    path = tmp_path / "config.yaml"
    write_config(path, data)

    with pytest.raises(ValueError, match=missing_field):
        Config.from_yaml(path)


def test_config_rejects_unknown_adapter(tmp_path):
    data = config_data()
    data["adapter"] = "typo"
    path = tmp_path / "config.yaml"
    write_config(path, data)

    with pytest.raises(ValueError, match="adapter"):
        Config.from_yaml(path)


def test_config_preserves_explicit_model_adapter_fields(tmp_path):
    data = config_data()
    path = tmp_path / "config.yaml"
    write_config(path, data)

    config = Config.from_yaml(path)

    assert config.llm.adapter is AdapterName.ANTHROPIC
    assert config.llm.api_base == data["api_base"]
    assert config.llm.model == data["model"]
    assert config.llm.max_output_tokens == data["max_output_tokens"]
    assert config.agent.local_compaction_token_limit is None


def test_config_only_enables_local_compaction_with_explicit_limit(tmp_path):
    data = config_data()
    data["local_compaction_token_limit"] = 123
    path = tmp_path / "config.yaml"
    write_config(path, data)

    config = Config.from_yaml(path)

    assert config.agent.local_compaction_token_limit == 123


def test_config_rejects_nonpositive_local_compaction_limit(tmp_path):
    data = config_data()
    data["local_compaction_token_limit"] = 0
    path = tmp_path / "config.yaml"
    write_config(path, data)

    with pytest.raises(ValueError, match="local_compaction_token_limit"):
        Config.from_yaml(path)


def test_config_rejects_nonpositive_output_limit(tmp_path):
    data = config_data()
    data["max_output_tokens"] = 0
    path = tmp_path / "config.yaml"
    write_config(path, data)

    with pytest.raises(ValueError, match="max_output_tokens"):
        Config.from_yaml(path)


def test_config_example_tracks_explicit_adapter_schema(tmp_path):
    template_path = Path("mini_agent/config/config-example.yaml")
    data = yaml.safe_load(template_path.read_text(encoding="utf-8"))
    assert "provider" not in data

    data.update(
        api_key="test-key",
        api_base="https://model.example.test/messages",
        model="test-model",
        max_output_tokens=61,
    )
    path = tmp_path / "config.yaml"
    write_config(path, data)

    config = Config.from_yaml(path)

    assert config.llm.adapter is AdapterName.ANTHROPIC
    assert config.llm.max_output_tokens == 61
    assert config.agent.local_compaction_token_limit is None


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter_name", list(AdapterName))
async def test_factory_passes_endpoint_to_registered_adapter_verbatim(
    monkeypatch,
    adapter_name,
):
    class RecordingAdapter:
        def __init__(self, **settings):
            self.settings = settings
            self.requests = []

        async def generate(self, messages, tools=None):
            self.requests.append((messages, tools))
            return LLMResponse(
                content="recorded",
                thinking=None,
                tool_calls=None,
                finish_reason="stop",
                usage=None,
            )

    monkeypatch.setitem(
        adapter_factory._ADAPTERS,
        adapter_name,
        RecordingAdapter,
    )
    configured_endpoint = "https://api.minimax.io.evil/v1proxy/"
    client = create_model_client(
        adapter=adapter_name,
        api_key="test-key",
        api_base=configured_endpoint,
        model="test-model",
        max_output_tokens=41,
    )
    messages = [Message(role="user", content="hello")]
    tools = [
        ToolDefinition(
            name="echo",
            description="Echo text.",
            parameters={"type": "object"},
        )
    ]

    response = await client.generate(messages, tools)

    assert client.settings == {
        "api_key": "test-key",
        "api_base": configured_endpoint,
        "model": "test-model",
        "max_output_tokens": 41,
        "retry_config": None,
    }
    assert client.requests == [(messages, tools)]
    assert response.content == "recorded"


def test_factory_rejects_unknown_adapter():
    with pytest.raises(ValueError, match="Unsupported model adapter 'typo'"):
        create_model_client(
            adapter="typo",
            api_key="test-key",
            api_base="https://model.example.test",
            model="test-model",
            max_output_tokens=43,
        )


@pytest.mark.asyncio
async def test_anthropic_adapter_uses_only_explicit_base_protocol_fields(monkeypatch):
    init_kwargs = {}
    requests = []
    response = SimpleNamespace(
        content=[
            SimpleNamespace(
                type="thinking",
                thinking="unprobed response detail",
            ),
            SimpleNamespace(type="text", text="done"),
            SimpleNamespace(
                type="tool_use",
                id="call-1",
                name="echo",
                input={"text": "hi"},
            ),
        ],
        stop_reason="tool_use",
        usage=SimpleNamespace(
            input_tokens=7,
            output_tokens=3,
            cache_read_input_tokens=100,
            cache_creation_input_tokens=200,
        ),
    )

    class Messages:
        async def create(self, **kwargs):
            requests.append(kwargs)
            return response

    def build_sdk(**kwargs):
        init_kwargs.update(kwargs)
        return SimpleNamespace(messages=Messages())

    monkeypatch.setattr(anthropic_module.anthropic, "AsyncAnthropic", build_sdk)
    adapter = AnthropicAdapter(
        api_key="test-key",
        api_base="https://api.minimax.io.evil/v1proxy/",
        model="test-model",
        max_output_tokens=47,
        retry_config=RetryConfig(enabled=False),
    )
    tools = [
        ToolDefinition(
            name="echo",
            description="Echo text.",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string"}},
            },
        )
    ]

    result = await adapter.generate(
        [
            Message(role="system", content="system"),
            Message(role="user", content="hello"),
        ],
        tools,
    )

    assert init_kwargs == {
        "base_url": "https://api.minimax.io.evil/v1proxy/",
        "api_key": "test-key",
        "max_retries": 0,
    }
    assert requests == [
        {
            "model": "test-model",
            "max_tokens": 47,
            "messages": [{"role": "user", "content": "hello"}],
            "system": "system",
            "tools": [
                {
                    "name": "echo",
                    "description": "Echo text.",
                    "input_schema": tools[0].parameters,
                }
            ],
        }
    ]
    assert result.content == "done"
    assert result.thinking is None
    assert result.finish_reason == "tool_use"
    assert result.usage == TokenUsage(
        prompt_tokens=7,
        completion_tokens=3,
        total_tokens=10,
    )
    assert result.tool_calls == [
        ToolCall(
            id="call-1",
            type="function",
            function=FunctionCall(name="echo", arguments={"text": "hi"}),
        )
    ]


@pytest.mark.asyncio
async def test_openai_adapter_uses_only_explicit_base_protocol_fields(monkeypatch):
    init_kwargs = {}
    requests = []
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="tool_calls",
                message=SimpleNamespace(
                    content="done",
                    reasoning_details=[
                        SimpleNamespace(text="unprobed response detail")
                    ],
                    tool_calls=[
                        SimpleNamespace(
                            id="call-2",
                            function=SimpleNamespace(
                                name="echo",
                                arguments='{"text": "hi"}',
                            ),
                        )
                    ],
                ),
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=11,
            completion_tokens=5,
            total_tokens=16,
        ),
    )

    class Completions:
        async def create(self, **kwargs):
            requests.append(kwargs)
            return response

    def build_sdk(**kwargs):
        init_kwargs.update(kwargs)
        return SimpleNamespace(
            chat=SimpleNamespace(completions=Completions()),
        )

    monkeypatch.setattr(openai_module, "AsyncOpenAI", build_sdk)
    adapter = OpenAIAdapter(
        api_key="test-key",
        api_base="https://api.minimax.io.evil/v1proxy/",
        model="test-model",
        max_output_tokens=53,
        retry_config=RetryConfig(enabled=False),
    )
    tools = [
        ToolDefinition(
            name="echo",
            description="Echo text.",
            parameters={"type": "object"},
        )
    ]

    result = await adapter.generate(
        [
            Message(role="system", content="system"),
            Message(role="user", content="hello"),
        ],
        tools,
    )

    assert init_kwargs == {
        "api_key": "test-key",
        "base_url": "https://api.minimax.io.evil/v1proxy/",
        "max_retries": 0,
    }
    assert requests == [
        {
            "model": "test-model",
            "messages": [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "hello"},
            ],
            "max_tokens": 53,
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "echo",
                        "description": "Echo text.",
                        "parameters": tools[0].parameters,
                    },
                }
            ],
        }
    ]
    assert result.content == "done"
    assert result.thinking is None
    assert result.finish_reason == "tool_calls"
    assert result.usage == TokenUsage(
        prompt_tokens=11,
        completion_tokens=5,
        total_tokens=16,
    )
    assert result.tool_calls == [
        ToolCall(
            id="call-2",
            type="function",
            function=FunctionCall(name="echo", arguments={"text": "hi"}),
        )
    ]


def test_default_adapters_do_not_rebuild_unprobed_thinking_state():
    call = ToolCall(
        id="call-history",
        type="function",
        function=FunctionCall(name="echo", arguments={"text": "hi"}),
    )
    message = Message(
        role="assistant",
        content="visible answer",
        thinking="opaque continuation that cannot be reconstructed",
        tool_calls=[call],
    )
    tool_result = Message(
        role="tool",
        content="hi",
        tool_call_id="call-history",
    )

    _, anthropic_messages = AnthropicAdapter._convert_messages(
        None,
        [message, tool_result],
    )
    _, openai_messages = OpenAIAdapter._convert_messages(
        None,
        [message, tool_result],
    )

    assert anthropic_messages == [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "visible answer"},
                {
                    "type": "tool_use",
                    "id": "call-history",
                    "name": "echo",
                    "input": {"text": "hi"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call-history",
                    "content": "hi",
                }
            ],
        },
    ]
    assert openai_messages == [
        {
            "role": "assistant",
            "content": "visible answer",
            "tool_calls": [
                {
                    "id": "call-history",
                    "type": "function",
                    "function": {
                        "name": "echo",
                        "arguments": '{"text": "hi"}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-history",
            "content": "hi",
        },
    ]
