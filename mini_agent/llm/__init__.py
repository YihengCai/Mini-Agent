"""Core-facing model contract and configured wire adapters."""

from .base import LLMAdapter
from .factory import AdapterName, create_model_client
from .protocol import ModelClient, ToolDefinition

__all__ = [
    "AdapterName",
    "LLMAdapter",
    "ModelClient",
    "ToolDefinition",
    "create_model_client",
]
