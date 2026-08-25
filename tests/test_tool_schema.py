"""Test cases for Tool schema methods."""

from typing import Any

import pytest

from mini_agent.llm.protocol import ToolDefinition
from mini_agent.tools.base import Tool, ToolResult


class MockWeatherTool(Tool):
    """Mock weather tool for testing."""

    @property
    def name(self) -> str:
        return "get_weather"

    @property
    def description(self) -> str:
        return "Get weather information"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "Location name",
                },
            },
            "required": ["location"],
        }

    async def execute(self, **kwargs) -> ToolResult:
        return ToolResult(success=True, content="Weather data")


class MockCalculatorTool(Tool):
    """Mock calculator tool for testing."""

    @property
    def name(self) -> str:
        return "calculator"

    @property
    def description(self) -> str:
        return "Perform calculations"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "Math expression",
                },
            },
            "required": ["expression"],
        }

    async def execute(self, **kwargs) -> ToolResult:
        return ToolResult(success=True, content="42")


class MockSearchTool(Tool):
    """Mock search tool with complex schema."""

    @property
    def name(self) -> str:
        return "search_database"

    @property
    def description(self) -> str:
        return "Search the database"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query",
                },
                "filters": {
                    "type": "object",
                    "properties": {
                        "category": {"type": "string"},
                        "min_price": {"type": "number"},
                        "max_price": {"type": "number"},
                    },
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 10,
                },
            },
            "required": ["query"],
        }

    async def execute(self, **kwargs) -> ToolResult:
        return ToolResult(success=True, content="Search results")


class MockEnumTool(Tool):
    """Mock tool with enum parameter."""

    @property
    def name(self) -> str:
        return "set_status"

    @property
    def description(self) -> str:
        return "Set status"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["active", "inactive", "pending"],
                    "description": "Status value",
                }
            },
            "required": ["status"],
        }

    async def execute(self, **kwargs) -> ToolResult:
        return ToolResult(success=True, content="Status set")


def definition_for(tool: Tool) -> ToolDefinition:
    return ToolDefinition(
        name=tool.name,
        description=tool.description,
        parameters=tool.parameters,
    )


def test_tool_definition_fields():
    """A tool exposes one vendor-neutral definition."""
    tool = MockWeatherTool()
    definition = definition_for(tool)

    assert definition.name == "get_weather"
    assert definition.description == "Get weather information"
    assert definition.parameters["type"] == "object"
    assert "location" in definition.parameters["properties"]
    assert definition.parameters["required"] == ["location"]


def test_tool_definition_complex():
    """Test tool with complex input schema."""
    definition = definition_for(MockSearchTool())

    assert definition.name == "search_database"
    params = definition.parameters
    assert "query" in params["properties"]
    assert "filters" in params["properties"]
    assert "limit" in params["properties"]
    assert params["required"] == ["query"]


def test_multiple_tools():
    """Test creating multiple tool instances."""
    tool1 = MockWeatherTool()
    tool2 = MockCalculatorTool()

    tools = [tool1, tool2]
    assert len(tools) == 2
    assert tools[0].name == "get_weather"
    assert tools[1].name == "calculator"

    definitions = [definition_for(tool) for tool in tools]
    assert [definition.name for definition in definitions] == [
        "get_weather",
        "calculator",
    ]


def test_tool_with_enum():
    """Test tool with enum parameter."""
    definition = definition_for(MockEnumTool())
    status_prop = definition.parameters["properties"]["status"]
    assert "enum" in status_prop
    assert status_prop["enum"] == ["active", "inactive", "pending"]

def test_tool_definition_keeps_one_parameter_schema():
    """Protocol adapters, not Tool, own wire-specific wrapping."""
    tool = MockCalculatorTool()
    definition = definition_for(tool)

    assert definition.parameters == tool.parameters
    assert "input_schema" not in definition.parameters
    assert "function" not in definition.parameters


@pytest.mark.asyncio
async def test_tool_execute():
    """Test that tools can be executed."""
    tool = MockWeatherTool()
    result = await tool.execute(location="Tokyo")

    assert isinstance(result, ToolResult)
    assert result.success is True
    assert result.content == "Weather data"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
