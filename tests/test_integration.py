"""Integration test cases - Full agent demos."""

import asyncio
import tempfile
from pathlib import Path

import pytest

from mini_agent import create_model_client
from mini_agent.agent import AgentSession
from mini_agent.config import Config
from mini_agent.tools import (
    BackgroundShellManager,
    BashTool,
    EditTool,
    ReadTool,
    WriteTool,
)
from mini_agent.tools.mcp_loader import MCPManager, MCPTimeoutConfig


pytestmark = pytest.mark.external


@pytest.mark.asyncio
async def test_basic_agent_usage():
    """Test basic agent usage with file creation task.

    This is the integration test for basic agent functionality,
    converted from example.py.
    """
    print("\n" + "=" * 80)
    print("Integration Test: Basic Agent Usage")
    print("=" * 80)

    # Load configuration
    config_path = Path("mini_agent/config/config.yaml")
    if not config_path.exists():
        pytest.skip("config.yaml not found")

    config = Config.from_yaml(config_path)

    # Check API key
    if not config.llm.api_key or config.llm.api_key == "YOUR_API_KEY_HERE":
        pytest.skip("API key not configured")

    # Use temporary workspace
    with tempfile.TemporaryDirectory() as workspace_dir:
        # Load system prompt (Agent will auto-inject workspace info)
        system_prompt_path = Path("mini_agent/config/system_prompt.md")
        if system_prompt_path.exists():
            system_prompt = system_prompt_path.read_text(encoding="utf-8")
        else:
            system_prompt = "You are a helpful AI assistant."

        # Initialize LLM client
        llm_client = create_model_client(
            api_key=config.llm.api_key,
            adapter=config.llm.adapter,
            api_base=config.llm.api_base,
            model=config.llm.model,
            max_output_tokens=config.llm.max_output_tokens,
        )

        # Initialize basic tools
        shell_manager = BackgroundShellManager()
        mcp_manager = MCPManager(
            MCPTimeoutConfig(
                connect_timeout=config.tools.mcp.connect_timeout,
                execute_timeout=config.tools.mcp.execute_timeout,
                sse_read_timeout=config.tools.mcp.sse_read_timeout,
            )
        )
        try:
            tools = [
                ReadTool(workspace_dir=workspace_dir),
                WriteTool(workspace_dir=workspace_dir),
                EditTool(workspace_dir=workspace_dir),
                BashTool(manager=shell_manager),
            ]

            # Load MCP tools (optional) - with timeout protection
            try:
                # MCP tools are disabled by default to prevent test hangs
                # Enable specific MCP servers in mcp.json if needed
                mcp_tools = await mcp_manager.load_tools(
                    "mini_agent/config/mcp.json"
                )
                if mcp_tools:
                    print(f"✓ Loaded {len(mcp_tools)} MCP tools")
                    tools.extend(mcp_tools)
                else:
                    print("⚠️  No MCP tools configured (mcp.json is empty)")
            except Exception as e:
                print(f"⚠️  MCP tools not loaded: {e}")

            # Create agent
            agent = AgentSession(
                llm_client=llm_client,
                system_prompt=system_prompt,
                tools=tools,
                max_steps=config.agent.max_steps,
            )

            # Task: Create a Python file with hello world
            task = """
            Create a Python file named hello.py in the workspace that prints "Hello, Mini Agent!".
            Then execute it to verify it works.
            """

            print(f"\nTask: {task}")
            print("\n" + "=" * 80 + "\n")
            outcome = await agent.start_turn(task).wait()
        finally:
            try:
                await shell_manager.close()
            finally:
                await mcp_manager.close()
        result = outcome.last_assistant_message or ""

        print("\n" + "=" * 80)
        print(f"Result: {result}")
        print("=" * 80)

        # Verify the file was created or task completed
        hello_file = Path(workspace_dir) / "hello.py"
        assert hello_file.exists() or "complete" in result.lower(), (
            "Agent should create the file or indicate completion"
        )

        print("\n✅ Basic agent usage test passed")

async def main():
    """Run all integration tests."""
    print("=" * 80)
    print("Running Integration Tests")
    print("=" * 80)
    print("\nNote: These tests require a configured model API in config.yaml")
    print("These tests will actually call the LLM API and may take some time.\n")

    try:
        await test_basic_agent_usage()
    except Exception as e:
        print(f"❌ Basic usage test failed: {e}")

    print("\n" + "=" * 80)
    print("Integration tests completed!")
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(main())
