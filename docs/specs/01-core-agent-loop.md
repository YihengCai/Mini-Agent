# core agent loop 与 CLI 观察边界

> 状态：已实现并通过离线回归验证。实现位于 `mini_agent/core/` 与 `mini_agent/cli_events.py`，取舍见 [ADR-0003](../decisions/0003-remove-acp-and-extract-core-loop.md)。

## 问题证据

改造前的 `mini_agent/agent.py` 在模型—工具循环中直接调用 `print()`、终端颜色工具和 `AgentLogger`；ACP 在另一文件复制模型调用、工具执行与消息追加。可用以下命令复查删除前的实现：

```bash
git show fe6a682^:mini_agent/agent.py
git show cd9ae14^:mini_agent/acp/__init__.py
git show cd9ae14^:tests/test_acp.py
```

最后一条显示 ACP 测试直接构造 Python 对象，没有经过 JSON-RPC、stdio 或真实客户端。它不能证明协议互操作，却让每次执行框架改造都要维护第二条控制流。

## 已实现边界

- `Agent` 在 `mini_agent/core/agent.py:33-593` 持有消息、工具表、压缩状态、取消检查和唯一模型—工具循环；CLI 使用 `add_user_message()` 与 `clear_history()` 修改会话，不再直接替换消息列表。
- `Agent.run(cancel_event=None, event_sink=None)` 保留字符串返回；不给 `event_sink` 时没有终端输出或日志副作用（`mini_agent/core/agent.py:374-589`）。
- `mini_agent/core/events.py:1-160` 定义运行、步骤、模型、工具、压缩、清理和终止事件。事件接收器是同步回调；事件数据在回调期间只读借用，需要留存时由消费者立即复制或序列化。
- `mini_agent/cli_events.py:1-233` 把事件映射到原终端显示和 `AgentLogger`；CLI 的非交互与交互入口都把同一个接收器传给 core（`mini_agent/cli.py:569-589,715-788`）。
- `mini_agent/agent.py` 保留 `Agent` 的旧导入路径；这只是兼容转发，不包含控制流。
- 发行配置不再包含 ACP 源码、测试、命令或依赖（`pyproject.toml:11-27`）。

## 不变量

1. 生产代码只有 `mini_agent/core/agent.py` 调用模型并执行工具；UI 或未来消费者不能复制 loop。
2. core 不导入 CLI、终端颜色、`AgentLogger` 或传输模块，也不调用 `print()`。
3. 一次正常工具流程的事件顺序是运行开始、步骤开始、模型请求、模型响应、工具开始、工具完成、步骤完成；最终响应后只有一个 `RunFinished`，其 `result` 等于 `Agent.run()` 的返回值。
4. 摘要模型调用使用 `purpose="summary"` 进入同一观察序列；主循环调用使用 `purpose="agent"`。
5. `ModelRequest` 在模型调用前发出，`ModelResponse` 在 assistant 消息追加前发出，`ToolFinished` 在 tool 消息追加前发出；同步 CLI 接收器因而保留原日志的请求、响应、工具结果顺序。
6. 接收器异常直接传播。本轮不在 core 中定义尽力而为或持久化失败策略。

## 未包含

- 不把同步事件宣称为会话事实日志、持久化格式、回放 contract 或基准评测轨迹；当前消息仍会被压缩和取消清理改写。
- 不把 `Agent.run()` 改成结构化结果或异步生成器，不修复等待中的模型与工具无法被真正取消的问题。
- 不拆分日志与统计为独立消费者，不定义 token、成本、延迟或任务成功率口径。
- 不保留占位 ACP 模块；只有真实客户端和端到端协议测试出现后才重新评估协议层。

## 已验证行为

- `tests/test_agent_loop_offline.py:290-352` 验证两步工具循环的完整事件顺序和终止结果；
- `tests/test_agent_loop_offline.py:355-412` 验证无 `event_sink` 的静默 core 以及 CLI 显示、日志调用；
- `tests/test_agent_loop_offline.py:457-521` 验证摘要与主循环事件顺序；
- README 的离线测试命令在 2026-08-24 实测为 `122 passed`，`uv lock --check` 通过。
