"""Offline ownership tests for MCP runtimes."""

import asyncio
import json
from dataclasses import FrozenInstanceError

import pytest

import mini_agent.tools.mcp_loader as mcp_module
from mini_agent.tools.mcp_loader import (
    MCPManager,
    MCPServerConnection,
    MCPTimeoutConfig,
)


def write_server_config(path, name: str, **overrides) -> None:
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    name: {
                        "command": "offline-server",
                        **overrides,
                    }
                }
            }
        ),
        encoding="utf-8",
    )


def test_connections_snapshot_runtime_timeouts_and_prefer_server_overrides() -> None:
    first_defaults = MCPTimeoutConfig(
        connect_timeout=11.0,
        execute_timeout=12.0,
        sse_read_timeout=13.0,
    )
    second_defaults = MCPTimeoutConfig(
        connect_timeout=21.0,
        execute_timeout=22.0,
        sse_read_timeout=23.0,
    )
    first = MCPServerConnection(
        name="first",
        timeout_config=first_defaults,
        connect_timeout=0.0,
    )
    second = MCPServerConnection(name="second", timeout_config=second_defaults)

    assert first._get_connect_timeout() == 0.0
    assert first._get_execute_timeout() == 12.0
    assert first._get_sse_read_timeout() == 13.0
    assert second._get_connect_timeout() == 21.0
    assert second._get_execute_timeout() == 22.0
    assert second._get_sse_read_timeout() == 23.0
    with pytest.raises(FrozenInstanceError):
        first_defaults.connect_timeout = 99.0


@pytest.mark.asyncio
async def test_managers_isolate_connections_and_close_idempotently(
    monkeypatch,
    tmp_path,
) -> None:
    connections = []

    class FakeConnection:
        def __init__(self, **kwargs) -> None:
            self.name = kwargs["name"]
            self.timeout_config = kwargs["timeout_config"]
            self.connect_timeout = kwargs["connect_timeout"]
            self.tools = [f"tool:{self.name}"]
            self.disconnect_calls = 0
            connections.append(self)

        async def connect(self) -> bool:
            return True

        async def disconnect(self) -> None:
            self.disconnect_calls += 1

    monkeypatch.setattr(mcp_module, "MCPServerConnection", FakeConnection)
    first_path = tmp_path / "first.json"
    second_path = tmp_path / "second.json"
    write_server_config(first_path, "first", connect_timeout=5.0)
    write_server_config(second_path, "second")
    first = MCPManager(
        MCPTimeoutConfig(
            connect_timeout=11.0,
            execute_timeout=12.0,
            sse_read_timeout=13.0,
        )
    )
    second = MCPManager(
        MCPTimeoutConfig(
            connect_timeout=21.0,
            execute_timeout=22.0,
            sse_read_timeout=23.0,
        )
    )

    assert await first.load_tools(str(first_path)) == ["tool:first"]
    assert await second.load_tools(str(second_path)) == ["tool:second"]
    assert connections[0].timeout_config.connect_timeout == 11.0
    assert connections[0].connect_timeout == 5.0
    assert connections[1].timeout_config.connect_timeout == 21.0

    await first.close()
    await first.close()

    assert connections[0].disconnect_calls == 1
    assert connections[1].disconnect_calls == 0
    await second.close()
    assert connections[1].disconnect_calls == 1


@pytest.mark.asyncio
async def test_cancelled_connection_remains_owned_until_runtime_close(
    monkeypatch,
    tmp_path,
) -> None:
    entered = asyncio.Event()
    instances = []

    class BlockingConnection:
        def __init__(self, **_kwargs) -> None:
            self.tools = []
            self.disconnect_calls = 0
            instances.append(self)

        async def connect(self) -> bool:
            entered.set()
            await asyncio.Future()

        async def disconnect(self) -> None:
            self.disconnect_calls += 1

    monkeypatch.setattr(mcp_module, "MCPServerConnection", BlockingConnection)
    config_path = tmp_path / "cancel.json"
    write_server_config(config_path, "cancelled")
    manager = MCPManager()

    loading = asyncio.create_task(manager.load_tools(str(config_path)))
    await asyncio.wait_for(entered.wait(), timeout=1)
    loading.cancel()
    with pytest.raises(asyncio.CancelledError):
        await loading

    assert instances[0].disconnect_calls == 0
    await manager.close()
    assert instances[0].disconnect_calls == 1


@pytest.mark.asyncio
async def test_cancelled_disconnect_retains_transport_for_close_retry(
    monkeypatch,
    tmp_path,
) -> None:
    cancellation = asyncio.CancelledError("cancel MCP close")

    class FlakyExitStack:
        def __init__(self) -> None:
            self.close_calls = 0

        async def aclose(self) -> None:
            self.close_calls += 1
            if self.close_calls == 1:
                raise cancellation

    connection = MCPServerConnection(name="retry-close")
    exit_stack = FlakyExitStack()
    session = object()
    connection.exit_stack = exit_stack
    connection.session = session
    connection.tools = []

    async def already_connected() -> bool:
        return True

    connection.connect = already_connected
    monkeypatch.setattr(
        mcp_module,
        "MCPServerConnection",
        lambda **_kwargs: connection,
    )
    config_path = tmp_path / "retry.json"
    write_server_config(config_path, "retry-close")
    manager = MCPManager()
    await manager.load_tools(str(config_path))

    with pytest.raises(asyncio.CancelledError) as raised:
        await manager.close()

    assert raised.value is cancellation
    assert connection.exit_stack is exit_stack
    assert connection.session is session
    await manager.close()
    assert exit_stack.close_calls == 2
    assert connection.exit_stack is None
    assert connection.session is None


@pytest.mark.asyncio
async def test_close_seals_loading_and_serializes_concurrent_callers(
    monkeypatch,
    tmp_path,
) -> None:
    disconnect_entered = asyncio.Event()
    release_disconnect = asyncio.Event()
    instances = []

    class BlockingDisconnectConnection:
        def __init__(self, **_kwargs) -> None:
            self.tools = []
            self.disconnect_calls = 0
            instances.append(self)

        async def connect(self) -> bool:
            return True

        async def disconnect(self) -> None:
            self.disconnect_calls += 1
            disconnect_entered.set()
            await release_disconnect.wait()

    monkeypatch.setattr(
        mcp_module,
        "MCPServerConnection",
        BlockingDisconnectConnection,
    )
    config_path = tmp_path / "close.json"
    write_server_config(config_path, "close")
    manager = MCPManager()
    await manager.load_tools(str(config_path))

    first_close = asyncio.create_task(manager.close())
    await asyncio.wait_for(disconnect_entered.wait(), timeout=1)
    second_close = asyncio.create_task(manager.close())
    await asyncio.sleep(0)
    release_disconnect.set()
    await asyncio.gather(first_close, second_close)

    assert instances[0].disconnect_calls == 1
    with pytest.raises(RuntimeError, match="closed"):
        await manager.load_tools(str(config_path))


@pytest.mark.asyncio
async def test_close_attempts_all_connections_and_retries_only_failures(
    monkeypatch,
    tmp_path,
) -> None:
    disconnect_error = OSError("first disconnect failed")
    connections = {}

    class RetryableConnection:
        def __init__(self, **kwargs) -> None:
            self.name = kwargs["name"]
            self.tools = []
            self.disconnect_calls = 0
            connections[self.name] = self

        async def connect(self) -> bool:
            return True

        async def disconnect(self) -> None:
            self.disconnect_calls += 1
            if self.name == "first" and self.disconnect_calls == 1:
                raise disconnect_error

    monkeypatch.setattr(mcp_module, "MCPServerConnection", RetryableConnection)
    config_path = tmp_path / "two-servers.json"
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "first": {"command": "offline-first"},
                    "second": {"command": "offline-second"},
                }
            }
        ),
        encoding="utf-8",
    )
    manager = MCPManager()
    await manager.load_tools(str(config_path))

    with pytest.raises(OSError) as raised:
        await manager.close()

    assert raised.value is disconnect_error
    assert connections["first"].disconnect_calls == 1
    assert connections["second"].disconnect_calls == 1
    await manager.close()
    assert connections["first"].disconnect_calls == 2
    assert connections["second"].disconnect_calls == 1
