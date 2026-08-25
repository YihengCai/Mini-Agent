"""Shell command execution tool with background process management.

Supports both bash (Unix/Linux/macOS) and PowerShell (Windows).
"""

import asyncio
import platform
import re
import time
import uuid
from typing import Any

from pydantic import Field, model_validator

from .base import Tool, ToolResult


class BashOutputResult(ToolResult):
    """Bash command execution result with separated stdout and stderr.

    Inherits from ToolResult which provides:
    - success: bool
    - content: str (used for formatted output message, auto-generated from stdout/stderr)
    - error: str | None (used for error messages)
    """

    stdout: str = Field(description="The command's standard output")
    stderr: str = Field(description="The command's standard error output")
    exit_code: int = Field(description="The command's exit code")
    bash_id: str | None = Field(default=None, description="Shell process ID (only when run_in_background=True)")

    @model_validator(mode="after")
    def format_content(self) -> "BashOutputResult":
        """Auto-format content from stdout and stderr if content is empty."""
        output = ""
        if self.stdout:
            output += self.stdout
        if self.stderr:
            output += f"\n[stderr]:\n{self.stderr}"
        if self.bash_id:
            output += f"\n[bash_id]:\n{self.bash_id}"
        if self.exit_code:
            output += f"\n[exit_code]:\n{self.exit_code}"

        if not output:
            output = "(no output)"

        self.content = output
        return self


class BackgroundShell:
    """Background shell data container.

    Pure data class that only stores state and output.
    IO operations are managed externally by BackgroundShellManager.
    """

    def __init__(self, bash_id: str, command: str, process: "asyncio.subprocess.Process", start_time: float):
        self.bash_id = bash_id
        self.command = command
        self.process = process
        self.start_time = start_time
        self.output_lines: list[str] = []
        self.last_read_index = 0
        self.status = "running"
        self.exit_code: int | None = None

    def add_output(self, line: str):
        """Add new output line."""
        self.output_lines.append(line)

    def get_new_output(self, filter_pattern: str | None = None) -> list[str]:
        """Get new output since last check, optionally filtered by regex."""
        new_lines = self.output_lines[self.last_read_index :]
        self.last_read_index = len(self.output_lines)

        if filter_pattern:
            try:
                pattern = re.compile(filter_pattern)
                new_lines = [line for line in new_lines if pattern.search(line)]
            except re.error:
                # Invalid regex, return all lines
                pass

        return new_lines

    def update_status(self, is_alive: bool, exit_code: int | None = None):
        """Update process status."""
        if not is_alive:
            self.status = "completed" if exit_code == 0 else "failed"
            self.exit_code = exit_code
        else:
            self.status = "running"

    async def terminate(self) -> None:
        """Terminate the background process."""
        if self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        self.status = "terminated"
        self.exit_code = self.process.returncode


class BackgroundShellManager:
    """Own one runtime's background shells and monitor tasks."""

    def __init__(self) -> None:
        self._shells: dict[str, BackgroundShell] = {}
        self._monitor_tasks: dict[str, asyncio.Task] = {}
        self._closed = False
        self._close_lock = asyncio.Lock()

    def track(self, shell: BackgroundShell) -> None:
        """Atomically register one shell and start its monitor task."""

        bash_id = shell.bash_id
        if self._closed:
            raise RuntimeError("Background shell manager is closed")
        if bash_id in self._shells or bash_id in self._monitor_tasks:
            raise ValueError(f"Duplicate shell ID: {bash_id}")

        self._shells[bash_id] = shell
        monitor = self._monitor(shell)
        try:
            task = asyncio.create_task(monitor)
        except BaseException:
            monitor.close()
            del self._shells[bash_id]
            raise

        if not task.done():
            self._monitor_tasks[bash_id] = task

    def get(self, bash_id: str) -> BackgroundShell | None:
        """Get a background shell by ID."""
        return self._shells.get(bash_id)

    def get_available_ids(self) -> list[str]:
        """Get all available bash IDs."""
        return list(self._shells.keys())

    def _remove(self, bash_id: str) -> None:
        """Remove a background shell from management (internal use only)."""
        self._shells.pop(bash_id, None)

    async def _monitor(self, shell: BackgroundShell) -> None:
        bash_id = shell.bash_id
        try:
            process = shell.process
            while process.stdout is not None:
                try:
                    line = await asyncio.wait_for(
                        process.stdout.readline(),
                        timeout=0.1,
                    )
                    if not line:
                        break
                    decoded_line = line.decode(
                        "utf-8",
                        errors="replace",
                    ).rstrip("\n")
                    shell.add_output(decoded_line)
                except asyncio.TimeoutError:
                    await asyncio.sleep(0.1)
                    continue
                except Exception:
                    if process.returncode is not None:
                        break
                    await asyncio.sleep(0.1)
                    continue

            try:
                returncode = await process.wait()
            except Exception:
                returncode = -1
            shell.update_status(is_alive=False, exit_code=returncode)
        except Exception as error:
            if self.get(bash_id) is shell:
                shell.status = "error"
                shell.add_output(f"Monitor error: {error}")
        finally:
            current_task = asyncio.current_task()
            if self._monitor_tasks.get(bash_id) is current_task:
                del self._monitor_tasks[bash_id]

    async def _cancel_monitor(self, bash_id: str) -> None:
        """Cancel a monitor and wait until its cleanup has completed."""

        task = self._monitor_tasks.pop(bash_id, None)
        if task is None:
            return
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def terminate(self, bash_id: str) -> BackgroundShell:
        """Terminate a background shell and clean up all resources.

        Args:
            bash_id: The unique identifier of the background shell

        Returns:
            The terminated BackgroundShell object

        Raises:
            ValueError: If shell not found
        """
        shell = self.get(bash_id)
        if not shell:
            raise ValueError(f"Shell not found: {bash_id}")

        await shell.terminate()
        await self._cancel_monitor(bash_id)
        self._remove(bash_id)

        return shell

    async def close(self) -> None:
        """Seal registration and terminate all shells, retaining failures for retry."""

        self._closed = True
        async with self._close_lock:
            results = await asyncio.gather(
                *(self.terminate(bash_id) for bash_id in list(self._shells)),
                return_exceptions=True,
            )
            await asyncio.gather(
                *(self._cancel_monitor(bash_id) for bash_id in list(self._monitor_tasks)),
            )

            for result in results:
                if isinstance(result, BaseException):
                    raise result


async def _kill_and_wait(process: asyncio.subprocess.Process) -> None:
    """Kill a direct subprocess if needed, then wait for it to be reaped."""

    if process.returncode is None:
        process.kill()
    await process.wait()


class BashTool(Tool):
    """Execute shell commands in foreground or background.

    Automatically detects OS and uses appropriate shell:
    - Windows: PowerShell
    - Unix/Linux/macOS: bash
    """

    def __init__(
        self,
        workspace_dir: str | None = None,
        *,
        manager: BackgroundShellManager,
    ):
        """Initialize BashTool with OS-specific shell detection.

        Args:
            workspace_dir: Working directory for command execution.
                           If provided, all commands run in this directory.
                           If None, commands run in the process's cwd.
        """
        self.is_windows = platform.system() == "Windows"
        self.shell_name = "PowerShell" if self.is_windows else "bash"
        self.workspace_dir = workspace_dir
        self._manager = manager

    @property
    def name(self) -> str:
        return "bash"

    @property
    def description(self) -> str:
        shell_examples = {
            "Windows": """Execute PowerShell commands in foreground or background.

For terminal operations like git, npm, docker, etc. DO NOT use for file operations - use specialized tools.

Parameters:
  - command (required): PowerShell command to execute
  - timeout (optional): Timeout in seconds (default: 120, max: 600) for foreground commands
  - run_in_background (optional): Set true for long-running commands (servers, etc.)

Tips:
  - Quote file paths with spaces: cd "My Documents"
  - Chain dependent commands with semicolon: git add . ; git commit -m "msg"
  - Use absolute paths instead of cd when possible
  - For background commands, monitor with bash_output and terminate with bash_kill

Examples:
  - git status
  - npm test
  - python -m http.server 8080 (with run_in_background=true)""",
            "Unix": """Execute bash commands in foreground or background.

For terminal operations like git, npm, docker, etc. DO NOT use for file operations - use specialized tools.

Parameters:
  - command (required): Bash command to execute
  - timeout (optional): Timeout in seconds (default: 120, max: 600) for foreground commands
  - run_in_background (optional): Set true for long-running commands (servers, etc.)

Tips:
  - Quote file paths with spaces: cd "My Documents"
  - Chain dependent commands with &&: git add . && git commit -m "msg"
  - Use absolute paths instead of cd when possible
  - For background commands, monitor with bash_output and terminate with bash_kill

Examples:
  - git status
  - npm test
  - python3 -m http.server 8080 (with run_in_background=true)""",
        }
        return shell_examples["Windows"] if self.is_windows else shell_examples["Unix"]

    @property
    def parameters(self) -> dict[str, Any]:
        cmd_desc = f"The {self.shell_name} command to execute. Quote file paths with spaces using double quotes."
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": cmd_desc,
                },
                "timeout": {
                    "type": "integer",
                    "description": "Optional: Timeout in seconds (default: 120, max: 600). Only applies to foreground commands.",
                    "default": 120,
                },
                "run_in_background": {
                    "type": "boolean",
                    "description": "Optional: Set to true to run the command in the background. Use this for long-running commands like servers. You can monitor output using bash_output tool.",
                    "default": False,
                },
            },
            "required": ["command"],
        }

    async def execute(
        self,
        command: str,
        timeout: int = 120,
        run_in_background: bool = False,
    ) -> ToolResult:
        """Execute shell command with optional background execution.

        Args:
            command: The shell command to execute
            timeout: Timeout in seconds (default: 120, max: 600)
            run_in_background: Set true to run command in background

        Returns:
            BashExecutionResult with command output and status
        """

        background_cleanup_error: BaseException | None = None
        try:
            # Validate timeout
            if timeout > 600:
                timeout = 600
            elif timeout < 1:
                timeout = 120

            # Prepare shell-specific command execution
            if self.is_windows:
                # Windows: Use PowerShell with appropriate encoding
                shell_cmd = ["powershell.exe", "-NoProfile", "-Command", command]
            else:
                # Unix/Linux/macOS: Use bash
                shell_cmd = command

            if run_in_background:
                # Background execution: Create isolated process
                bash_id = str(uuid.uuid4())[:8]

                # Start background process with combined stdout/stderr
                if self.is_windows:
                    process = await asyncio.create_subprocess_exec(
                        *shell_cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT,
                        cwd=self.workspace_dir,
                    )
                else:
                    process = await asyncio.create_subprocess_shell(
                        shell_cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT,
                        cwd=self.workspace_dir,
                    )

                # Create background shell and register it with this runtime.
                bg_shell = BackgroundShell(bash_id=bash_id, command=command, process=process, start_time=time.time())
                try:
                    self._manager.track(bg_shell)
                except BaseException as tracking_error:
                    try:
                        await bg_shell.terminate()
                    except BaseException as cleanup_error:
                        background_cleanup_error = cleanup_error
                        try:
                            if process.returncode is None:
                                process.kill()
                                await process.wait()
                        except BaseException as force_cleanup_error:
                            background_cleanup_error = force_cleanup_error
                        raise tracking_error from background_cleanup_error
                    raise

                # Return immediately with bash_id
                message = f"Command started in background. Use bash_output to monitor (bash_id='{bash_id}')."
                formatted_content = f"{message}\n\nCommand: {command}\nBash ID: {bash_id}"

                return BashOutputResult(
                    success=True,
                    content=formatted_content,
                    stdout=f"Background command started with ID: {bash_id}",
                    stderr="",
                    exit_code=0,
                    bash_id=bash_id,
                )

            else:
                # Foreground execution: Create isolated process
                if self.is_windows:
                    process = await asyncio.create_subprocess_exec(
                        *shell_cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        cwd=self.workspace_dir,
                    )
                else:
                    process = await asyncio.create_subprocess_shell(
                        shell_cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        cwd=self.workspace_dir,
                    )

                try:
                    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
                except asyncio.TimeoutError:
                    await _kill_and_wait(process)
                    error_msg = f"Command timed out after {timeout} seconds"
                    return BashOutputResult(
                        success=False,
                        error=error_msg,
                        stdout="",
                        stderr=error_msg,
                        exit_code=-1,
                    )
                except BaseException as execution_error:
                    try:
                        await _kill_and_wait(process)
                    except BaseException as cleanup_error:
                        raise execution_error from cleanup_error
                    raise

                # Decode output
                stdout_text = stdout.decode("utf-8", errors="replace")
                stderr_text = stderr.decode("utf-8", errors="replace")

                # Create result (content auto-formatted by model_validator)
                is_success = process.returncode == 0
                error_msg = None
                if not is_success:
                    error_msg = f"Command failed with exit code {process.returncode}"
                    if stderr_text:
                        error_msg += f"\n{stderr_text.strip()}"

                return BashOutputResult(
                    success=is_success,
                    error=error_msg,
                    stdout=stdout_text,
                    stderr=stderr_text,
                    exit_code=process.returncode or 0,
                )

        except Exception as e:
            error_message = str(e)
            if background_cleanup_error is not None:
                error_message += (
                    "; background process cleanup also failed: "
                    f"{type(background_cleanup_error).__name__}: "
                    f"{background_cleanup_error}"
                )
            return BashOutputResult(
                success=False,
                error=error_message,
                stdout="",
                stderr=error_message,
                exit_code=-1,
            )


class BashOutputTool(Tool):
    """Retrieve output from background bash shells."""

    def __init__(self, *, manager: BackgroundShellManager) -> None:
        self._manager = manager

    @property
    def name(self) -> str:
        return "bash_output"

    @property
    def description(self) -> str:
        return """Retrieves output from a running or completed background bash shell.

        - Takes a bash_id parameter identifying the shell
        - Always returns only new output since the last check
        - Returns stdout and stderr output along with shell status
        - Supports optional regex filtering to show only lines matching a pattern
        - Use this tool when you need to monitor or check the output of a long-running shell
        - Shell IDs can be found using the bash tool with run_in_background=true

        Process status values:
          - "running": Still executing
          - "completed": Finished successfully
          - "failed": Finished with error
          - "terminated": Was terminated
          - "error": Error occurred

        Example: bash_output(bash_id="abc12345")"""

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "bash_id": {
                    "type": "string",
                    "description": "The ID of the background shell to retrieve output from. Shell IDs are returned when starting a command with run_in_background=true.",
                },
                "filter_str": {
                    "type": "string",
                    "description": "Optional regular expression to filter the output lines. Only lines matching this regex will be included in the result. Any lines that do not match will no longer be available to read.",
                },
            },
            "required": ["bash_id"],
        }

    async def execute(
        self,
        bash_id: str,
        filter_str: str | None = None,
    ) -> BashOutputResult:
        """Retrieve output from background shell.

        Args:
            bash_id: The unique identifier of the background shell
            filter_str: Optional regex pattern to filter output lines

        Returns:
            BashOutputResult with shell output including stdout, stderr, status, and success flag
        """

        try:
            # Get background shell from manager
            bg_shell = self._manager.get(bash_id)
            if not bg_shell:
                available_ids = self._manager.get_available_ids()
                return BashOutputResult(
                    success=False,
                    error=f"Shell not found: {bash_id}. Available: {available_ids or 'none'}",
                    stdout="",
                    stderr="",
                    exit_code=-1,
                )

            # Get new output
            new_lines = bg_shell.get_new_output(filter_pattern=filter_str)
            stdout = "\n".join(new_lines) if new_lines else ""

            return BashOutputResult(
                success=True,
                stdout=stdout,
                stderr="",  # Background shells combine stdout/stderr
                exit_code=bg_shell.exit_code if bg_shell.exit_code is not None else 0,
                bash_id=bash_id,
            )

        except Exception as e:
            return BashOutputResult(
                success=False,
                error=f"Failed to get bash output: {str(e)}",
                stdout="",
                stderr=str(e),
                exit_code=-1,
            )


class BashKillTool(Tool):
    """Terminate a running background bash shell."""

    def __init__(self, *, manager: BackgroundShellManager) -> None:
        self._manager = manager

    @property
    def name(self) -> str:
        return "bash_kill"

    @property
    def description(self) -> str:
        return """Kills a running background bash shell by its ID.

        - Takes a bash_id parameter identifying the shell to kill
        - Attempts graceful termination (SIGTERM) first, then forces (SIGKILL) if needed
        - Returns the final status and any remaining output before termination
        - Cleans up all resources associated with the shell
        - Use this tool when you need to terminate a long-running shell
        - Shell IDs can be found using the bash tool with run_in_background=true

        Example: bash_kill(bash_id="abc12345")"""

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "bash_id": {
                    "type": "string",
                    "description": "The ID of the background shell to terminate. Shell IDs are returned when starting a command with run_in_background=true.",
                },
            },
            "required": ["bash_id"],
        }

    async def execute(self, bash_id: str) -> BashOutputResult:
        """Terminate a background shell process.

        Args:
            bash_id: The unique identifier of the background shell to terminate

        Returns:
            BashOutputResult with termination status and remaining output
        """

        try:
            # Get remaining output before termination
            bg_shell = self._manager.get(bash_id)
            if bg_shell:
                remaining_lines = bg_shell.get_new_output()
            else:
                remaining_lines = []

            # Terminate through manager (handles all cleanup)
            bg_shell = await self._manager.terminate(bash_id)

            # Get remaining output
            stdout = "\n".join(remaining_lines) if remaining_lines else ""

            return BashOutputResult(
                success=True,
                stdout=stdout,
                stderr="",
                exit_code=bg_shell.exit_code if bg_shell.exit_code is not None else 0,
                bash_id=bash_id,
            )

        except ValueError as e:
            # Shell not found
            available_ids = self._manager.get_available_ids()
            return BashOutputResult(
                success=False,
                error=f"{str(e)}. Available: {available_ids or 'none'}",
                stdout="",
                stderr=str(e),
                exit_code=-1,
            )
        except Exception as e:
            return BashOutputResult(
                success=False,
                error=f"Failed to terminate bash shell: {str(e)}",
                stdout="",
                stderr=str(e),
                exit_code=-1,
            )
