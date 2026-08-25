# ADR-0009：由 CLI runtime 持有后台 shell 资源

- 日期：2026-08-25
- 状态：已采纳
- 关联：`mini_agent/tools/bash_tool.py`、`mini_agent/cli.py`、`tests/test_background_shell_lifecycle.py`、提交 `9a088b6`、[P-010](../PITFALLS.md)、[P-011](../PITFALLS.md)

## 背景

baseline 的 `BackgroundShellManager` 用类变量保存 shell 与 monitor 任务，`BashTool`、`BashOutputTool` 和 `BashKillTool` 通过这两张全局表隐式协作（`git show 953b943:mini_agent/tools/bash_tool.py | nl -ba | sed -n '108,214p'`）。CLI 分别构造三个工具，退出时却只清理 MCP（`git show 953b943:mini_agent/cli.py | nl -ba | sed -n '303,328p;399,448p;805,806p'`）。

故障注入证明两个 manager 会互相看到 shell，旧事件循环的 subprocess 会泄漏到新循环；取消 monitor 后 manager 已返回但任务尚未收敛，强杀也没有再次等待进程。实现前的 10 项生命周期回归在旧代码上实测为 `10 failed in 0.76s`。

## 选项

1. **保留进程级全局 manager**：只补 `close()` 与等待；构造接口最小，但不同 CLI runtime 和事件循环仍共享可变状态。
2. **每个工具或 AgentSession 各持有 manager**：局部所有权明确，但三个 shell 工具无法天然协作；`/clear` 替换 Session 时也会让已运行进程失去控制入口。
3. **一次 CLI runtime 显式持有一个 manager**：把同一实例注入三个工具，`/clear` 只替换逻辑对话，runtime 退出时统一关闭。
4. **先建立通用工具资源协议**：为所有 Tool 引入 owner 和 `close()` contract；可扩展到 MCP 等资源，但当前只有 shell 的生命周期证据。

## 决定

采用选项 3。`BackgroundShellManager` 改为实例状态，三个 shell 工具的构造函数都要求显式 `manager`；CLI 在配置和模型客户端构造成功后创建唯一实例，工具组装、`/clear` 后的新 Session 和最终清理都使用它（`mini_agent/cli.py:351-380,451-476,629-666,799-808`）。

manager 以单个 `track()` 原子登记 shell 和 monitor，拒绝重复标识符与关闭后的新登记。`close()` 先封闭登记入口，用锁串行化并发调用，尝试全部 shell，并保留失败项给后续关闭重试（`mini_agent/tools/bash_tool.py:109-238`）。monitor 取消和 subprocess 强杀都要等待收敛。

CLI 的清理顺序是 shell、MCP，失败优先级是 runtime、shell、MCP；次级清理错误只诊断，不覆盖原异常对象（`mini_agent/cli.py:512-548`）。本轮不改进程组、后代进程、输出预算、权限、操作系统沙箱、前台超时或 MCP 内部 owner。

## 为什么否决其他的

**否决进程级全局 manager**：它无法把 subprocess 与创建它的事件循环和宿主生命周期对齐，也不能在同一进程的两个宿主间隔离标识符与输出游标。若程序只有一个不会重启事件循环的全局宿主，且有可验证的进程退出回收，该方案反而足够。

**否决工具或 Session 各自持有**：后台启动、读取和终止是一个资源的三个视图，分开 owner 会破坏协作；Session 又是逻辑对话边界，不是宿主资源边界。若一个 Session 确实拥有完整工具组且其结束必然终止所有副作用，Session owner 反而更直接。

**暂不建通用工具资源协议**：会把单一 shell 问题扩展成所有 Tool 的公开 contract，却没有第二种资源的关闭顺序、错误与所有权证据。当至少两类工具共享稳定的生命周期 contract，并且存在 CLI 之外的多个宿主时，该抽象反而值得引入。

## 怎么验证它是对的

- `.venv/bin/python -m pytest -q tests/test_background_shell_lifecycle.py` 实测 `25 passed in 0.71s`，覆盖隔离、原子登记、进程回收、monitor 收敛、并发关闭、失败重试、CLI 接线和异常优先级。
- `.venv/bin/python -m pytest -q tests/test_background_shell_lifecycle.py tests/test_bash_tool.py tests/test_tools.py tests/test_session_integration.py tests/test_tool_execution.py -W error` 实测 `88 passed in 9.43s`。
- 显式排除 `external` 的完整集合实测 `227 passed, 9 deselected in 14.00s`；`.venv/bin/python -m compileall -q mini_agent tests` 与 `git diff --check` 通过。

## 回头看

初版 CLI wrapper 包住了配置早退路径，会在还没有 runtime 资源时清理全局 MCP 并永久替换事件循环异常处理器；MCP 清理的 `CancelledError` 还能覆盖已捕获的 runtime 主异常。最终把 owner 边界移到配置与模型客户端成功之后，并显式定义三层失败优先级，见 [P-010](../PITFALLS.md)。

初版 `close()` 只快照当前表并并发 terminate，串行重复调用的测试不能暴露关闭途中新登记和并发二次终止。故障注入后增加封闭门与关闭锁，失败项保留供下次重试，见 [P-011](../PITFALLS.md)。最终实现没有引入通用 Tool 生命周期 contract，也没有把输出预算或进程组混入本轮。

后续 [ADR-0011](0011-runtime-owned-mcp-connections.md) 让 MCP 成为第二类 runtime owner。重新评估通用资源协议后仍没有合并：shell 的登记同时拥有 subprocess 与 monitor，MCP 的 `AsyncExitStack` 则要求同一任务内串行关闭，目前稳定共享的只有 CLI 中的关闭顺序。若以后出现第三类资源或多个宿主需要同一套取得与失败 contract，再引入注册表更合适。
