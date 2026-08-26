"""Core-facing model contract and configured wire adapters."""

from .factory import AdapterName, create_model_client
from .protocol import ModelClient, ToolDefinition

__all__ = [
    "AdapterName",
    "ModelClient",
    "ToolDefinition",
    "create_model_client",
]
