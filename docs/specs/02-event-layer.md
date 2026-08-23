# 事件层、中断与 steering

> 状态：测试框架之后的第一个核心模块。steering 指运行中追加指令。

## 要解决的问题

`Agent.run()`（`mini_agent/agent.py:294-492`）同时持有控制流和终端渲染，`agent.py` 有约 30 个 `print()`；ACP 因此在 `mini_agent/acp/__init__.py:127-165` 复制了一份已经与主循环不一致的副本。当前 Esc 只设置事件，不调用 `Task.cancel()`；消息历史清理还会删除已完成的工具结果。复现见 [P-003](../PITFALLS.md)。

## 实现顺序

### 1. 最小事件层

保留 `await agent.run() -> str`；新增 `on_event` 接收器，把循环输出全部变成带类型的事件。第一版只定义现有使用方需要的事件：

- `RunStarted` / `RunFinished`；
- `StepStarted`；
- `MessageCompleted`；
- `ToolStarted` / `ToolFinished`；
- `Interrupted`；
- `AgentError`。

`ConsoleRenderer` 保持现有 CLI 输出，`JsonlSink` 提供机器可读记录，`AcpSink` 替换 ACP 中重复的循环。压缩、权限和 subagent 事件在对应模块实现时新增，不提前冻结未使用的协议。

事件携带完整内容；字符限制、ANSI 和布局只存在于渲染器。

### 2. 中断恢复

`interrupt()` 必须取消当前运行任务。`except asyncio.CancelledError` 的第一条语句调用同步的 `_repair_history()`：

- 找出最后一个 assistant 工具调用组；
- 为缺失的 ID 追加非空合成工具结果；
- 保留已完成的工具结果；
- 修复后再发送事件；
- 不是本 agent 发起的取消操作继续向外抛。

原则是“补齐，不删除”。文件或进程副作用已经发生，删除对话记录只会让模型可见的消息历史与现实不一致。

`BashTool` 遇到 `CancelledError` 时必须终止并等待子进程，然后重新抛出异常；验证还要确认没有残留进程。

### 3. 流式输出

先探测 C4/C5。支持时，模型客户端负责组装分片的工具 JSON，agent 只接收完整的 `LLMResponse`；文本增量通过事件旁路发送。首个增量已经显示后不得自动重试，否则会拼接两个响应。

不支持时，`stream_generate()` 降级到一次性 `generate()`，事件与渲染器 contract 不变。

### 4. Steering

运行中的输入进入队列，只在已经闭合的步骤边界清空队列。不能把用户内容插在 assistant 工具调用和剩余工具结果之间。若协议格式需要，把 steering 内容合并到已经完成的工具结果用户轮次；具体行为由 C9 探测结果决定。

## 离线验证

`tests/test_events.py` 至少覆盖：

- `on_event=None` 时 agent 的 stdout 为空；
- 渲染输出与重构前的固定结果一致；
- 取消第二个工具时，第一个工具结果保留、缺失结果被补齐、消息历史仍然有效；
- 外层取消操作不会被吞掉；
- steering 在最后一个工具结果之后、下一条 assistant 之前恰好注入一次；
- 事件接收器抛出异常时有明确策略，不让 UI 故障破坏消息历史。

真实演示补一条进程检查：中断长时间 shell 后，目标进程不存在。

## 不在范围内

完整 TUI、事件持久化协议版本控制、背压与重放、权限请求事件、并行工具调度、跨平台输入线程重写。
