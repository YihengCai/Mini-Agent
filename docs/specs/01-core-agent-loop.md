# Session、Turn、Step 生命周期与 CLI 观察边界

> 状态：已实现并通过离线回归验证。实现位于 `mini_agent/core/` 与 `mini_agent/cli_events.py`，取舍见 [ADR-0003](../decisions/0003-remove-acp-and-extract-core-loop.md)、[ADR-0004](../decisions/0004-session-turn-step-lifecycle.md) 与 [ADR-0032](../decisions/0032-observers-do-not-control-turns.md)；旧压缩的后续删除见 [ADR-0006](../decisions/0006-remove-legacy-local-compaction.md)，正数 Step 预算边界见 [ADR-0014](../decisions/0014-positive-step-budget-at-config-and-core.md)。

## 问题证据

第一次抽取 core 后，`Agent` 同时表示长期消息状态和一次性 `run() -> str`；正常回复、模型错误、中断和步数耗尽都压进字符串，只有可选事件保留停止原因。旧代码可用下列命令复查：

```bash
git show fdcd945^:mini_agent/core/agent.py
git show fdcd945^:mini_agent/cli.py
```

旧 CLI 两种入口都丢弃 `run()` 返回值，说明字符串不是实际控制边界；模型不再请求工具被称为 `completed`，又把 Turn 结束误写成任务完成。

## 已实现边界

- `AgentSession` 表示一段逻辑对话，持有一份完整模型可见历史、只读配置和至多一个活动 Turn（`mini_agent/core/agent.py:88-233`）。当前没有自动上下文预算或压缩；CLI `/clear` 通过同一工厂创建新 Session（`mini_agent/cli.py:697-707,804-808`）。
- `start_turn(user_input, event_sink=...)` 原子接纳输入并返回 `TurnHandle`。同一 Session 有活动 Turn 时拒绝新输入；等待调用者被取消不会取消私有 runner（`mini_agent/core/agent.py:168-223`；`mini_agent/core/turn.py:51-90`）。
- Turn 从客户端交出控制权开始，到 `end_turn`、`interrupted`、`max_steps` 或 `failed` 后交还控制权。`TurnOutcome` 可以带最后回复与结构化错误，但没有任务成功字段（`mini_agent/core/turn.py:10-48`）。
- Step 是一次模型请求、该响应中的全部工具调用及结果写入。工具调用会继续同一个 Turn；所有模型事件都属于当前 Step（`mini_agent/core/agent.py:303-521`）。
- 公开 core API 只有 Session、Turn 句柄、结果和观察事件；`_AgentLoop` 是 Session 内部实现，不能绕过接纳不变量（`mini_agent/core/agent.py:236-565`；`mini_agent/core/__init__.py:1-47`）。
- `AgentEventEnvelope` 为每个事件附加 Session、Turn 和可选 Step 身份；CLI 用同步接收器分别渲染 Turn 控制权边界和内部 Step，并写原日志（`mini_agent/core/events.py:20-103`；`mini_agent/cli_events.py:22-146`）。

## 不变量

1. 一个 Session 同时至多一个活动 Turn；检查、预占、输入追加和 runner 创建失败回滚形成一个接纳边界。Turn 开始后使用接纳时的 Session 身份、模型引用、工具映射与步数。
2. 一个 Step 至多发起一次模型请求；如果响应含工具调用，同一 Step 执行完整批次，并把 assistant 工具调用与所有工具结果成组写入。下一次模型请求必定属于下一 Step。
3. `end_turn` 只表示模型没有继续请求工具；`max_steps` 只表示 Step 上限；两者都不代表用户任务正确。模型或内部失败使用 `failed`，且必须带错误细节。
4. 中断是合作式请求。完整工具批次、终止响应和模型失败优先完成当前 Step；下一 Step 不再启动。由此保留消息配对，但中断延迟可能等于一次模型调用加整批工具执行时间。
5. 模型请求消息与事件消息是两份快照；事件中的响应、工具定义、调用和结果也不能修改 Session 或真实模型输入。
6. 接收器首个普通异常会禁用该接收器；观察是最佳努力通知，不改变模型、工具、历史或 Turn 结果。已成功交给接收器的 `TurnFinished.outcome` 与 `wait()` 返回同一个对象；接收器失败后宿主不会收到轨迹不完整诊断。
7. `TurnHandle.wait()` 屏蔽等待者取消，CLI 必须先请求中断并等待 runner 收敛，再传播应用取消或开始下一次输入（`mini_agent/cli.py:273-291,830-854`）。
8. CLI 必须把一次 Turn 中的多个 Step 显示为层级关系；`end_turn` 只显示控制权交还，不使用成功或完成标记。`max_steps` 的用户可见定义是“一个 Turn 内允许的 agent 模型请求数”。
9. `max_steps` 必须是正整数；配置入口在 runtime 组装前拒绝，`AgentSession` 在身份生成、工具注册和 Turn 接纳前独立拒绝。
10. `AgentSession` 原样保存宿主提供的单条 system message，不读取工作区或改写提示词；CLI 负责在构造 Session 前追加本次绝对工作区的完整事实块，只有同一完整块已经存在时才跳过。

## 未包含

- `TurnOutcome` 不是 TaskSupervisor 或 BenchmarkEvaluator；本轮没有 definition of done、SWE-bench 评分或自动继续策略。
- 同步事件不是 append-only 会话事实、持久化轨迹、回放格式或多客户端传输 contract。
- 当前中断不会取消正在等待的模型请求或工具协程，也不会回滚已经发生的工具副作用。
- steering 仍是独立研究主题；当前只保证活动 Turn 期间的新输入不会静默混入请求，未来可以在显式接纳边界扩展。
- 当前没有上下文预算或压缩；完整模型可见历史会持续增长，事实记录与请求视图也尚未分离。

## 已验证行为

- `tests/test_agent_session_offline.py` 与 `tests/test_llm_adapters.py`：配置和公开 core 入口分别拒绝非正 Step 预算，且不发起模型请求；
- `tests/test_config_provenance.py`：CLI 的偶然文字与旧路径不能抑制当前工作区事实，准确块不重复，runtime 确实把组装结果交给 Session；`tests/test_agent_session_offline.py` 固定 core 原样保存提示词；
- `tests/test_agent_session_offline.py:191-304`：跨 Turn 完整历史、唯一身份、单活动 Turn、可重入接纳和创建失败回滚；
- `tests/test_agent_session_offline.py:306-416`：等待者取消、CLI 收敛、配置快照和工具驱动的多 Step；
- `tests/test_agent_session_offline.py` 与 `tests/test_tool_execution.py`：观察数据隔离、接收器失败后停用、完整批次与结构化停止原因；
- `tests/test_agent_session_offline.py:667-720`：完整 Step 中断与终止优先级；
- `tests/test_agent_loop_offline.py:381-519`：一个多 Step Turn 的 CLI 层级、中性结束标记、失败去重和帮助文案；
- README 的离线命令在 2026-08-26 最近一次实测为 `271 passed, 8 deselected in 13.81s`，没有 warning。
