# ADR-0011：由 CLI runtime 持有 MCP 超时与连接

- 日期：2026-08-25
- 状态：已采纳
- 关联：`mini_agent/tools/mcp_loader.py`、`mini_agent/cli.py`、`tests/test_mcp_runtime_ownership.py`、`tests/test_background_shell_lifecycle.py`、提交 `243ffe1`、[P-014](../PITFALLS.md)

## 背景

baseline 用模块级可变对象保存默认超时与全部连接；`MCPServerConnection` 在每次取值时重新读取默认对象，任一清理入口则关闭进程中登记的全部连接（`git show 953b943:mini_agent/tools/mcp_loader.py | nl -ba | sed -n '21,57p;122,169p;284,433p'`）。改动前的双 runtime 探针先以 `11.0` 构造连接，再把全局默认改为 `22.0`，实测输出为 `first_timeout_after_second_runtime 22.0`、`second_timeout 22.0` 和 `cleanup_closed True True`。

连接又只在 `await connection.connect()` 成功后进入全局表（同上 `397-414`）；建立期间的 `CancelledError` 不属于 `Exception`，既逃出 loader，也没有任何 owner 能找到可能已进入一半的 `AsyncExitStack`。这不是 transport 功能问题，而是超时状态、连接接纳和关闭责任没有共同生命周期。

## 选项

1. **保留全局状态并在运行前后重置**：改动小，但两个同时存在的 runtime 仍会互相改写超时和清理连接。
2. **由 AgentSession 持有 MCP 连接**：逻辑对话拥有资源；但 `/clear` 替换 Session 时会提前关闭宿主仍要复用的工具。
3. **一次 CLI runtime 显式持有一个 MCP manager**：manager 固定超时、登记连接并统一关闭，和创建 transport 的宿主生命周期一致。
4. **立即建立通用 Tool 资源协议**：把 shell 与 MCP 都注册为统一 owner；能减少 CLI 的显式参数，但两者目前只有 `close()` 名字相同，接纳、任务上下文与失败恢复并不相同。

## 决定

采用选项 3。`MCPTimeoutConfig` 变为不可变快照；`MCPManager` 持有该快照、连接表、关闭门和生命周期锁。CLI 在配置与模型客户端成功后创建 manager，把同一实例交给工具加载和最终清理；`/clear` 不替换它（`mini_agent/tools/mcp_loader.py:21-27,305-411`；`mini_agent/cli.py:351-440,491-543,622-663`）。模块级 timeout setter/getter、loader 和全局 cleanup contract 全部删除。

每个连接在首个 `await connect()` 前登记；普通连接失败完成自身清理后移除，取消或意外逃逸则保留给 runtime 关闭。`close()` 在首个可重入点封闭加载，串行关闭全部连接，只删除成功项，并原样抛出首个失败；`MCPServerConnection.disconnect()` 只有在 `AsyncExitStack.aclose()` 成功后才丢弃句柄。server 级超时仍优先于 runtime 快照，判断使用 `is not None`，所以 `0.0` 也不会被误当成缺省值。

CLI 保留 shell、MCP 的串行清理和 runtime、shell、MCP 的失败优先级；`_quiet_cleanup()` 只屏蔽事件循环结束后的异步生成器噪声，不再吞掉 owner 的普通关闭异常。本轮不改 transport、配置查找、工具结果语义、重连、并行连接、权限、通用 Tool 生命周期或真实网络测试。

## 为什么否决其他的

**否决全局状态加重置**：重置只能照顾严格串行且唯一的宿主，不能定义两个 runtime 重叠时谁能改超时、谁能关闭连接。若进程保证终身只有一个 runtime，连接永不跨事件循环，且启动和退出都有不可绕过的重置入口，这个方案反而更短。

**否决 AgentSession 持有**：MCP transport 是宿主资源，Session 是可由 `/clear` 替换的逻辑对话；把二者绑定会让对话重置改变外部连接寿命。若未来每个 Session 都有独立权限、独立工具组且结束时必须撤销全部外部资源，Session owner 反而正确。

**暂不建立通用 Tool 资源协议**：shell 的同步登记关联 subprocess 与 monitor，MCP 则要求在同一任务上下文串行退出 `AsyncExitStack`；当前共同部分只有关闭顺序中的一个调用。若 CLI 之外出现多个宿主，或第三类资源也需要共享稳定的取得、关闭和错误 contract，统一资源注册表反而值得引入。

## 怎么验证它是对的

- `.venv/bin/python -m pytest -q tests/test_mcp_runtime_ownership.py` 实测 `6 passed in 0.45s`，覆盖不可变超时、server 覆盖、runtime 隔离、connect 取消、关闭取消重试、并发关闭和多连接失败重试。
- `.venv/bin/python -m pytest -q tests/test_mcp.py tests/test_mcp_runtime_ownership.py tests/test_background_shell_lifecycle.py -m 'not external'` 实测 `55 passed, 5 deselected in 0.71s`，覆盖 loader、owner 与 CLI 接线。
- 把连接登记移到 `await connect()` 之后，取消回收回归实测 `1 failed in 0.50s`；把 transport 句柄改回在 `finally` 中清空，关闭重试回归实测 `1 failed in 0.49s`。
- 显式排除 `external` 的完整集合实测 `237 passed, 9 deselected in 13.10s`；`.venv/bin/python -m compileall -q mini_agent tests` 与 `git diff --check` 通过。外部 MCP/网络测试本次未运行。

## 回头看

初版 manager 已经把关闭失败的连接留在 `remaining`，但沿用了叶子 `disconnect()` 的无条件 `finally` 清空；故障注入证明首次 `CancelledError` 后 manager 名义上仍拥有连接，实际 `AsyncExitStack` 已丢，第二次关闭不会再调用 transport。最终把句柄清理移到成功路径，并增加原异常对象与第二次调用次数断言，见 [P-014](../PITFALLS.md)。

复审还发现原 `_quiet_cleanup()` 会吞掉所有普通 MCP 关闭异常，使既有的 runtime、shell、MCP 优先级只在测试替身上成立；当前改为让 owner 异常进入统一收集。最终没有并行断开连接，因为 MCP/anyio 的上下文具有任务归属，串行关闭既保留原行为，也让失败次序确定。
