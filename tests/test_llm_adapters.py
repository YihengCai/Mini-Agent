"""Offline contract tests for configured model API adapters."""

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import mini_agent.llm.anthropic_client as anthropic_module
import mini_agent.llm.factory as adapter_factory
import mini_agent.llm.openai_client as openai_module
from mini_agent.config import (
    AgentConfig,
    Config,
    LLMConfig,
    MCPConfig,
    RetryConfig as FileRetryConfig,
    ToolsConfig,
)
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
    ["api_key", "adapter", "api_base", "model", "max_output_tokens"],
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


def test_config_rejects_placeholder_api_key(tmp_path):
    data = config_data()
    data["api_key"] = "YOUR_API_KEY_HERE"
    path = tmp_path / "config.yaml"
    write_config(path, data)

    with pytest.raises(ValueError, match="valid API Key"):
        Config.from_yaml(path)


def test_config_requires_mapping_root(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("- api_key\n", encoding="utf-8")

    with pytest.raises(ValueError, match="root must be a mapping"):
        Config.from_yaml(path)


def test_config_rejects_legacy_provider_field(tmp_path):
    data = config_data()
    data["provider"] = "openai"
    path = tmp_path / "config.yaml"
    write_config(path, data)

    with pytest.raises(ValueError, match="'provider'.*'adapter'"):
        Config.from_yaml(path)


def test_llm_config_rejects_unknown_programmatic_fields():
    with pytest.raises(ValueError, match="provider"):
        LLMConfig(
            api_key="test-key",
            adapter="anthropic",
            api_base="https://model.example.test/messages",
            model="test-model",
            max_output_tokens=37,
            provider="openai",
        )


@pytest.mark.parametrize(
    "model",
    [Config, AgentConfig, FileRetryConfig, MCPConfig, ToolsConfig],
)
def test_config_models_reject_unknown_programmatic_fields(model):
    with pytest.raises(ValueError, match="unknown_field"):
        model.model_validate({"unknown_field": True})


@pytest.mark.parametrize(
    ("unknown_data", "unknown_field"),
    [
        ({"max_step": 1}, "max_step"),
        ({"retry": {"max_retry": 0}}, "max_retry"),
        ({"tools": {"enable_mpc": False}}, "enable_mpc"),
        ({"tools": {"mcp": {"connect_timout": 1.0}}}, "connect_timout"),
    ],
)
def test_config_rejects_unknown_yaml_fields(
    tmp_path,
    unknown_data,
    unknown_field,
):
    data = config_data()
    data.update(unknown_data)
    path = tmp_path / "config.yaml"
    write_config(path, data)

    with pytest.raises(ValueError, match=unknown_field):
        Config.from_yaml(path)


def test_config_models_supply_yaml_defaults(tmp_path):
    path = tmp_path / "config.yaml"
    write_config(path, config_data())

    config = Config.from_yaml(path)

    assert config.llm.retry.model_dump() == {
        "enabled": True,
        "max_retries": 3,
        "initial_delay": 1.0,
        "max_delay": 60.0,
        "exponential_base": 2.0,
    }
    assert config.agent.model_dump() == {
        "max_steps": 50,
        "workspace_dir": "./workspace",
        "system_prompt_path": "system_prompt.md",
    }
    assert config.tools.model_dump() == {
        "enable_file_tools": True,
        "enable_bash": True,
        "enable_note": True,
        "enable_skills": True,
        "skills_dir": "./skills",
        "enable_mcp": True,
        "mcp_config_path": "mcp.json",
        "mcp": {
            "connect_timeout": 10.0,
            "execute_timeout": 60.0,
            "sse_read_timeout": 120.0,
        },
    }


def test_config_preserves_all_explicit_fields(tmp_path):
    data = config_data()
    data.update(
        retry={
            "enabled": False,
            "max_retries": 7,
            "initial_delay": 0.25,
            "max_delay": 8.0,
            "exponential_base": 3.0,
        },
        max_steps=12,
        workspace_dir="./different-workspace",
        system_prompt_path="different-prompt.md",
        tools={
            "enable_file_tools": False,
            "enable_bash": False,
            "enable_note": False,
            "enable_skills": False,
            "skills_dir": "./different-skills",
            "enable_mcp": False,
            "mcp_config_path": "different-mcp.json",
            "mcp": {
                "connect_timeout": 1.5,
                "execute_timeout": 2.5,
                "sse_read_timeout": 3.5,
            },
        },
    )
    path = tmp_path / "config.yaml"
    write_config(path, data)

    config = Config.from_yaml(path)

    assert config.llm.model_dump() == {
        **config_data(),
        "adapter": AdapterName.ANTHROPIC,
        "retry": data["retry"],
    }
    assert config.agent.model_dump() == {
        "max_steps": data["max_steps"],
        "workspace_dir": data["workspace_dir"],
        "system_prompt_path": data["system_prompt_path"],
    }
    assert config.tools.model_dump() == data["tools"]


def test_config_rejects_removed_local_compaction_field(tmp_path):
    data = config_data()
    data["local_compaction_token_limit"] = 123
    path = tmp_path / "config.yaml"
    write_config(path, data)

    with pytest.raises(ValueError, match="removed.*compaction"):
        Config.from_yaml(path)


def test_config_rejects_nonpositive_output_limit(tmp_path):
    data = config_data()
    data["max_output_tokens"] = 0
    path = tmp_path / "config.yaml"
    write_config(path, data)

    with pytest.raises(ValueError, match="max_output_tokens"):
        Config.from_yaml(path)


@pytest.mark.parametrize("max_steps", [0, -1])
def test_config_rejects_nonpositive_max_steps(tmp_path, max_steps):
    data = config_data()
    data["max_steps"] = max_steps
    path = tmp_path / "config.yaml"
    write_config(path, data)

    with pytest.raises(ValueError, match="max_steps"):
        Config.from_yaml(path)


def test_config_rejects_negative_max_retries(tmp_path):
    data = config_data()
    data["retry"] = {"max_retries": -1}
    path = tmp_path / "config.yaml"
    write_config(path, data)

    with pytest.raises(ValueError, match="max_retries"):
        Config.from_yaml(path)


def test_config_accepts_zero_max_retries(tmp_path):
    data = config_data()
    data["retry"] = {"max_retries": 0}
    path = tmp_path / "config.yaml"
    write_config(path, data)

    assert Config.from_yaml(path).llm.retry.max_retries == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("initial_delay", -1),
        ("max_delay", -1),
        ("exponential_base", 0),
        ("exponential_base", -1),
        ("initial_delay", float("nan")),
        ("initial_delay", float("inf")),
        ("max_delay", float("nan")),
        ("max_delay", float("inf")),
        ("exponential_base", float("nan")),
        ("exponential_base", float("inf")),
    ],
)
def test_config_rejects_invalid_retry_backoff_values(tmp_path, field, value):
    data = config_data()
    data["retry"] = {field: value}
    path = tmp_path / "config.yaml"
    write_config(path, data)

    with pytest.raises(ValueError, match=field):
        Config.from_yaml(path)


def test_config_accepts_finite_retry_backoff_boundaries(tmp_path):
    data = config_data()
    data["retry"] = {
        "initial_delay": 0,
        "max_delay": 0,
        "exponential_base": 0.5,
    }
    path = tmp_path / "config.yaml"
    write_config(path, data)

    retry = Config.from_yaml(path).llm.retry
    assert retry.initial_delay == 0
    assert retry.max_delay == 0
    assert retry.exponential_base == 0.5


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


def test_adapters_do_not_invent_missing_finish_reason():
    anthropic_response = SimpleNamespace(
        content=[],
        stop_reason=None,
        usage=None,
    )
    openai_response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason=None,
                message=SimpleNamespace(content=None, tool_calls=None),
            )
        ],
        usage=None,
    )

    anthropic_result = AnthropicAdapter._parse_response(None, anthropic_response)
    openai_result = OpenAIAdapter._parse_response(None, openai_response)

    assert anthropic_result.finish_reason is None
    assert openai_result.finish_reason is None
