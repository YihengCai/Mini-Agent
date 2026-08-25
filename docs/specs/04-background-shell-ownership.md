# 后台 shell 状态与资源所有权

> 状态：已实现。代码位于 `mini_agent/tools/bash_tool.py:109-238,249-487,490-672` 与 `mini_agent/cli.py:351-380,451-548,629-666`；离线回归位于 `tests/test_background_shell_lifecycle.py`，取舍见 [ADR-0009](../decisions/0009-runtime-owned-background-shells.md)。

## 问题证据

baseline 的 `BackgroundShellManager` 用类变量保存 shell 和监控任务，所以名义上的不同 manager、工具组和事件循环实际共享状态（`git show 953b943:mini_agent/tools/bash_tool.py | nl -ba | sed -n '108,214p'`）。故障注入还证明：取消监控只删登记而不等待任务收敛，强杀后没有第二次 `wait()`，CLI 只清理 MCP 而没有 shell owner 级关闭入口。上游证据已记入 [`UPSTREAM_AUDIT.md`](../UPSTREAM_AUDIT.md)。

## 本轮不变量

1. 配置与模型客户端构造成功后，一次 CLI runtime 才持有一个 `BackgroundShellManager`；三个 shell 工具只保存显式注入的引用。不同 manager 的标识符、输出游标、进程和监控任务完全隔离。
2. `/clear` 只新建逻辑对话的 AgentSession，继续复用同一 runtime 的工具与 manager；runtime 结束才关闭后台 shell。
3. manager 用一个操作登记 shell 与监控任务，拒绝重复标识符；监控结束只删除仍指向当前任务的登记。
4. 单项 terminate 必须在返回前等待对应 monitor 收敛；温和终止超时后强杀并再次等待 subprocess。`close()` 先封闭新登记，串行化并发关闭调用，尝试所有 shell 并等待 monitor；失败项保留给后续 `close()` 重试。
5. CLI 正常返回、普通异常和 `CancelledError` 都按 shell manager、MCP 的顺序清理。异常优先级是运行体、shell 清理、MCP 清理；次级失败不能覆盖原异常对象。
6. subprocess 已启动但 manager 拒绝登记时，`BashTool` 必须回收它；清理也失败时继续强杀与等待，并保留登记错误或取消为主因。

## 不在范围

不改变 foreground 超时、shell/PowerShell 命令格式、合并 stdout/stderr、增量读取与过滤后丢弃语义、进程组或后代进程、权限与沙箱、后台缓冲及原始事件/日志预算、MCP 内部 owner 或 AgentSession core。模型可见消息后来由 [模型可见工具输出预算](05-tool-output-budget.md) 统一约束；MCP owner 后来由 [MCP 超时与连接的运行时所有权](06-mcp-runtime-ownership.md) 单独实现。

后续 [ADR-0026](../decisions/0026-foreground-shell-reaps-on-interruption.md) 单独补齐了前台超时与取消的直接子进程回收，没有改变这里的 `BackgroundShellManager` 所有权。

## 离线验证

- `.venv/bin/python -m pytest -q tests/test_background_shell_lifecycle.py` 实测 `25 passed in 0.71s`；覆盖实例隔离、重复登记、创建回滚、取消、并发关闭、失败重试、强杀、CLI 接线和异常优先级。
- 显式排除 `external` 的完整集合实测 `227 passed, 9 deselected in 14.00s`；`.venv/bin/python -m compileall -q mini_agent tests` 与 `git diff --check` 通过。
