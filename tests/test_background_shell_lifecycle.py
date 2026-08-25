"""Offline ownership and cleanup tests for background shell runtimes."""

import asyncio
from types import SimpleNamespace

import pytest

import mini_agent.cli as cli
import mini_agent.tools.bash_tool as bash_module
from mini_agent.tools.bash_tool import (
    BackgroundShell,
    BackgroundShellManager,
    BashKillTool,
    BashOutputTool,
    BashTool,
)
from mini_agent.tools.mcp_loader import MCPManager


ORIGINAL_WAIT_FOR = asyncio.wait_for


async def wait_for_event(event: asyncio.Event) -> None:
    await ORIGINAL_WAIT_FOR(event.wait(), timeout=1)


class ControlledStream:
    def __init__(self, *, hold_cancel: bool = False) -> None:
        self.blocked = asyncio.Event()
        self.cancel_started = asyncio.Event()
        self.release_cancel = asyncio.Event()
        if not hold_cancel:
            self.release_cancel.set()

    async def readline(self) -> bytes:
        self.blocked.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self.cancel_started.set()
            await self.release_cancel.wait()
            raise


class FakeProcess:
    def __init__(
        self,
        *,
        stdout=None,
        returncode: int | None = None,
        exit_on_terminate: bool = True,
    ) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.exit_on_terminate = exit_on_terminate
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls = 0

    def terminate(self) -> None:
        self.terminate_calls += 1
        if self.exit_on_terminate:
            self.returncode = -15

    def kill(self) -> None:
        self.kill_calls += 1
        self.returncode = -9

    def wait(self):
        self.wait_calls += 1

        async def completed_wait():
            return self.returncode

        return completed_wait()


class TerminateFailingProcess(FakeProcess):
    def __init__(self, error: BaseException) -> None:
        super().__init__()
        self.error = error

    def terminate(self) -> None:
        self.terminate_calls += 1
        raise self.error


def make_shell(
    shell_id: str,
    process: FakeProcess,
) -> BackgroundShell:
    return BackgroundShell(
        bash_id=shell_id,
        command=f"fake:{shell_id}",
        process=process,
        start_time=0.0,
    )


async def begin_tracking(
    manager: BackgroundShellManager,
    shell: BackgroundShell,
) -> None:
    manager.track(shell)
    await asyncio.sleep(0)


def make_cli_config():
    retry = SimpleNamespace(
        enabled=False,
        max_retries=0,
        initial_delay=0,
        max_delay=0,
        exponential_base=1,
    )
    return SimpleNamespace(
        llm=SimpleNamespace(
            api_key="offline",
            adapter="anthropic",
            api_base="http://localhost.invalid",
            model="offline",
            max_output_tokens=1,
            retry=retry,
        ),
        tools=SimpleNamespace(
            mcp=SimpleNamespace(
                connect_timeout=10.0,
                execute_timeout=60.0,
                sse_read_timeout=120.0,
            )
        ),
    )


@pytest.mark.asyncio
async def test_managers_isolate_shell_state() -> None:
    first = BackgroundShellManager()
    second = BackgroundShellManager()
    shell = make_shell("private", FakeProcess(returncode=0))

    try:
        await begin_tracking(first, shell)

        assert first.get("private") is shell
        assert second.get("private") is None
        assert second.get_available_ids() == []
        isolated_result = await BashOutputTool(manager=second).execute(
            bash_id="private",
        )
        assert not isolated_result.success
    finally:
        await first.close()


@pytest.mark.asyncio
async def test_track_rejects_duplicate_ids_without_replacing_the_owner() -> None:
    manager = BackgroundShellManager()
    stream = ControlledStream()
    first = make_shell("duplicate", FakeProcess(stdout=stream))
    second = make_shell("duplicate", FakeProcess(returncode=0))
    original_monitor = None

    try:
        await begin_tracking(manager, first)
        original_monitor = manager._monitor_tasks["duplicate"]
        await wait_for_event(stream.blocked)

        with pytest.raises(ValueError, match="Duplicate shell ID"):
            manager.track(second)

        assert manager.get("duplicate") is first
        assert manager._monitor_tasks["duplicate"] is original_monitor
    finally:
        if original_monitor is not None:
            original_monitor.cancel()
            await asyncio.gather(original_monitor, return_exceptions=True)
        await manager.close()


@pytest.mark.asyncio
async def test_track_rolls_back_when_monitor_creation_fails(monkeypatch) -> None:
    manager = BackgroundShellManager()
    shell = make_shell("rollback", FakeProcess(returncode=0))
    creation_error = RuntimeError("cannot create monitor")

    def fail_to_create_task(_awaitable):
        raise creation_error

    monkeypatch.setattr(bash_module.asyncio, "create_task", fail_to_create_task)

    with pytest.raises(RuntimeError) as raised:
        manager.track(shell)

    assert raised.value is creation_error
    assert manager.get_available_ids() == []
    assert manager._monitor_tasks == {}


@pytest.mark.asyncio
async def test_bash_tool_terminates_a_spawned_process_when_tracking_fails(
    monkeypatch,
) -> None:
    class RejectingManager:
        def track(self, _shell) -> None:
            raise RuntimeError("manager rejected shell")

    process = FakeProcess()

    async def create_process(*_args, **_kwargs):
        return process

    monkeypatch.setattr(
        bash_module.asyncio,
        "create_subprocess_shell",
        create_process,
    )
    tool = BashTool(manager=RejectingManager())
    tool.is_windows = False

    result = await tool.execute("fake command", run_in_background=True)

    assert not result.success
    assert "manager rejected shell" in result.error
    assert process.terminate_calls == 1
    assert process.wait_calls == 1


@pytest.mark.asyncio
async def test_bash_tool_cleans_up_before_propagating_tracking_cancellation(
    monkeypatch,
) -> None:
    cancellation = asyncio.CancelledError("tracking cancelled")

    class CancellingManager:
        def track(self, _shell) -> None:
            raise cancellation

    process = FakeProcess()

    async def create_process(*_args, **_kwargs):
        return process

    monkeypatch.setattr(
        bash_module.asyncio,
        "create_subprocess_shell",
        create_process,
    )
    tool = BashTool(manager=CancellingManager())
    tool.is_windows = False

    with pytest.raises(asyncio.CancelledError) as raised:
        await tool.execute("fake command", run_in_background=True)

    assert raised.value is cancellation
    assert process.terminate_calls == 1
    assert process.wait_calls == 1


@pytest.mark.asyncio
async def test_tracking_error_remains_primary_when_process_cleanup_also_fails(
    monkeypatch,
) -> None:
    tracking_error = RuntimeError("primary tracking failure")
    cleanup_error = OSError("secondary process cleanup failure")

    class RejectingManager:
        def track(self, _shell) -> None:
            raise tracking_error

    process = TerminateFailingProcess(cleanup_error)

    async def create_process(*_args, **_kwargs):
        return process

    monkeypatch.setattr(
        bash_module.asyncio,
        "create_subprocess_shell",
        create_process,
    )
    tool = BashTool(manager=RejectingManager())
    tool.is_windows = False

    result = await tool.execute("fake command", run_in_background=True)

    assert not result.success
    assert "primary tracking failure" in result.error
    assert "secondary process cleanup failure" in result.error
    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert process.wait_calls == 1


@pytest.mark.asyncio
async def test_tracking_cancellation_remains_primary_when_cleanup_also_fails(
    monkeypatch,
) -> None:
    tracking_error = asyncio.CancelledError("primary tracking cancellation")
    cleanup_error = OSError("secondary process cleanup failure")

    class CancellingManager:
        def track(self, _shell) -> None:
            raise tracking_error

    process = TerminateFailingProcess(cleanup_error)

    async def create_process(*_args, **_kwargs):
        return process

    monkeypatch.setattr(
        bash_module.asyncio,
        "create_subprocess_shell",
        create_process,
    )
    tool = BashTool(manager=CancellingManager())
    tool.is_windows = False

    with pytest.raises(asyncio.CancelledError) as raised:
        await tool.execute("fake command", run_in_background=True)

    assert raised.value is tracking_error
    assert raised.value.__cause__ is cleanup_error
    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert process.wait_calls == 1


@pytest.mark.asyncio
async def test_terminate_waits_for_monitor_finally(monkeypatch) -> None:
    async def direct_wait_for(awaitable, timeout):
        return await awaitable

    monkeypatch.setattr(bash_module.asyncio, "wait_for", direct_wait_for)
    manager = BackgroundShellManager()
    stream = ControlledStream(hold_cancel=True)
    process = FakeProcess(stdout=stream)
    shell = make_shell("blocked-monitor", process)

    await begin_tracking(manager, shell)
    monitor_task = manager._monitor_tasks["blocked-monitor"]
    await wait_for_event(stream.blocked)
    terminate_task = asyncio.create_task(manager.terminate("blocked-monitor"))

    try:
        await wait_for_event(stream.cancel_started)
        assert not terminate_task.done()

        stream.release_cancel.set()
        terminated = await terminate_task

        assert terminated is shell
        assert monitor_task.done()
        assert manager.get("blocked-monitor") is None
    finally:
        stream.release_cancel.set()
        if not terminate_task.done():
            terminate_task.cancel()
        if not monitor_task.done():
            monitor_task.cancel()
        await asyncio.gather(
            terminate_task,
            monitor_task,
            return_exceptions=True,
        )
        await manager.close()


@pytest.mark.asyncio
async def test_close_terminates_every_shell_waits_for_monitors_and_is_idempotent() -> None:
    manager = BackgroundShellManager()
    streams = [ControlledStream(), ControlledStream()]
    processes = [FakeProcess(stdout=stream) for stream in streams]
    shell_ids = ["first-close", "second-close"]

    for shell_id, process in zip(shell_ids, processes):
        await begin_tracking(manager, make_shell(shell_id, process))
    monitor_tasks = [manager._monitor_tasks[shell_id] for shell_id in shell_ids]
    for stream in streams:
        await wait_for_event(stream.blocked)

    try:
        await manager.close()

        assert [process.terminate_calls for process in processes] == [1, 1]
        assert all(task.done() for task in monitor_tasks)
        assert manager.get_available_ids() == []
        assert manager._monitor_tasks == {}

        await manager.close()
        assert [process.terminate_calls for process in processes] == [1, 1]
    finally:
        for stream in streams:
            stream.release_cancel.set()
        for task in monitor_tasks:
            task.cancel()
        await asyncio.gather(*monitor_tasks, return_exceptions=True)
        await manager.close()


@pytest.mark.asyncio
async def test_close_seals_registration_and_serializes_concurrent_callers() -> None:
    class GatedShell(BackgroundShell):
        def __init__(self) -> None:
            super().__init__(
                bash_id="gated-close",
                command="fake:gated-close",
                process=FakeProcess(returncode=0),
                start_time=0.0,
            )
            self.terminate_calls = 0
            self.terminate_started = asyncio.Event()
            self.release_terminate = asyncio.Event()

        async def terminate(self) -> None:
            self.terminate_calls += 1
            self.terminate_started.set()
            await self.release_terminate.wait()
            self.status = "terminated"
            self.exit_code = self.process.returncode

    manager = BackgroundShellManager()
    shell = GatedShell()
    await begin_tracking(manager, shell)
    first_close = asyncio.create_task(manager.close())
    second_close = None

    try:
        await wait_for_event(shell.terminate_started)

        with pytest.raises(RuntimeError, match="manager is closed"):
            manager.track(make_shell("during-close", FakeProcess(returncode=0)))

        second_close = asyncio.create_task(manager.close())
        await asyncio.sleep(0)
        assert not second_close.done()
        assert shell.terminate_calls == 1

        shell.release_terminate.set()
        await asyncio.gather(first_close, second_close)

        with pytest.raises(RuntimeError, match="manager is closed"):
            manager.track(make_shell("after-close", FakeProcess(returncode=0)))
        assert shell.terminate_calls == 1
        assert manager.get_available_ids() == []
    finally:
        shell.release_terminate.set()
        tasks = [first_close]
        if second_close is not None:
            tasks.append(second_close)
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_close_attempts_every_shell_before_raising_a_cleanup_error() -> None:
    class FlakyShell(BackgroundShell):
        terminate_calls = 0

        async def terminate(self) -> None:
            self.terminate_calls += 1
            if self.terminate_calls == 1:
                raise OSError("first shell failed to terminate")
            self.status = "terminated"
            self.exit_code = self.process.returncode

    manager = BackgroundShellManager()
    failing_shell = FlakyShell(
        bash_id="failing-close",
        command="fake:failing-close",
        process=FakeProcess(returncode=0),
        start_time=0.0,
    )
    stream = ControlledStream()
    healthy_process = FakeProcess(stdout=stream)
    healthy_shell = make_shell("healthy-close", healthy_process)

    await begin_tracking(manager, failing_shell)
    await begin_tracking(manager, healthy_shell)
    monitor_tasks = list(manager._monitor_tasks.values())
    await wait_for_event(stream.blocked)

    try:
        with pytest.raises(OSError, match="first shell failed to terminate"):
            await manager.close()

        assert healthy_process.terminate_calls == 1
        assert manager.get("healthy-close") is None
        assert manager._monitor_tasks == {}

        with pytest.raises(RuntimeError, match="manager is closed"):
            manager.track(make_shell("new-after-failure", FakeProcess(returncode=0)))

        await manager.close()
        assert failing_shell.terminate_calls == 2
        assert manager.get_available_ids() == []
    finally:
        stream.release_cancel.set()
        for task in monitor_tasks:
            task.cancel()
        await asyncio.gather(*monitor_tasks, return_exceptions=True)
        try:
            await manager.close()
        except OSError:
            pass


@pytest.mark.asyncio
async def test_force_kill_waits_again_and_records_the_exit_code(monkeypatch) -> None:
    async def force_timeout(awaitable, timeout):
        awaitable.close()
        raise asyncio.TimeoutError

    monkeypatch.setattr(bash_module.asyncio, "wait_for", force_timeout)
    process = FakeProcess(exit_on_terminate=False)
    shell = make_shell("force-kill", process)

    await shell.terminate()

    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert process.wait_calls == 2
    assert shell.status == "terminated"
    assert shell.exit_code == -9


@pytest.mark.asyncio
async def test_cli_assembles_all_bash_tools_with_one_runtime_manager(
    tmp_path,
) -> None:
    config = SimpleNamespace(
        tools=SimpleNamespace(
            enable_bash=True,
            enable_skills=False,
            enable_mcp=False,
            enable_file_tools=False,
            enable_note=False,
        )
    )
    manager = BackgroundShellManager()
    mcp_manager = MCPManager()

    tools, skill_loader = await cli.initialize_base_tools(
        config,
        config_path=tmp_path / "config.yaml",
        shell_manager=manager,
        mcp_manager=mcp_manager,
    )
    cli.add_workspace_tools(
        tools,
        config,
        tmp_path,
        shell_manager=manager,
    )

    assert skill_loader is None
    shell_tools = [
        tool
        for tool in tools
        if isinstance(tool, (BashTool, BashOutputTool, BashKillTool))
    ]
    assert len(shell_tools) == 3
    assert all(tool._manager is manager for tool in shell_tools)
    await mcp_manager.close()


@pytest.mark.asyncio
async def test_cli_loads_mcp_tools_through_passed_runtime_manager(
    tmp_path,
) -> None:
    config_path = tmp_path / "config.yaml"
    mcp_config_path = tmp_path / "mcp.json"
    mcp_config_path.write_text("{}", encoding="utf-8")
    mcp_tool = object()
    loaded_paths = []

    class RecordingMCPManager:
        async def load_tools(self, path: str):
            loaded_paths.append(path)
            return [mcp_tool]

    config = SimpleNamespace(
        tools=SimpleNamespace(
            enable_bash=False,
            enable_skills=False,
            enable_mcp=True,
            enable_file_tools=False,
            enable_note=False,
            mcp_config_path="mcp.json",
            mcp=SimpleNamespace(
                connect_timeout=10.0,
                execute_timeout=60.0,
                sse_read_timeout=120.0,
            ),
        )
    )
    shell_manager = BackgroundShellManager()
    mcp_manager = RecordingMCPManager()

    tools, skill_loader = await cli.initialize_base_tools(
        config,
        config_path=config_path,
        shell_manager=shell_manager,
        mcp_manager=mcp_manager,
    )

    assert tools == [mcp_tool]
    assert skill_loader is None
    assert loaded_paths == [str(mcp_config_path)]
    await shell_manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "invalid"])
async def test_invalid_config_returns_before_runtime_resources_exist(
    monkeypatch,
    tmp_path,
    failure,
) -> None:
    shell_manager_creations = 0
    shell_manager_closes = 0
    mcp_manager_creations = 0
    mcp_manager_closes = 0
    cleanup_calls = 0

    class RecordingShellManager:
        def __init__(self) -> None:
            nonlocal shell_manager_creations
            shell_manager_creations += 1

        async def close(self) -> None:
            nonlocal shell_manager_closes
            shell_manager_closes += 1

    class RecordingMCPManager:
        def __init__(self, _timeout_config) -> None:
            nonlocal mcp_manager_creations
            mcp_manager_creations += 1

        async def close(self) -> None:
            nonlocal mcp_manager_closes
            mcp_manager_closes += 1

    async def quiet_cleanup(_mcp_manager) -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1

    config_path = tmp_path / "config.yaml"
    if failure == "invalid":
        config_path.write_text("invalid", encoding="utf-8")

        def reject_config(_path):
            raise ValueError("invalid config")

        monkeypatch.setattr(cli.Config, "from_yaml", reject_config)

    monkeypatch.setattr(cli.Config, "get_default_config_path", lambda: config_path)
    monkeypatch.setattr(cli, "BackgroundShellManager", RecordingShellManager)
    monkeypatch.setattr(cli, "MCPManager", RecordingMCPManager)
    monkeypatch.setattr(cli, "_quiet_cleanup", quiet_cleanup)

    await cli.run_agent(tmp_path)

    assert shell_manager_creations == 0
    assert shell_manager_closes == 0
    assert mcp_manager_creations == 0
    assert mcp_manager_closes == 0
    assert cleanup_calls == 0


@pytest.mark.asyncio
async def test_run_agent_uses_the_same_manager_for_runtime_and_cleanup(
    monkeypatch,
    tmp_path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("offline", encoding="utf-8")
    config = make_cli_config()
    llm_client = object()
    shell_managers = []
    mcp_managers = []
    runtime_shell_managers = []
    runtime_mcp_managers = []
    closed_shell_managers = []
    closed_mcp_managers = []
    cleanup_calls = 0

    class RecordingShellManager:
        def __init__(self) -> None:
            shell_managers.append(self)

        async def close(self) -> None:
            closed_shell_managers.append(self)

    class RecordingMCPManager:
        def __init__(self, timeout_config) -> None:
            self.timeout_config = timeout_config
            mcp_managers.append(self)

        async def close(self) -> None:
            closed_mcp_managers.append(self)

    async def record_runtime(_workspace_dir, **kwargs) -> None:
        runtime_shell_managers.append(kwargs["shell_manager"])
        runtime_mcp_managers.append(kwargs["mcp_manager"])
        assert kwargs["config"] is config
        assert kwargs["config_path"] == config_path
        assert kwargs["llm_client"] is llm_client

    async def quiet_cleanup(mcp_manager) -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1
        await mcp_manager.close()

    monkeypatch.setattr(cli.Config, "get_default_config_path", lambda: config_path)
    monkeypatch.setattr(cli.Config, "from_yaml", lambda _path: config)
    monkeypatch.setattr(cli, "create_model_client", lambda **_kwargs: llm_client)
    monkeypatch.setattr(cli, "BackgroundShellManager", RecordingShellManager)
    monkeypatch.setattr(cli, "MCPManager", RecordingMCPManager)
    monkeypatch.setattr(cli, "_run_configured_runtime", record_runtime)
    monkeypatch.setattr(cli, "_quiet_cleanup", quiet_cleanup)

    await cli.run_agent(tmp_path)

    assert len(shell_managers) == 1
    assert len(mcp_managers) == 1
    assert mcp_managers[0].timeout_config.connect_timeout == 10.0
    assert mcp_managers[0].timeout_config.execute_timeout == 60.0
    assert mcp_managers[0].timeout_config.sse_read_timeout == 120.0
    assert runtime_shell_managers == shell_managers
    assert runtime_mcp_managers == mcp_managers
    assert closed_shell_managers == shell_managers
    assert closed_mcp_managers == mcp_managers
    assert cleanup_calls == 1


@pytest.mark.asyncio
async def test_model_client_failure_precedes_runtime_resource_creation(
    monkeypatch,
    tmp_path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("offline", encoding="utf-8")
    model_error = RuntimeError("model client construction failed")
    shell_manager_creations = 0
    mcp_manager_creations = 0
    cleanup_calls = 0

    class RecordingShellManager:
        def __init__(self) -> None:
            nonlocal shell_manager_creations
            shell_manager_creations += 1

    class RecordingMCPManager:
        def __init__(self, _timeout_config) -> None:
            nonlocal mcp_manager_creations
            mcp_manager_creations += 1

    def fail_model_client(**_kwargs):
        raise model_error

    async def quiet_cleanup(_mcp_manager) -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1

    monkeypatch.setattr(cli.Config, "get_default_config_path", lambda: config_path)
    monkeypatch.setattr(cli.Config, "from_yaml", lambda _path: make_cli_config())
    monkeypatch.setattr(cli, "create_model_client", fail_model_client)
    monkeypatch.setattr(cli, "BackgroundShellManager", RecordingShellManager)
    monkeypatch.setattr(cli, "MCPManager", RecordingMCPManager)
    monkeypatch.setattr(cli, "_quiet_cleanup", quiet_cleanup)

    with pytest.raises(RuntimeError) as raised:
        await cli.run_agent(tmp_path)

    assert raised.value is model_error
    assert shell_manager_creations == 0
    assert mcp_manager_creations == 0
    assert cleanup_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body_error",
    [None, RuntimeError("runtime failed"), asyncio.CancelledError()],
    ids=["normal", "error", "cancelled"],
)
async def test_runtime_boundary_always_cleans_shell_before_mcp(
    monkeypatch,
    body_error,
) -> None:
    order = []

    class RecordingManager:
        async def close(self) -> None:
            order.append("shell")

    mcp_manager = object()

    async def quiet_cleanup(owner) -> None:
        assert owner is mcp_manager
        order.append("mcp")

    async def runtime_body() -> None:
        order.append("body")
        if body_error is not None:
            raise body_error

    monkeypatch.setattr(cli, "_quiet_cleanup", quiet_cleanup)
    manager = RecordingManager()

    if body_error is None:
        await cli._run_with_runtime_cleanup(
            runtime_body(),
            manager,
            mcp_manager,
        )
    else:
        with pytest.raises(type(body_error)) as raised:
            await cli._run_with_runtime_cleanup(
                runtime_body(),
                manager,
                mcp_manager,
            )
        assert raised.value is body_error

    assert order == ["body", "shell", "mcp"]


@pytest.mark.asyncio
async def test_runtime_error_wins_over_cleanup_error_but_mcp_still_closes(
    monkeypatch,
    capsys,
) -> None:
    order = []
    body_error = RuntimeError("primary runtime failure")

    class FailingManager:
        async def close(self) -> None:
            order.append("shell")
            raise OSError("secondary shell cleanup failure")

    mcp_manager = object()

    async def quiet_cleanup(owner) -> None:
        assert owner is mcp_manager
        order.append("mcp")

    async def runtime_body() -> None:
        order.append("body")
        raise body_error

    monkeypatch.setattr(cli, "_quiet_cleanup", quiet_cleanup)

    with pytest.raises(RuntimeError) as raised:
        await cli._run_with_runtime_cleanup(
            runtime_body(),
            FailingManager(),
            mcp_manager,
        )

    assert raised.value is body_error
    assert order == ["body", "shell", "mcp"]
    assert "secondary shell cleanup failure" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_runtime_error_wins_over_mcp_cleanup_cancellation(
    monkeypatch,
    capsys,
) -> None:
    body_error = RuntimeError("primary runtime failure")
    mcp_error = asyncio.CancelledError("secondary MCP cleanup cancellation")

    class RecordingManager:
        async def close(self) -> None:
            pass

    mcp_manager = object()

    async def cancelled_cleanup(owner) -> None:
        assert owner is mcp_manager
        raise mcp_error

    async def runtime_body() -> None:
        raise body_error

    monkeypatch.setattr(cli, "_quiet_cleanup", cancelled_cleanup)

    with pytest.raises(RuntimeError) as raised:
        await cli._run_with_runtime_cleanup(
            runtime_body(),
            RecordingManager(),
            mcp_manager,
        )

    assert raised.value is body_error
    assert "secondary MCP cleanup cancellation" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_shell_cleanup_error_wins_over_mcp_cleanup_cancellation(
    monkeypatch,
    capsys,
) -> None:
    shell_error = OSError("primary shell cleanup failure")
    mcp_error = asyncio.CancelledError("secondary MCP cleanup cancellation")

    class FailingManager:
        async def close(self) -> None:
            raise shell_error

    mcp_manager = object()

    async def cancelled_cleanup(owner) -> None:
        assert owner is mcp_manager
        raise mcp_error

    async def runtime_body() -> None:
        pass

    monkeypatch.setattr(cli, "_quiet_cleanup", cancelled_cleanup)

    with pytest.raises(OSError) as raised:
        await cli._run_with_runtime_cleanup(
            runtime_body(),
            FailingManager(),
            mcp_manager,
        )

    assert raised.value is shell_error
    assert "secondary MCP cleanup cancellation" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_mcp_cleanup_cancellation_surfaces_when_no_prior_error_exists(
    monkeypatch,
) -> None:
    mcp_error = asyncio.CancelledError("primary MCP cleanup cancellation")

    class RecordingManager:
        async def close(self) -> None:
            pass

    mcp_manager = object()

    async def cancelled_cleanup(owner) -> None:
        assert owner is mcp_manager
        raise mcp_error

    async def runtime_body() -> None:
        pass

    monkeypatch.setattr(cli, "_quiet_cleanup", cancelled_cleanup)

    with pytest.raises(asyncio.CancelledError) as raised:
        await cli._run_with_runtime_cleanup(
            runtime_body(),
            RecordingManager(),
            mcp_manager,
        )

    assert raised.value is mcp_error


@pytest.mark.asyncio
async def test_cleanup_error_surfaces_after_successful_runtime_and_mcp_cleanup(
    monkeypatch,
) -> None:
    order = []
    cleanup_error = OSError("shell cleanup failed")

    class FailingManager:
        async def close(self) -> None:
            order.append("shell")
            raise cleanup_error

    mcp_manager = object()

    async def quiet_cleanup(owner) -> None:
        assert owner is mcp_manager
        order.append("mcp")

    async def runtime_body() -> None:
        order.append("body")

    monkeypatch.setattr(cli, "_quiet_cleanup", quiet_cleanup)

    with pytest.raises(OSError) as raised:
        await cli._run_with_runtime_cleanup(
            runtime_body(),
            FailingManager(),
            mcp_manager,
        )

    assert raised.value is cleanup_error
    assert order == ["body", "shell", "mcp"]


@pytest.mark.asyncio
async def test_quiet_cleanup_uses_owner_and_propagates_close_failure() -> None:
    close_error = OSError("MCP close failed")
    close_calls = 0

    class FailingMCPManager:
        async def close(self) -> None:
            nonlocal close_calls
            close_calls += 1
            raise close_error

    loop = asyncio.get_running_loop()
    original_handler = loop.get_exception_handler()
    try:
        with pytest.raises(OSError) as raised:
            await cli._quiet_cleanup(FailingMCPManager())
    finally:
        loop.set_exception_handler(original_handler)

    assert raised.value is close_error
    assert close_calls == 1
