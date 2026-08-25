"""MCP tool loader with real MCP client integration and timeout handling."""

import asyncio
import json
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

from .base import Tool, ToolResult

# Connection type aliases
ConnectionType = Literal["stdio", "sse", "http", "streamable_http"]


@dataclass(frozen=True)
class MCPTimeoutConfig:
    """Immutable timeout defaults for one MCP runtime."""

    connect_timeout: float = 10.0  # Connection timeout (seconds)
    execute_timeout: float = 60.0  # Tool execution timeout (seconds)
    sse_read_timeout: float = 120.0  # SSE read timeout (seconds)


class MCPTool(Tool):
    """Wrapper for MCP tools with timeout handling."""

    def __init__(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        session: ClientSession,
        execute_timeout: float | None = None,
    ):
        self._name = name
        self._description = description
        self._parameters = parameters
        self._session = session
        self._execute_timeout = (
            execute_timeout
            if execute_timeout is not None
            else MCPTimeoutConfig().execute_timeout
        )

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._parameters

    async def execute(self, **kwargs) -> ToolResult:
        """Execute MCP tool via the session with timeout protection."""
        timeout = self._execute_timeout

        try:
            # Wrap call_tool with timeout
            async with asyncio.timeout(timeout):
                result = await self._session.call_tool(self._name, arguments=kwargs)

            # MCP tool results are a list of content items
            content_parts = []
            for item in result.content:
                if hasattr(item, "text"):
                    content_parts.append(item.text)
                else:
                    content_parts.append(str(item))

            content_str = "\n".join(content_parts)

            is_error = result.isError if hasattr(result, "isError") else False

            return ToolResult(success=not is_error, content=content_str, error=None if not is_error else "Tool returned error")

        except TimeoutError:
            return ToolResult(
                success=False,
                content="",
                error=f"MCP tool execution timed out after {timeout}s. The remote server may be slow or unresponsive.",
            )
        except Exception as e:
            return ToolResult(success=False, content="", error=f"MCP tool execution failed: {str(e)}")


class MCPServerConnection:
    """Manages connection to a single MCP server (STDIO or URL-based) with timeout handling."""

    def __init__(
        self,
        name: str,
        connection_type: ConnectionType = "stdio",
        # STDIO params
        command: str | None = None,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        # URL-based params
        url: str | None = None,
        headers: dict[str, str] | None = None,
        timeout_config: MCPTimeoutConfig | None = None,
        # Timeout overrides (per-server)
        connect_timeout: float | None = None,
        execute_timeout: float | None = None,
        sse_read_timeout: float | None = None,
    ):
        self.name = name
        self.connection_type = connection_type
        # STDIO
        self.command = command
        self.args = args or []
        self.env = env or {}
        # URL-based
        self.url = url
        self.headers = headers or {}
        self._timeout_config = timeout_config or MCPTimeoutConfig()
        # Timeout settings (per-server overrides)
        self.connect_timeout = connect_timeout
        self.execute_timeout = execute_timeout
        self.sse_read_timeout = sse_read_timeout
        # Connection state
        self.session: ClientSession | None = None
        self.exit_stack: AsyncExitStack | None = None
        self.tools: list[MCPTool] = []

    def _get_connect_timeout(self) -> float:
        """Get effective connect timeout."""
        return (
            self.connect_timeout
            if self.connect_timeout is not None
            else self._timeout_config.connect_timeout
        )

    def _get_sse_read_timeout(self) -> float:
        """Get effective SSE read timeout."""
        return (
            self.sse_read_timeout
            if self.sse_read_timeout is not None
            else self._timeout_config.sse_read_timeout
        )

    def _get_execute_timeout(self) -> float:
        """Get effective execute timeout."""
        return (
            self.execute_timeout
            if self.execute_timeout is not None
            else self._timeout_config.execute_timeout
        )

    async def connect(self) -> bool:
        """Connect to the MCP server with timeout protection."""
        connect_timeout = self._get_connect_timeout()

        try:
            self.exit_stack = AsyncExitStack()

            # Wrap connection with timeout
            async with asyncio.timeout(connect_timeout):
                if self.connection_type == "stdio":
                    read_stream, write_stream = await self._connect_stdio()
                elif self.connection_type == "sse":
                    read_stream, write_stream = await self._connect_sse()
                else:  # http / streamable_http
                    read_stream, write_stream = await self._connect_streamable_http()

                # Enter client session context
                session = await self.exit_stack.enter_async_context(ClientSession(read_stream, write_stream))
                self.session = session

                # Initialize the session
                await session.initialize()

                # List available tools
                tools_list = await session.list_tools()

            # Wrap each tool with execute timeout
            execute_timeout = self._get_execute_timeout()
            for tool in tools_list.tools:
                parameters = tool.inputSchema if hasattr(tool, "inputSchema") else {}
                mcp_tool = MCPTool(
                    name=tool.name,
                    description=tool.description or "",
                    parameters=parameters,
                    session=session,
                    execute_timeout=execute_timeout,
                )
                self.tools.append(mcp_tool)

            conn_info = self.url if self.url else self.command
            print(f"✓ Connected to MCP server '{self.name}' ({self.connection_type}: {conn_info}) - loaded {len(self.tools)} tools")
            for tool in self.tools:
                desc = tool.description[:60] if len(tool.description) > 60 else tool.description
                print(f"  - {tool.name}: {desc}...")
            return True

        except TimeoutError:
            print(f"✗ Connection to MCP server '{self.name}' timed out after {connect_timeout}s")
            if self.exit_stack:
                await self.disconnect()
            return False

        except Exception as e:
            print(f"✗ Failed to connect to MCP server '{self.name}': {e}")
            if self.exit_stack:
                await self.disconnect()
            import traceback

            traceback.print_exc()
            return False

    async def _connect_stdio(self):
        """Connect via STDIO transport."""
        server_params = StdioServerParameters(command=self.command, args=self.args, env=self.env if self.env else None)
        return await self.exit_stack.enter_async_context(stdio_client(server_params))

    async def _connect_sse(self):
        """Connect via SSE transport with timeout parameters."""
        connect_timeout = self._get_connect_timeout()
        sse_read_timeout = self._get_sse_read_timeout()

        return await self.exit_stack.enter_async_context(
            sse_client(
                url=self.url,
                headers=self.headers if self.headers else None,
                timeout=connect_timeout,
                sse_read_timeout=sse_read_timeout,
            )
        )

    async def _connect_streamable_http(self):
        """Connect via Streamable HTTP transport with timeout parameters."""
        connect_timeout = self._get_connect_timeout()
        sse_read_timeout = self._get_sse_read_timeout()

        # streamablehttp_client returns (read, write, get_session_id)
        read_stream, write_stream, _ = await self.exit_stack.enter_async_context(
            streamablehttp_client(
                url=self.url,
                headers=self.headers if self.headers else None,
                timeout=connect_timeout,
                sse_read_timeout=sse_read_timeout,
            )
        )
        return read_stream, write_stream

    async def disconnect(self):
        """Properly disconnect from the MCP server."""
        if self.exit_stack:
            await self.exit_stack.aclose()
            self.exit_stack = None
            self.session = None


def _determine_connection_type(server_config: dict) -> ConnectionType:
    """Determine connection type from server config."""
    explicit_type = server_config.get("type", "").lower()
    if explicit_type in ("stdio", "sse", "http", "streamable_http"):
        return explicit_type
    # Auto-detect: if url exists, default to streamable_http; otherwise stdio
    if server_config.get("url"):
        return "streamable_http"
    return "stdio"


def _resolve_mcp_config_path(config_path: str) -> Path | None:
    """
    Resolve MCP config path with fallback logic.

    Priority:
    1. If the specified path exists, use it
    2. If mcp.json doesn't exist, try mcp-example.json in the same directory
    3. Return None if no config found

    Args:
        config_path: User-specified config path

    Returns:
        Resolved Path object or None if not found
    """
    config_file = Path(config_path)

    # If specified path exists, use it directly
    if config_file.exists():
        return config_file

    # Fallback: if looking for mcp.json, try mcp-example.json
    if config_file.name == "mcp.json":
        example_file = config_file.parent / "mcp-example.json"
        if example_file.exists():
            print(f"mcp.json not found, using template: {example_file}")
            return example_file

    return None


class MCPManager:
    """Own MCP timeout defaults and connections for one runtime."""

    def __init__(
        self,
        timeout_config: MCPTimeoutConfig | None = None,
    ) -> None:
        self.timeout_config = timeout_config or MCPTimeoutConfig()
        self._connections: list[MCPServerConnection] = []
        self._closed = False
        self._lifecycle_lock = asyncio.Lock()

    async def load_tools(self, config_path: str = "mcp.json") -> list[Tool]:
        """Load tools and retain every live connection until runtime close."""

        async with self._lifecycle_lock:
            if self._closed:
                raise RuntimeError("MCP manager is closed")
            return await self._load_tools(config_path)

    async def _load_tools(self, config_path: str) -> list[Tool]:
        config_file = _resolve_mcp_config_path(config_path)

        if config_file is None:
            print(f"MCP config not found: {config_path}")
            return []

        try:
            with open(config_file, encoding="utf-8") as f:
                config = json.load(f)

            mcp_servers = config.get("mcpServers", {})
            if not mcp_servers:
                print("No MCP servers configured")
                return []

            all_tools = []
            for server_name, server_config in mcp_servers.items():
                if server_config.get("disabled", False):
                    print(f"Skipping disabled server: {server_name}")
                    continue

                conn_type = _determine_connection_type(server_config)
                url = server_config.get("url")
                command = server_config.get("command")

                if conn_type == "stdio" and not command:
                    print(f"No command specified for STDIO server: {server_name}")
                    continue
                if conn_type in ("sse", "http", "streamable_http") and not url:
                    print(
                        f"No url specified for {conn_type.upper()} server: "
                        f"{server_name}"
                    )
                    continue

                connection = MCPServerConnection(
                    name=server_name,
                    connection_type=conn_type,
                    command=command,
                    args=server_config.get("args", []),
                    env=server_config.get("env", {}),
                    url=url,
                    headers=server_config.get("headers", {}),
                    timeout_config=self.timeout_config,
                    connect_timeout=server_config.get("connect_timeout"),
                    execute_timeout=server_config.get("execute_timeout"),
                    sse_read_timeout=server_config.get("sse_read_timeout"),
                )
                # Register before the first await so cancellation cannot orphan a
                # partially entered AsyncExitStack.
                self._connections.append(connection)
                success = await connection.connect()

                if success:
                    all_tools.extend(connection.tools)
                else:
                    self._connections.remove(connection)

            print(f"\nTotal MCP tools loaded: {len(all_tools)}")
            return all_tools

        except Exception as e:
            print(f"Error loading MCP config: {e}")
            import traceback

            traceback.print_exc()
            return []

    async def close(self) -> None:
        """Seal this runtime and serially disconnect every owned connection."""

        async with self._lifecycle_lock:
            self._closed = True
            failures: list[BaseException] = []
            remaining: list[MCPServerConnection] = []

            for connection in self._connections:
                try:
                    await connection.disconnect()
                except BaseException as error:
                    failures.append(error)
                    remaining.append(connection)

            self._connections = remaining
            if failures:
                raise failures[0]
