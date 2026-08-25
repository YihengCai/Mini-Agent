"""Test cases for MCP tool loading and Git-based MCP servers."""

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

import mini_agent.tools.mcp_loader as mcp_module
from mini_agent.tools.mcp_loader import (
    MCPManager,
    MCPServerConnection,
    MCPTimeoutConfig,
    _determine_connection_type,
)


# =============================================================================
# Connection Type Detection Tests
# =============================================================================


class TestDetermineConnectionType:
    """Tests for _determine_connection_type function."""

    def test_stdio_with_command_only(self):
        """STDIO is default when only command is specified."""
        config = {"command": "npx", "args": ["-y", "some-server"]}
        assert _determine_connection_type(config) == "stdio"

    def test_stdio_explicit_type(self):
        """Explicit type=stdio should return stdio."""
        config = {"command": "npx", "type": "stdio"}
        assert _determine_connection_type(config) == "stdio"

    def test_url_defaults_to_streamable_http(self):
        """URL without explicit type should default to streamable_http."""
        config = {"url": "https://mcp.example.com/mcp"}
        assert _determine_connection_type(config) == "streamable_http"

    def test_sse_explicit_type(self):
        """Explicit type=sse should return sse."""
        config = {"url": "https://mcp.example.com/sse", "type": "sse"}
        assert _determine_connection_type(config) == "sse"

    def test_http_explicit_type(self):
        """Explicit type=http should return http."""
        config = {"url": "https://mcp.example.com/http", "type": "http"}
        assert _determine_connection_type(config) == "http"

    def test_streamable_http_explicit_type(self):
        """Explicit type=streamable_http should return streamable_http."""
        config = {"url": "https://mcp.example.com/mcp", "type": "streamable_http"}
        assert _determine_connection_type(config) == "streamable_http"

    def test_case_insensitive_type(self):
        """Type should be case insensitive."""
        config = {"url": "https://mcp.example.com/sse", "type": "SSE"}
        assert _determine_connection_type(config) == "sse"

    def test_empty_config_defaults_to_stdio(self):
        """Empty config should default to stdio."""
        config = {}
        assert _determine_connection_type(config) == "stdio"

    @pytest.mark.parametrize("explicit_type", ["unknown", "", None, 1])
    def test_explicit_invalid_type_is_rejected(self, explicit_type):
        """Only a missing type may use transport inference."""
        config = {
            "url": "https://mcp.example.com/mcp",
            "type": explicit_type,
        }

        with pytest.raises(ValueError, match="connection type"):
            _determine_connection_type(config)


# =============================================================================
# MCPServerConnection Initialization Tests
# =============================================================================


class TestMCPServerConnectionInit:
    """Tests for MCPServerConnection initialization."""

    def test_stdio_connection_init(self):
        """Test STDIO connection initialization."""
        conn = MCPServerConnection(
            name="test-stdio",
            connection_type="stdio",
            command="npx",
            args=["-y", "test-server"],
            env={"API_KEY": "test"},
        )
        assert conn.name == "test-stdio"
        assert conn.connection_type == "stdio"
        assert conn.command == "npx"
        assert conn.args == ["-y", "test-server"]
        assert conn.env == {"API_KEY": "test"}
        assert conn.url is None

    def test_url_connection_init(self):
        """Test URL-based connection initialization."""
        conn = MCPServerConnection(
            name="test-url",
            connection_type="streamable_http",
            url="https://mcp.example.com/mcp",
            headers={"Authorization": "Bearer token"},
        )
        assert conn.name == "test-url"
        assert conn.connection_type == "streamable_http"
        assert conn.url == "https://mcp.example.com/mcp"
        assert conn.headers == {"Authorization": "Bearer token"}
        assert conn.command is None

    def test_sse_connection_init(self):
        """Test SSE connection initialization."""
        conn = MCPServerConnection(
            name="test-sse",
            connection_type="sse",
            url="https://mcp.example.com/sse",
        )
        assert conn.name == "test-sse"
        assert conn.connection_type == "sse"
        assert conn.url == "https://mcp.example.com/sse"

    def test_default_values(self):
        """Test default values for optional parameters."""
        conn = MCPServerConnection(name="test-default")
        assert conn.connection_type == "stdio"
        assert conn.args == []
        assert conn.env == {}
        assert conn.headers == {}

    def test_invalid_connection_type_is_rejected(self):
        with pytest.raises(ValueError, match="websocket"):
            MCPServerConnection(
                name="invalid",
                connection_type="websocket",
                url="https://mcp.example.com",
            )

    def test_timeout_overrides(self):
        """Test per-server timeout override initialization."""
        conn = MCPServerConnection(
            name="test-timeout",
            connection_type="sse",
            url="https://mcp.example.com/sse",
            connect_timeout=15.0,
            execute_timeout=90.0,
            sse_read_timeout=180.0,
        )
        assert conn.connect_timeout == 15.0
        assert conn.execute_timeout == 90.0
        assert conn.sse_read_timeout == 180.0


# =============================================================================
# Timeout Configuration Tests
# =============================================================================


class TestMCPTimeoutConfig:
    """Tests for MCP timeout configuration."""

    def test_default_timeout_config(self):
        """Test default timeout configuration values."""
        config = MCPTimeoutConfig()
        assert config.connect_timeout == 10.0
        assert config.execute_timeout == 60.0
        assert config.sse_read_timeout == 120.0

    def test_custom_timeout_config(self):
        """Test custom timeout configuration values."""
        config = MCPTimeoutConfig(
            connect_timeout=5.0,
            execute_timeout=30.0,
            sse_read_timeout=60.0,
        )
        assert config.connect_timeout == 5.0
        assert config.execute_timeout == 30.0
        assert config.sse_read_timeout == 60.0

class TestMCPServerConnectionTimeout:
    """Tests for MCPServerConnection timeout behavior."""

    def test_get_effective_connect_timeout_with_override(self):
        """Test getting effective connect timeout with per-server override."""
        conn = MCPServerConnection(
            name="test",
            connection_type="sse",
            url="https://example.com",
            connect_timeout=20.0,
        )
        assert conn._get_connect_timeout() == 20.0

    def test_get_effective_connect_timeout_without_override(self):
        """Test getting effective connect timeout using global default."""
        conn = MCPServerConnection(
            name="test",
            connection_type="sse",
            url="https://example.com",
        )
        assert conn._get_connect_timeout() == MCPTimeoutConfig().connect_timeout

    def test_get_effective_execute_timeout_with_override(self):
        """Test getting effective execute timeout with per-server override."""
        conn = MCPServerConnection(
            name="test",
            connection_type="sse",
            url="https://example.com",
            execute_timeout=180.0,
        )
        assert conn._get_execute_timeout() == 180.0


# =============================================================================
# URL-based Config Loading Tests
# =============================================================================


@pytest.mark.asyncio
async def test_url_config_validation():
    """Test that URL-based config without url is rejected."""
    manager = MCPManager()
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        config = {
            "mcpServers": {
                "broken-sse": {
                    "type": "sse",
                    # Missing "url" field
                }
            }
        }
        json.dump(config, f)
        f.flush()

        try:
            tools = await manager.load_tools(f.name)
            # Should return empty list (server skipped due to missing url)
            assert tools == []
        finally:
            await manager.close()
            Path(f.name).unlink()


@pytest.mark.asyncio
async def test_stdio_config_validation():
    """Test that STDIO config without command is rejected."""
    manager = MCPManager()
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        config = {
            "mcpServers": {
                "broken-stdio": {
                    "type": "stdio",
                    # Missing "command" field
                }
            }
        }
        json.dump(config, f)
        f.flush()

        try:
            tools = await manager.load_tools(f.name)
            # Should return empty list (server skipped due to missing command)
            assert tools == []
        finally:
            await manager.close()
            Path(f.name).unlink()


@pytest.mark.asyncio
async def test_mixed_config_loading():
    """Test loading config with both STDIO and URL-based servers."""
    manager = MCPManager()
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        config = {
            "mcpServers": {
                "stdio-server": {"command": "npx", "args": ["-y", "nonexistent-server"], "disabled": True},
                "url-server": {"url": "https://mcp.nonexistent.example.com/mcp", "disabled": True},
                "sse-server": {"url": "https://sse.nonexistent.example.com/sse", "type": "sse", "disabled": True},
            }
        }
        json.dump(config, f)
        f.flush()

        try:
            # All servers are disabled, should return empty but not error
            tools = await manager.load_tools(f.name)
            assert tools == []
        finally:
            await manager.close()
            Path(f.name).unlink()


@pytest.mark.asyncio
async def test_invalid_type_skips_only_its_server(
    monkeypatch,
    tmp_path,
    capsys,
):
    config_path = tmp_path / "mcp.json"
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "unsafe-typo": {
                        "type": "stdoi",
                        "command": "local-command",
                        "url": "https://remote.example.com/mcp",
                        "headers": {"Authorization": "secret"},
                    },
                    "valid-local": {
                        "type": "stdio",
                        "command": "local-command",
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    constructed = []

    class RecordingConnection:
        def __init__(self, *, name, connection_type, **_kwargs) -> None:
            constructed.append((name, connection_type))
            self.tools = [f"tool:{name}"]

        async def connect(self) -> bool:
            return True

        async def disconnect(self) -> None:
            pass

    monkeypatch.setattr(mcp_module, "MCPServerConnection", RecordingConnection)
    manager = MCPManager()

    try:
        tools = await manager.load_tools(str(config_path))
    finally:
        await manager.close()

    assert tools == ["tool:valid-local"]
    assert constructed == [("valid-local", "stdio")]
    output = capsys.readouterr().out
    assert "unsafe-typo" in output
    assert "stdoi" in output


@pytest.mark.external
@pytest.mark.asyncio
async def test_mcp_tools_loading():
    """Test loading MCP tools from mcp.json."""
    print("\n=== Testing MCP Tool Loading ===")
    manager = MCPManager()

    try:
        # Load MCP tools
        tools = await manager.load_tools("mini_agent/config/mcp.json")

        print(f"Loaded {len(tools)} MCP tools")

        # Display loaded tools
        if tools:
            for tool in tools:
                desc = tool.description[:60] if len(tool.description) > 60 else tool.description
                print(f"  - {tool.name}: {desc}")

        # Test should pass even if no tools loaded (e.g., no mcp.json or no Node.js)
        assert isinstance(tools, list), "Should return a list of tools"
        print("✅ MCP tools loading test passed")

    finally:
        # Cleanup MCP connections
        await manager.close()


@pytest.mark.external
@pytest.mark.asyncio
async def test_git_mcp_tool_availability():
    """Test Git MCP tool availability."""
    print("\n=== Testing Git MCP Tool Availability ===")
    manager = MCPManager()

    try:
        tools = await manager.load_tools("mini_agent/config/mcp.json")

        if not tools:
            pytest.skip("No MCP tools loaded")
            return

        # Find search tool
        search_tool = None
        for tool in tools:
            if "search" in tool.name.lower():
                search_tool = tool
                break

        assert search_tool is not None, "Should contain search-related tools"
        print(f"✅ Found search tool: {search_tool.name}")

    finally:
        await manager.close()


@pytest.mark.external
@pytest.mark.asyncio
async def test_mcp_tool_execution():
    """Test executing an MCP tool if available (memory server)."""
    print("\n=== Testing MCP Tool Execution ===")
    manager = MCPManager()

    try:
        tools = await manager.load_tools("mini_agent/config/mcp.json")

        if not tools:
            print("⚠️  No MCP tools loaded, skipping execution test")
            pytest.skip("No MCP tools available")
            return

        # Try to find and test create_entities (from memory server)
        create_tool = None
        for tool in tools:
            if tool.name == "create_entities":
                create_tool = tool
                break

        if create_tool:
            print(f"Testing: {create_tool.name}")
            try:
                result = await create_tool.execute(
                    entities=[
                        {
                            "name": "test_entity",
                            "entityType": "test",
                            "observations": ["Test observation for pytest"],
                        }
                    ]
                )
                assert result.success, f"Tool execution should succeed: {result.error}"
                print(f"✅ Tool execution successful: {result.content[:100]}")
            except Exception as e:
                pytest.fail(f"Tool execution failed: {e}")
        else:
            print("⚠️  create_entities tool not found, skipping execution test")
            pytest.skip("create_entities tool not available")

    finally:
        await manager.close()


@pytest.mark.external
@pytest.mark.asyncio
async def test_connection_timeout_on_unreachable_server():
    """Test that connection to unreachable server times out properly."""
    print("\n=== Testing Connection Timeout ===")

    conn = None
    try:
        conn = MCPServerConnection(
            name="unreachable-test",
            connection_type="streamable_http",
            url="https://10.255.255.1:9999/mcp",  # Non-routable IP, will timeout
            timeout_config=MCPTimeoutConfig(connect_timeout=2.0),
        )

        import time

        start = time.time()
        success = await conn.connect()
        elapsed = time.time() - start

        assert success is False, "Connection to unreachable server should fail"
        # Should timeout within reasonable time (connect_timeout + some overhead)
        assert elapsed < 10.0, f"Should timeout quickly, but took {elapsed:.1f}s"
        print(f"✅ Connection timed out as expected in {elapsed:.1f}s")

    finally:
        if conn is not None:
            await conn.disconnect()


@pytest.mark.external
@pytest.mark.asyncio
async def test_per_server_timeout_override_in_config():
    """Test that per-server timeout overrides from config are respected."""
    print("\n=== Testing Per-Server Timeout Override ===")
    manager = MCPManager()

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        config = {
            "mcpServers": {
                "fast-server": {
                    "url": "https://10.255.255.1:9999/mcp",
                    "connect_timeout": 1.0,  # Very short timeout
                    "execute_timeout": 30.0,
                }
            }
        }
        json.dump(config, f)
        f.flush()

        try:
            import time

            start = time.time()
            tools = await manager.load_tools(f.name)
            elapsed = time.time() - start

            # Should fail due to unreachable server
            assert tools == []
            # Should respect the short 1.0s connect_timeout
            assert elapsed < 5.0, f"Should use per-server timeout, but took {elapsed:.1f}s"
            print(f"✅ Per-server timeout override worked, failed in {elapsed:.1f}s")

        finally:
            await manager.close()
            Path(f.name).unlink()


async def main():
    """Run all MCP tests."""
    print("=" * 80)
    print("Running MCP Integration Tests")
    print("=" * 80)
    print("\nNote: These tests require Node.js and will use MCP servers defined in mcp.json")
    print("Tests will pass even if MCP is not configured.\n")

    await test_mcp_tools_loading()
    await test_mcp_tool_execution()
    await test_connection_timeout_on_unreachable_server()

    print("\n" + "=" * 80)
    print("MCP tests completed! ✅")
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(main())
