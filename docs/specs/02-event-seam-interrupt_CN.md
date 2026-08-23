# 事件缝、正确中断与 steering

> 状态：测试底座后的第一个核心实现模块。

## 要解决的问题

`Agent.run()`（`mini_agent/agent.py:294-492`）同时持有控制流和终端渲染，`agent.py` 有约 30 个 `print()`；ACP 因此在 `mini_agent/acp/__init__.py:127-165` 复制了一份已漂移的循环。当前 Esc 只设置 event，不调用 `Task.cancel()`；历史清理又会删除已经完成的 tool 结果，复现见 [P-003](../PITFALLS.md)。

## 实现顺序

### 1. 最小事件缝

保留 `await agent.run() -> str`；新增 `on_event` sink，把引擎中的输出全部变成事件。第一版只定义已有消费者需要的事件：

- `RunStarted` / `RunFinished`；
- `StepStarted`；
- `MessageCompleted`；
- `ToolStarted` / `ToolFinished`；
- `Interrupted`；
- `AgentError`。

`ConsoleRenderer` 保持现有 CLI 输出，`JsonlSink` 提供机器可读记录，`AcpSink` 替换 ACP 的复制循环。未来 compaction、permission 和 subagent 事件在对应模块落地时新增，不提前冻结未使用的协议。

引擎事件携带完整内容；字符上限、ANSI 和布局只存在于 renderer。

### 2. 正确中断

`interrupt()` 必须取消当前 run task。`except asyncio.CancelledError` 的第一条语句调用同步 `_repair_history()`：

- 找出最后一个 assistant tool-call 组；
- 为尚未完成的 id 追加非空合成 tool result；
- 保留已经完成的 tool result；
- 修复后再发事件；
- 不是本 agent 主动发出的取消继续向外抛。

原则是“合成，不删除”。文件或进程副作用已经发生，删除 transcript 只会让模型与现实分叉。

`BashTool` 在 `CancelledError` 下必须杀死并等待子进程后重新抛出；验收不仅看 Python task 结束，还要确认没有残留进程。

### 3. 流式

先探测 C4/C5。支持时，provider client 负责组装碎片化 tool JSON，agent 只接收完整 `LLMResponse`；文本 delta 走事件旁路。首个 delta 已经对用户可见后不得透明重试，否则两个回答会拼在一起。

不支持时，`stream_generate()` 退回一次性 `generate()`，事件与 renderer 契约不变。

### 4. Steering

运行中输入进入队列，只在完整 step 边界排空。绝不能把 user 文本插在 assistant tool call 和剩余 tool result 之间。若 wire format 需要，把 steering 文本合并进闭合 tool-result user turn；具体行为由 C9 探测决定。

## 离线工件

`tests/test_events.py` 至少覆盖：

- `on_event=None` 时引擎 stdout 为空；
- renderer 输出与重构前 golden 一致；
- 取消第二个工具时，第一个工具结果保留、缺失结果被合成、历史仍有效；
- 外层取消不会被吞掉；
- steering 在最后一个 tool result 之后、下一条 assistant 之前恰好注入一次；
- sink 抛普通异常时有明确策略，不让 UI 故障静默破坏历史。

真实演示补一条子进程检查：中断长时间 shell 后，目标进程不存在。

## 明确后置

完整 TUI、事件持久化协议版本、背压与重放、权限请求事件、并行工具调度、跨平台输入线程重写。
