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


class RetryConfig(_StrictConfigModel):
    """Retry configuration"""

    enabled: bool = True
    max_retries: int = Field(default=3, ge=0)
    initial_delay: float = 1.0
    max_delay: float = 60.0
    exponential_base: float = 2.0


class LLMConfig(_StrictConfigModel):
    """LLM configuration"""

    api_key: str
    adapter: AdapterName
    api_base: str
    model: str
    max_output_tokens: int = Field(gt=0)
    retry: RetryConfig = Field(default_factory=RetryConfig)


class AgentConfig(_StrictConfigModel):
    """Agent configuration"""

    max_steps: int = Field(default=50, gt=0)
    workspace_dir: str = "./workspace"
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
    enable_note: bool = True

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
    agent: AgentConfig
    tools: ToolsConfig

    @classmethod
    def load(cls) -> "Config":
        """Load configuration from the default search path."""
        config_path = cls.get_default_config_path()
        if not config_path.exists():
            raise FileNotFoundError("Configuration file not found. Run scripts/setup-config.sh or place config.yaml in mini_agent/config/.")
        return cls.from_yaml(config_path)

    @classmethod
    def from_yaml(cls, config_path: str | Path) -> "Config":
        """Load configuration from YAML file

        Args:
            config_path: Configuration file path

        Returns:
            Config instance

        Raises:
            FileNotFoundError: Configuration file does not exist
            ValueError: Invalid configuration format or missing required fields
        """
        config_path = Path(config_path)

        if not config_path.exists():
            raise FileNotFoundError(f"Configuration file does not exist: {config_path}")

        with open(config_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)

        if not data:
            raise ValueError("Configuration file is empty")
        if not isinstance(data, dict):
            raise ValueError("Configuration root must be a mapping")

        if "provider" in data:
            raise ValueError(
                "Configuration field 'provider' was replaced by 'adapter'; "
                "remove 'provider' and configure 'adapter' explicitly"
            )
        if "local_compaction_token_limit" in data:
            raise ValueError(
                "Configuration field 'local_compaction_token_limit' was removed; "
                "automatic local compaction is no longer available"
            )

        known_root_fields = {
            *LLMConfig.model_fields,
            *AgentConfig.model_fields,
            "tools",
        }
        unknown_fields = sorted(
            str(field) for field in set(data) - known_root_fields
        )
        if unknown_fields:
            raise ValueError(
                "Configuration file has unknown field(s): "
                + ", ".join(unknown_fields)
            )

        # Model endpoint selection is explicit; no vendor defaults are inferred.
        required_llm_fields = tuple(
            field_name
            for field_name, field in LLMConfig.model_fields.items()
            if field.is_required()
        )
        missing_fields = [field for field in required_llm_fields if field not in data]
        if missing_fields:
            raise ValueError(
                "Configuration file missing required field(s): "
                + ", ".join(missing_fields)
            )

        if not data["api_key"] or data["api_key"] == "YOUR_API_KEY_HERE":
            raise ValueError("Please configure a valid API Key")

        llm_data = {
            field: data[field]
            for field in LLMConfig.model_fields
            if field in data
        }
        agent_data = {
            field: data[field]
            for field in AgentConfig.model_fields
            if field in data
        }

        return cls.model_validate(
            {
                "llm": llm_data,
                "agent": agent_data,
                "tools": data.get("tools", {}),
            }
        )

    @staticmethod
    def get_package_dir() -> Path:
        """Get the package installation directory

        Returns:
            Path to the mini_agent package directory
        """
        # Get the directory where this config.py file is located
        return Path(__file__).parent

    @classmethod
    def find_config_file(cls, filename: str) -> Path | None:
        """Find configuration file with priority order

        Search for config file in the following order of priority:
        1) mini_agent/config/{filename} in current directory (development mode)
        2) ~/.mini-agent/config/{filename} in user home directory
        3) {package}/mini_agent/config/{filename} in package installation directory

        Args:
            filename: Configuration file name (e.g., "config.yaml", "mcp.json", "system_prompt.md")

        Returns:
            Path to found config file, or None if not found
        """
        # Priority 1: Development mode - current directory's config/ subdirectory
        dev_config = Path.cwd() / "mini_agent" / "config" / filename
        if dev_config.exists():
            return dev_config

        # Priority 2: User config directory
        user_config = Path.home() / ".mini-agent" / "config" / filename
        if user_config.exists():
            return user_config

        # Priority 3: Package installation directory's config/ subdirectory
        package_config = cls.get_package_dir() / "config" / filename
        if package_config.exists():
            return package_config

        return None

    @classmethod
    def get_default_config_path(cls) -> Path:
        """Get the default config file path with priority search

        Returns:
            Path to config.yaml (prioritizes: dev config/ > user config/ > package config/)
        """
        config_path = cls.find_config_file("config.yaml")
        if config_path:
            return config_path

        # Fallback to package config directory for error message purposes
        return cls.get_package_dir() / "config" / "config.yaml"
