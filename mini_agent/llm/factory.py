"""Explicit registry for model API adapters."""

from enum import Enum

from .anthropic_client import AnthropicAdapter
from .openai_client import OpenAIAdapter
from .protocol import ModelClient


class AdapterName(str, Enum):
    """Configured wire adapter names."""

    ANTHROPIC = "anthropic"
    OPENAI = "openai"


_ADAPTERS: dict[AdapterName, type[AnthropicAdapter] | type[OpenAIAdapter]] = {
    AdapterName.ANTHROPIC: AnthropicAdapter,
    AdapterName.OPENAI: OpenAIAdapter,
}


def create_model_client(
    *,
    adapter: AdapterName | str,
    api_key: str,
    api_base: str,
    model: str,
    max_output_tokens: int,
) -> ModelClient:
    """Build the configured adapter without inferring vendor behavior."""

    try:
        adapter_name = AdapterName(adapter)
    except ValueError as error:
        supported = ", ".join(item.value for item in AdapterName)
        raise ValueError(
            f"Unsupported model adapter {adapter!r}; expected one of: {supported}"
        ) from error

    adapter_type = _ADAPTERS[adapter_name]
    return adapter_type(
        api_key=api_key,
        api_base=api_base,
        model=model,
        max_output_tokens=max_output_tokens,
    )
