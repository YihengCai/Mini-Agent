"""Configuration management module

Provides unified configuration loading and management functionality
"""

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from .llm.factory import AdapterName


def resolve_config_companion(
    config_path: str | Path,
    configured_path: str | Path,
) -> Path:
    """Resolve one companion path from the selected main configuration."""

    candidate = Path(configured_path)
    if candidate.is_absolute():
        return candidate
    return Path(config_path).parent / candidate


class _StrictConfigModel(BaseModel):
    """Base model for configuration fields that must not be ignored."""

    model_config = ConfigDict(extra="forbid")


class LLMConfig(_StrictConfigModel):
    """LLM configuration"""

    api_key: str
    adapter: AdapterName
    api_base: str
    model: str
    max_output_tokens: int = Field(gt=0)


class AgentConfig(_StrictConfigModel):
    """Agent configuration"""

    max_steps: int = Field(default=50, gt=0)
    system_prompt_path: str = "system_prompt.md"


class MCPConfig(_StrictConfigModel):
    """MCP (Model Context Protocol) timeout configuration"""

    connect_timeout: float = 10.0  # Connection timeout (seconds)
    execute_timeout: float = 60.0  # Tool execution timeout (seconds)
    sse_read_timeout: float = 120.0  # SSE read timeout (seconds)


class ToolsConfig(_StrictConfigModel):
    """Tools configuration"""

    # Basic tools (file operations, bash)
    enable_file_tools: bool = True
    enable_bash: bool = True

    # Skills
    enable_skills: bool = True
    skills_dir: str = "./skills"

    # MCP tools
    enable_mcp: bool = True
    mcp_config_path: str = "mcp.json"
    mcp: MCPConfig = Field(default_factory=MCPConfig)


class Config(_StrictConfigModel):
    """Main configuration class"""

    llm: LLMConfig
    agent: AgentConfig = Field(default_factory=AgentConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)

    @classmethod
    def from_yaml(cls, config_path: str | Path) -> "Config":
        """Load one YAML document through the runtime configuration model."""
        config_path = Path(config_path)

        if not config_path.exists():
            raise FileNotFoundError(f"Configuration file does not exist: {config_path}")

        data = yaml.safe_load(config_path.read_text(encoding="utf-8"))

        if data is None:
            raise ValueError("Configuration file is empty")
        if not isinstance(data, dict):
            raise ValueError("Configuration root must be a mapping")
        return cls.model_validate(data)

    @staticmethod
    def get_package_dir() -> Path:
        """Return the installed mini_agent package directory."""
        return Path(__file__).parent

    @classmethod
    def find_config_file(cls, filename: str) -> Path | None:
        """Find a config file in development, user, then package order."""
        candidates = (
            Path.cwd() / "mini_agent" / "config" / filename,
            Path.home() / ".mini-agent" / "config" / filename,
            cls.get_package_dir() / "config" / filename,
        )
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return None

    @classmethod
    def get_default_config_path(cls) -> Path:
        """Return the selected config path or the package fallback path."""
        config_path = cls.find_config_file("config.yaml")
        if config_path:
            return config_path
        return cls.get_package_dir() / "config" / "config.yaml"
